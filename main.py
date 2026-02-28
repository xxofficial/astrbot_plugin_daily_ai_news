import asyncio
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# RSS 订阅源配置
RSS_URL = "https://imjuya.github.io/juya-ai-daily/rss.xml"

# AI 总结 prompt
SUMMARY_PROMPT = """你是一个专业的 AI 资讯编辑。请将以下 AI 早报内容进行精炼总结，要求：
1. 提取最重要的 5-8 条新闻要点
2. 每条用一句话概括，突出关键信息（公司、产品、技术、数据）
3. 使用简洁的中文表述
4. 在开头加上日期
5. 保持新闻的时效性和准确性

原文内容：
{content}

请输出总结："""


@register(
    "astrbot_plugin_daily_ai_news",
    "YourName",
    "每日AI资讯自动推送插件，通过 RSS 订阅获取 AI 早报并经 AI 总结后推送",
    "3.0.0",
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
        self._cache_file = os.path.join(
            "data", "astrbot_plugin_daily_ai_news", "summary_cache.json"
        )
        # 通过指令订阅的 unified_msg_origin 集合
        self._cmd_subscriptions: Set[str] = set()
        # 已经推送过的文章 ID（用于去重）
        self._sent_ids: Set[str] = set()


    async def initialize(self):
        """插件初始化：加载持久化数据，启动定时推送任务。"""
        os.makedirs(os.path.dirname(self._subscriptions_file), exist_ok=True)
        self._load_subscriptions()
        self._load_sent_news()

        self._task = asyncio.create_task(self._schedule_loop())
        logger.info("每日AI资讯推送插件已初始化（RSS 订阅 + AI 总结模式）")

    # ==================== 指令处理 ====================

    @filter.command("ainews")
    async def cmd_ainews(self, event: AstrMessageEvent):
        """手动获取最新 AI 早报"""
        today = datetime.now().strftime("%Y-%m-%d")

        # 检查缓存
        cache = self._read_summary_cache()
        cached = cache.get(today)
        if cached:
            logger.info(f"使用缓存的 AI 总结 ({today})")
            text = self._format_summary(
                cached["title"], cached["url"], cached["summary"]
            )
            yield event.plain_result(text)
            return

        yield event.plain_result("🔄 正在从 RSS 获取最新 AI 早报，请稍候...")
        article = await self._fetch_rss_latest()
        if not article:
            yield event.plain_result("😞 暂时未能获取到 AI 早报，请稍后再试。")
            return

        text = await self._get_or_create_summary(article, today)
        if not text:
            yield event.plain_result("😞 AI 总结失败，请稍后再试。")
            return
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
            "✅ 订阅成功！每日将自动推送 AI 早报总结到本群。\n"
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
        poll_interval = config.get("rss_poll_interval", 600)
        cmd_sub_count = len(self._cmd_subscriptions)
        cfg_groups = self._get_config_groups()
        cfg_group_count = len(cfg_groups)
        cfg_users = self._get_config_users()
        cfg_user_count = len(cfg_users)

        status_text = (
            "📊 **每日AI资讯推送状态**\n"
            f"📡 数据源：RSS 订阅（橘鸦 AI 日报）\n"
            f"⏰ 首次检查时间：每天 {hour:02d}:{minute:02d}\n"
            f"🔄 轮询间隔：{poll_interval} 秒\n"
            f"🤖 AI 总结：已启用\n"
            f"📋 指令订阅数：{cmd_sub_count}\n"
            f"📋 配置群号数：{cfg_group_count}\n"
            f"📋 配置私聊数：{cfg_user_count}\n"
            f"📚 已推送文章缓存：{len(self._sent_ids)} 篇"
        )
        yield event.plain_result(status_text)

    # ==================== 定时 + 轮询推送 ====================

    async def _schedule_loop(self):
        """后台定时循环：每天在设定时间首次检查 RSS，若未更新则轮询直到获取到当日文章。"""
        while True:
            try:
                config = self.context.get_config()
                target_hour = config.get("push_hour", 8)
                target_minute = config.get("push_minute", 0)
                poll_interval = config.get("rss_poll_interval", 600)

                now = datetime.now()
                target = now.replace(
                    hour=target_hour, minute=target_minute, second=0, microsecond=0
                )
                if target <= now:
                    target += timedelta(days=1)

                wait_seconds = (target - now).total_seconds()
                logger.info(
                    f"下次 RSS 检查时间：{target.strftime('%Y-%m-%d %H:%M')}，"
                    f"等待 {wait_seconds:.0f} 秒"
                )

                await asyncio.sleep(wait_seconds)

                # 到达设定时间，开始检查 RSS 并尝试推送
                today = datetime.now().strftime("%Y-%m-%d")

                # 检查今天是否已经推送过
                if today in self._sent_ids:
                    logger.info(f"今日 ({today}) 已推送过，等待明天")
                    continue

                # 首次尝试获取 RSS
                pushed = await self._try_fetch_and_push(today)
                if pushed:
                    continue

                # RSS 尚未更新，进入轮询模式
                logger.info(
                    f"RSS 尚未更新当日 ({today}) 内容，"
                    f"进入轮询模式（间隔 {poll_interval} 秒）"
                )
                while True:
                    await asyncio.sleep(poll_interval)

                    # 如果已经过了当天，停止轮询
                    current_date = datetime.now().strftime("%Y-%m-%d")
                    if current_date != today:
                        logger.info("已过当天，停止轮询，等待明天定时触发")
                        break

                    pushed = await self._try_fetch_and_push(today)
                    if pushed:
                        break

            except asyncio.CancelledError:
                logger.info("定时推送任务已取消")
                break
            except Exception as e:
                logger.error(f"定时推送任务出错: {e}")
                await asyncio.sleep(60)

    async def _try_fetch_and_push(self, today: str) -> bool:
        """尝试从 RSS 获取当日文章并推送。返回 True 表示成功推送。"""
        try:
            article = await self._fetch_rss_latest()
            if not article:
                logger.info("RSS 获取失败或无文章")
                return False

            # 检查是否是当日文章
            if not self._is_today_article(article["title"], today):
                logger.info(
                    f"RSS 最新文章不是今日内容：{article['title']}，继续等待"
                )
                return False

            # 检查是否已推送过该文章（基于链接去重）
            if article["link"] in self._sent_ids:
                logger.info(f"该文章已推送过：{article['link']}")
                return False

            # 获取 AI 总结并推送
            await self._do_push(article, today)
            return True

        except Exception as e:
            logger.error(f"尝试获取并推送失败: {e}")
            return False

    async def _do_push(self, article: Dict, today: str):
        """执行一次新闻推送到所有订阅目标。"""
        logger.info(f"开始执行每日AI资讯推送: {article['title']}")

        text = await self._get_or_create_summary(article, today)
        if not text:
            logger.warning("未能生成 AI 总结，跳过本次推送")
            return

        # 记录已推送（同时记录日期和链接，双重去重）
        self._sent_ids.add(today)
        self._sent_ids.add(article["link"])
        if len(self._sent_ids) > 200:
            self._sent_ids = set(list(self._sent_ids)[-100:])
        self._save_sent_news()

        # 推送
        targets = self._get_all_targets()
        if not targets:
            logger.info("没有任何推送目标，跳过推送")
            return

        for umo in targets:
            try:
                chain = MessageChain().message(text)
                await self.context.send_message(umo, chain)
                logger.info(f"已推送至: {umo}")
            except Exception as e:
                logger.error(f"推送到 {umo} 失败: {e}")

        logger.info(f"每日AI资讯推送完成，已推送到 {len(targets)} 个目标")

    async def _get_or_create_summary(
        self, article: Dict, date_str: str
    ) -> Optional[str]:
        """获取指定日期的 AI 总结，优先使用缓存。"""
        # 检查缓存
        cache = self._read_summary_cache()
        cached = cache.get(date_str)
        if cached:
            logger.info(f"使用缓存的 AI 总结 ({date_str})")
            return self._format_summary(
                cached["title"], cached["url"], cached["summary"]
            )

        # 缓存未命中，进行 AI 总结
        summary = await self._summarize_with_ai(article["content"])
        if summary:
            # 写入缓存
            cache = self._read_summary_cache()
            cache[date_str] = {
                "title": article["title"],
                "url": article["link"],
                "summary": summary,
            }
            self._save_summary_cache(cache)
            return self._format_summary(article["title"], article["link"], summary)
        else:
            return self._format_fallback(article)

    # ==================== RSS 获取 ====================

    async def _fetch_rss_latest(self) -> Optional[Dict]:
        """从 RSS 订阅源获取最新一篇文章。"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; AstrBot/3.0; +https://github.com/AstrBot)",
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            }

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as session:
                async with session.get(RSS_URL, headers=headers) as resp:
                    if resp.status != 200:
                        logger.warning(f"RSS 请求返回状态码 {resp.status}")
                        return None

                    xml_text = await resp.text()

            # 解析 RSS XML
            root = ET.fromstring(xml_text)
            channel = root.find("channel")
            if channel is None:
                logger.warning("RSS XML 中未找到 channel 元素")
                return None

            # 获取第一个 item（最新文章）
            item = channel.find("item")
            if item is None:
                logger.warning("RSS 中没有任何文章")
                return None

            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            description = item.findtext("description", "").strip()

            if not title:
                logger.warning("RSS 文章标题为空")
                return None

            # 清理 HTML（description 可能包含 HTML 标签）
            content = self._clean_html(description)

            logger.info(f"RSS 获取到最新文章：{title}")
            return {
                "title": title,
                "link": link,
                "content": content,
            }

        except ET.ParseError as e:
            logger.error(f"RSS XML 解析失败: {e}")
        except Exception as e:
            logger.error(f"RSS 获取失败: {e}")

        return None

    def _is_today_article(self, title: str, today: str) -> bool:
        """检查文章标题是否包含今天的日期，判断是否为当日文章。"""
        # 标题格式示例："AI 早报 2026-02-28"
        return today in title

    # ==================== AI 总结 ====================

    async def _summarize_with_ai(self, content: str) -> Optional[str]:
        """使用 AstrBot 内置 LLM 对内容进行总结。"""
        if not content or len(content.strip()) < 50:
            logger.warning("文章内容过短，跳过 AI 总结")
            return None

        try:
            # 内容过长时截断，避免超过模型上下文限制
            max_len = 8000
            if len(content) > max_len:
                content = content[:max_len] + "\n...(内容过长已截断)"

            prompt = SUMMARY_PROMPT.format(content=content)

            # 使用 AstrBot 提供的 LLM 接口
            provider = self.context.get_using_provider()
            if provider is None:
                logger.warning("未配置 LLM provider，无法进行 AI 总结")
                return None

            resp = await provider.text_chat(
                prompt=prompt,
                session_id="ainews_summary",
            )

            if resp and resp.completion_text:
                return resp.completion_text.strip()
            else:
                logger.warning("LLM 返回结果为空")
                return None

        except Exception as e:
            logger.error(f"AI 总结失败: {e}")
            return None

    # ==================== 格式化输出 ====================

    def _format_summary(self, title: str, url: str, summary: str) -> str:
        """格式化 AI 总结后的推送文本。"""
        today = datetime.now().strftime("%Y-%m-%d")
        return (
            f"📰 AI 早报速递 | {today}\n"
            f"{'=' * 28}\n\n"
            f"📌 原文：{title}\n\n"
            f"🤖 AI 总结：\n\n"
            f"{summary}\n\n"
            f"{'=' * 28}\n"
            f"🔗 原文链接：{url}\n"
            f"💡 发送 /ainews 随时获取最新资讯"
        )

    def _format_fallback(self, article: Dict) -> str:
        """当 AI 总结失败时，使用原文摘要。"""
        today = datetime.now().strftime("%Y-%m-%d")
        content = article.get("content", "")
        if len(content) > 500:
            content = content[:500] + "..."

        return (
            f"📰 AI 早报 | {today}\n"
            f"{'=' * 28}\n\n"
            f"📌 {article['title']}\n\n"
            f"{content}\n\n"
            f"{'=' * 28}\n"
            f"🔗 原文链接：{article.get('link', '')}\n"
            f"💡 发送 /ainews 随时获取最新资讯"
        )

    # ==================== 工具方法 ====================

    def _clean_html(self, text: str) -> str:
        """去除 HTML 标签，转为纯文本。"""
        if not text:
            return ""
        clean = re.sub(r"<[^>]+>", "", text)
        clean = clean.replace("&nbsp;", " ").replace("&amp;", "&")
        clean = clean.replace("&lt;", "<").replace("&gt;", ">")
        clean = clean.replace("&quot;", '"')
        clean = re.sub(r"\n{3,}", "\n\n", clean)
        return clean.strip()

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
        """获取所有推送目标。"""
        targets = set(self._cmd_subscriptions)

        cfg_groups = self._get_config_groups()
        for group_id in cfg_groups:
            umo = f"aiocqhttp:GroupMessage:{group_id}"
            targets.add(umo)

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
        """加载已推送记录。"""
        try:
            if os.path.exists(self._sent_file):
                with open(self._sent_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._sent_ids = set(data.get("sent_ids", []))
                logger.info(f"已加载 {len(self._sent_ids)} 条已推送记录")
        except Exception as e:
            logger.error(f"加载已推送记录失败: {e}")
            self._sent_ids = set()

    def _save_sent_news(self):
        """保存已推送记录。"""
        try:
            with open(self._sent_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"sent_ids": list(self._sent_ids)},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logger.error(f"保存已推送记录失败: {e}")

    def _read_summary_cache(self) -> Dict[str, Dict]:
        """每次从文件读取 AI 总结缓存，不使用内存变量。"""
        try:
            if os.path.exists(self._cache_file):
                with open(self._cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"读取总结缓存失败: {e}")
        return {}

    def _save_summary_cache(self, cache: Dict[str, Dict]):
        """保存 AI 总结缓存到文件。"""
        try:
            # 仅保留最近 10 条缓存
            if len(cache) > 10:
                sorted_keys = sorted(cache.keys())
                cache = {k: cache[k] for k in sorted_keys[-10:]}
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(
                    cache,
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logger.error(f"保存总结缓存失败: {e}")

    async def terminate(self):
        """插件卸载时取消定时任务。"""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("每日AI资讯推送插件已停用")
