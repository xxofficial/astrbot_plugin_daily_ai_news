import asyncio
import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import List, Dict, Set

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# 默认 RSS 源列表
DEFAULT_RSS_SOURCES = [
    "https://sspai.com/feed",                                               # 少数派
    "https://www.huxiu.com/rss/0.xml",                                      # 虎嗅
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",     # The Verge AI
    "https://feeds.feedburner.com/venturebeat/SZYF",                        # VentureBeat
    "https://www.marktechpost.com/feed/",                                   # MarkTechPost AI
]


@register(
    "astrbot_plugin_daily_ai_news",
    "YourName",
    "每日AI资讯自动推送插件，定时抓取多个 AI 资讯 RSS 源并推送到 QQ 群",
    "1.0.0",
    "https://github.com/YourName/astrbot_plugin_daily_ai_news",
)
class DailyAINewsPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._task: asyncio.Task = None
        self._subscriptions_file = os.path.join(
            "data", "astrbot_plugin_daily_ai_news", "subscriptions.json"
        )
        self._sent_file = os.path.join(
            "data", "astrbot_plugin_daily_ai_news", "sent_news.json"
        )
        # 通过指令订阅的 unified_msg_origin 集合
        self._cmd_subscriptions: Set[str] = set()
        # 已经推送过的新闻链接（用于去重，避免重复推送）
        self._sent_urls: Set[str] = set()

    async def initialize(self):
        """插件初始化：加载持久化数据，启动定时推送任务。"""
        # 确保数据目录存在
        os.makedirs(os.path.dirname(self._subscriptions_file), exist_ok=True)
        # 加载持久化的订阅列表
        self._load_subscriptions()
        # 加载已推送记录
        self._load_sent_news()
        # 启动后台定时推送任务
        self._task = asyncio.create_task(self._schedule_loop())
        logger.info("每日AI资讯推送插件已初始化")

    # ==================== 指令处理 ====================

    @filter.command("ainews")
    async def cmd_ainews(self, event: AstrMessageEvent):
        """手动获取最新 AI 资讯"""
        yield event.plain_result("🔄 正在获取最新 AI 资讯，请稍候...")
        news_list = await self._fetch_all_news()
        if not news_list:
            yield event.plain_result("😞 暂时未能获取到 AI 资讯，请稍后再试。")
            return
        config = self.context.get_config()
        count = config.get("news_count", 10)
        text = self._format_news(news_list[:count])
        yield event.plain_result(text)

    @filter.command("ainews_sub")
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """订阅每日 AI 资讯推送（在群聊中使用）"""
        umo = event.unified_msg_origin
        if umo in self._cmd_subscriptions:
            yield event.plain_result("📢 当前会话已订阅每日AI资讯推送。")
            return
        self._cmd_subscriptions.add(umo)
        self._save_subscriptions()
        yield event.plain_result(
            "✅ 订阅成功！每日将自动推送最新的 AI 资讯到本群。\n"
            "取消订阅请发送 /ainews_unsub"
        )

    @filter.command("ainews_unsub")
    async def cmd_unsubscribe(self, event: AstrMessageEvent):
        """取消每日 AI 资讯推送订阅"""
        umo = event.unified_msg_origin
        if umo not in self._cmd_subscriptions:
            yield event.plain_result("ℹ️ 当前会话未通过指令订阅过 AI 资讯推送。")
            return
        self._cmd_subscriptions.discard(umo)
        self._save_subscriptions()
        yield event.plain_result("✅ 已取消每日AI资讯推送订阅。")

    @filter.command("ainews_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看推送状态"""
        config = self.context.get_config()
        hour = config.get("push_hour", 8)
        minute = config.get("push_minute", 0)
        count = config.get("news_count", 10)
        cmd_sub_count = len(self._cmd_subscriptions)
        cfg_groups = self._get_config_groups()
        cfg_group_count = len(cfg_groups)
        cfg_users = self._get_config_users()
        cfg_user_count = len(cfg_users)

        status_text = (
            "📊 **每日AI资讯推送状态**\n"
            f"⏰ 推送时间：每天 {hour:02d}:{minute:02d}\n"
            f"📰 每次推送：{count} 条\n"
            f"📋 指令订阅数：{cmd_sub_count}\n"
            f"📋 配置群号数：{cfg_group_count}\n"
            f"📋 配置私聊数：{cfg_user_count}\n"
            f"📚 已推送新闻缓存：{len(self._sent_urls)} 条"
        )
        yield event.plain_result(status_text)

    # ==================== 定时推送 ====================

    async def _schedule_loop(self):
        """后台定时循环，每天在设定时间推送新闻。"""
        while True:
            try:
                config = self.context.get_config()
                target_hour = config.get("push_hour", 8)
                target_minute = config.get("push_minute", 0)

                # 计算距离下一次推送的秒数
                now = datetime.now()
                target = now.replace(
                    hour=target_hour, minute=target_minute, second=0, microsecond=0
                )
                if target <= now:
                    # 今天的推送时间已过，推到明天
                    target += timedelta(days=1)

                wait_seconds = (target - now).total_seconds()
                logger.info(
                    f"下次推送时间：{target.strftime('%Y-%m-%d %H:%M')}，"
                    f"等待 {wait_seconds:.0f} 秒"
                )

                await asyncio.sleep(wait_seconds)

                # 执行推送
                await self._do_push()

            except asyncio.CancelledError:
                logger.info("定时推送任务已取消")
                break
            except Exception as e:
                logger.error(f"定时推送任务出错: {e}")
                # 出错后等待 60 秒再重试
                await asyncio.sleep(60)

    async def _do_push(self):
        """执行一次新闻推送到所有订阅目标。"""
        logger.info("开始执行每日AI资讯推送...")

        news_list = await self._fetch_all_news()
        if not news_list:
            logger.warning("未能获取到任何新闻，跳过本次推送")
            return

        config = self.context.get_config()
        count = config.get("news_count", 10)

        # 过滤掉已经推送过的新闻
        new_news = [n for n in news_list if n["link"] not in self._sent_urls]
        if not new_news:
            logger.info("没有新的未推送新闻，跳过本次推送")
            return

        selected = new_news[:count]
        text = self._format_news(selected)

        # 记录已推送
        for n in selected:
            self._sent_urls.add(n["link"])
        # 只保留最近 500 条记录，避免无限增长
        if len(self._sent_urls) > 500:
            self._sent_urls = set(list(self._sent_urls)[-300:])
        self._save_sent_news()

        # 合并所有需要推送的目标
        targets = self._get_all_targets()
        if not targets:
            logger.info("没有任何推送目标，跳过推送")
            return

        # 发送到所有目标
        for umo in targets:
            try:
                chain = MessageChain().message(text)
                await self.context.send_message(umo, chain)
                logger.info(f"已推送至: {umo}")
            except Exception as e:
                logger.error(f"推送到 {umo} 失败: {e}")

        logger.info(f"每日AI资讯推送完成，共推送 {len(selected)} 条新闻到 {len(targets)} 个目标")

    # ==================== RSS 抓取 ====================

    async def _fetch_all_news(self) -> List[Dict]:
        """从所有配置的 RSS 源抓取新闻并合并排序。"""
        sources = self._get_rss_sources()
        all_news = []

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            tasks = [self._fetch_rss(session, url) for url in sources]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(f"RSS 源 {sources[i]} 抓取失败: {result}")
            elif result:
                all_news.extend(result)

        # 去重（按链接）
        seen = set()
        unique_news = []
        for item in all_news:
            if item["link"] not in seen:
                seen.add(item["link"])
                unique_news.append(item)

        # 按发布时间降序排列
        unique_news.sort(key=lambda x: x.get("pub_time", ""), reverse=True)
        return unique_news

    async def _fetch_rss(self, session: aiohttp.ClientSession, url: str) -> List[Dict]:
        """抓取单个 RSS 源并解析。"""
        news_list = []
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; AstrBot-AI-News/1.0)"
            }
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"RSS {url} 返回状态码 {resp.status}")
                    return []
                text = await resp.text()

            root = ET.fromstring(text)

            # 支持 RSS 2.0 和 Atom 格式
            # RSS 2.0
            items = root.findall(".//item")
            if items:
                for item in items:
                    title = self._get_xml_text(item, "title")
                    link = self._get_xml_text(item, "link")
                    pub_date = self._get_xml_text(item, "pubDate")
                    description = self._get_xml_text(item, "description")
                    if title and link:
                        news_list.append({
                            "title": title.strip(),
                            "link": link.strip(),
                            "pub_time": pub_date or "",
                            "summary": self._clean_html(description or ""),
                        })
                return news_list

            # Atom 格式
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall(".//atom:entry", ns)
            if not entries:
                # 尝试无命名空间
                entries = root.findall(".//entry")
            for entry in entries:
                title = self._get_xml_text(entry, "title", ns)
                link_el = entry.find("atom:link", ns)
                if link_el is None:
                    link_el = entry.find("link")
                link = link_el.get("href", "") if link_el is not None else ""
                pub_date = (
                    self._get_xml_text(entry, "updated", ns)
                    or self._get_xml_text(entry, "published", ns)
                    or ""
                )
                summary = self._get_xml_text(entry, "summary", ns) or ""
                if title and link:
                    news_list.append({
                        "title": title.strip(),
                        "link": link.strip(),
                        "pub_time": pub_date,
                        "summary": self._clean_html(summary),
                    })

        except ET.ParseError as e:
            logger.warning(f"RSS XML 解析失败 ({url}): {e}")
        except Exception as e:
            logger.warning(f"RSS 抓取异常 ({url}): {e}")

        return news_list

    # ==================== 工具方法 ====================

    def _get_xml_text(self, element, tag, ns=None):
        """安全获取 XML 子元素文本。"""
        if ns:
            for prefix, uri in ns.items():
                el = element.find(f"{{{uri}}}{tag}")
                if el is not None and el.text:
                    return el.text
        el = element.find(tag)
        return el.text if el is not None and el.text else None

    def _clean_html(self, text: str) -> str:
        """简单去除 HTML 标签。"""
        import re
        clean = re.sub(r"<[^>]+>", "", text)
        clean = clean.replace("&nbsp;", " ").replace("&amp;", "&")
        clean = clean.replace("&lt;", "<").replace("&gt;", ">")
        clean = clean.replace("&quot;", '"')
        # 截取前 100 个字符作为摘要
        clean = clean.strip()
        if len(clean) > 100:
            clean = clean[:100] + "..."
        return clean

    def _format_news(self, news_list: List[Dict]) -> str:
        """将新闻列表格式化为推送文本。"""
        today = datetime.now().strftime("%Y-%m-%d")
        lines = [f"📰 每日AI资讯 | {today}\n{'=' * 28}\n"]

        for i, news in enumerate(news_list, 1):
            title = news["title"]
            link = news["link"]
            summary = news.get("summary", "")
            line = f"{i}. {title}\n   🔗 {link}"
            if summary:
                line += f"\n   📝 {summary}"
            lines.append(line)

        lines.append(f"\n{'=' * 28}")
        lines.append("💡 发送 /ainews 随时获取最新资讯")
        return "\n\n".join(lines)

    def _get_rss_sources(self) -> List[str]:
        """获取 RSS 源列表，优先使用配置。"""
        config = self.context.get_config()
        sources_text = config.get("rss_sources", "")
        if sources_text and sources_text.strip():
            sources = [
                s.strip() for s in sources_text.strip().split("\n") if s.strip()
            ]
            if sources:
                return sources
        return DEFAULT_RSS_SOURCES

    def _get_config_groups(self) -> List[str]:
        """从配置中获取手动填写的 QQ 群号列表。"""
        config = self.context.get_config()
        groups_text = config.get("subscribed_groups", "")
        if not groups_text or not groups_text.strip():
            return []
        return [g.strip() for g in groups_text.strip().split("\n") if g.strip()]

    def _get_config_users(self) -> List[str]:
        """从配置中获取手动填写的私聊 QQ 号列表。"""
        config = self.context.get_config()
        users_text = config.get("subscribed_users", "")
        if not users_text or not users_text.strip():
            return []
        return [u.strip() for u in users_text.strip().split("\n") if u.strip()]

    def _get_all_targets(self) -> Set[str]:
        """
        获取所有推送目标的 unified_msg_origin。
        合并指令订阅、配置群号、配置私聊三种来源。
        """
        targets = set(self._cmd_subscriptions)

        # 将配置中的群号转换为 unified_msg_origin 格式
        cfg_groups = self._get_config_groups()
        for group_id in cfg_groups:
            umo = f"aiocqhttp:GroupMessage:{group_id}"
            targets.add(umo)

        # 将配置中的私聊 QQ 号转换为 unified_msg_origin 格式
        cfg_users = self._get_config_users()
        for user_id in cfg_users:
            umo = f"aiocqhttp:FriendMessage:{user_id}"
            targets.add(umo)

        return targets

    # ==================== 持久化 ====================

    def _load_subscriptions(self):
        """从文件加载指令订阅列表。"""
        try:
            if os.path.exists(self._subscriptions_file):
                with open(self._subscriptions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._cmd_subscriptions = set(data.get("subscriptions", []))
                logger.info(f"已加载 {len(self._cmd_subscriptions)} 个指令订阅")
        except Exception as e:
            logger.error(f"加载订阅列表失败: {e}")
            self._cmd_subscriptions = set()

    def _save_subscriptions(self):
        """将指令订阅列表保存到文件。"""
        try:
            with open(self._subscriptions_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"subscriptions": list(self._cmd_subscriptions)},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logger.error(f"保存订阅列表失败: {e}")

    def _load_sent_news(self):
        """加载已推送新闻记录。"""
        try:
            if os.path.exists(self._sent_file):
                with open(self._sent_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._sent_urls = set(data.get("sent_urls", []))
                logger.info(f"已加载 {len(self._sent_urls)} 条已推送记录")
        except Exception as e:
            logger.error(f"加载已推送记录失败: {e}")
            self._sent_urls = set()

    def _save_sent_news(self):
        """保存已推送新闻记录。"""
        try:
            with open(self._sent_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"sent_urls": list(self._sent_urls)},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logger.error(f"保存已推送记录失败: {e}")

    async def terminate(self):
        """插件卸载时取消定时任务。"""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("每日AI资讯推送插件已停用")
