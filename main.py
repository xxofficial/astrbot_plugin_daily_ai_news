import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.event import MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# 知乎专栏配置
ZHIHU_COLUMN_ID = "c_1885342192987509163"  # 橘鸦的 AI 日志
ZHIHU_COLUMN_API = f"https://www.zhihu.com/api/v4/columns/{ZHIHU_COLUMN_ID}/items"
ZHIHU_ARTICLE_KEYWORD = "早报"  # 筛选标题含此关键词的文章

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
    "每日AI资讯自动推送插件，抓取知乎 AI 早报并通过 AI 总结后推送",
    "2.0.0",
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
        logger.info("每日AI资讯推送插件已初始化（知乎专栏 + AI 总结模式）")

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
            text = self._format_summary(cached["title"], cached["url"], cached["summary"])
            yield event.plain_result(text)
            return

        yield event.plain_result("🔄 正在获取最新 AI 早报，请稍候...")
        text = await self._get_or_create_summary(today)
        if not text:
            yield event.plain_result("😞 暂时未能获取到 AI 早报，请稍后再试。")
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
        cmd_sub_count = len(self._cmd_subscriptions)
        cfg_groups = self._get_config_groups()
        cfg_group_count = len(cfg_groups)
        cfg_users = self._get_config_users()
        cfg_user_count = len(cfg_users)

        status_text = (
            "📊 **每日AI资讯推送状态**\n"
            f"📡 数据源：知乎专栏「橘鸦的 AI 日志」\n"
            f"⏰ 推送时间：每天 {hour:02d}:{minute:02d}\n"
            f"🤖 AI 总结：已启用\n"
            f"📋 指令订阅数：{cmd_sub_count}\n"
            f"📋 配置群号数：{cfg_group_count}\n"
            f"📋 配置私聊数：{cfg_user_count}\n"
            f"📚 已推送文章缓存：{len(self._sent_ids)} 篇"
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

                now = datetime.now()
                target = now.replace(
                    hour=target_hour, minute=target_minute, second=0, microsecond=0
                )
                if target <= now:
                    target += timedelta(days=1)

                wait_seconds = (target - now).total_seconds()
                logger.info(
                    f"下次推送时间：{target.strftime('%Y-%m-%d %H:%M')}，"
                    f"等待 {wait_seconds:.0f} 秒"
                )

                await asyncio.sleep(wait_seconds)
                await self._do_push()

            except asyncio.CancelledError:
                logger.info("定时推送任务已取消")
                break
            except Exception as e:
                logger.error(f"定时推送任务出错: {e}")
                await asyncio.sleep(60)

    async def _do_push(self):
        """执行一次新闻推送到所有订阅目标。"""
        logger.info("开始执行每日AI资讯推送...")

        today = datetime.now().strftime("%Y-%m-%d")

        # 检查是否已推送过今天的内容
        if today in self._sent_ids:
            logger.info(f"今日 ({today}) 已推送过，跳过")
            return

        text = await self._get_or_create_summary(today)
        if not text:
            logger.warning("未能获取到 AI 早报，跳过本次推送")
            return

        # 记录已推送
        self._sent_ids.add(today)
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

    async def _get_or_create_summary(self, date_str: str) -> Optional[str]:
        """获取指定日期的 AI 总结，优先使用缓存。"""
        # 检查缓存
        cache = self._read_summary_cache()
        cached = cache.get(date_str)
        if cached:
            logger.info(f"使用缓存的 AI 总结 ({date_str})")
            return self._format_summary(cached["title"], cached["url"], cached["summary"])

        # 缓存未命中，获取文章并总结
        article = await self._fetch_latest_article()
        if not article:
            return None

        summary = await self._summarize_with_ai(article["content"])
        if summary:
            # 写入缓存
            cache = self._read_summary_cache()
            cache[date_str] = {
                "title": article["title"],
                "url": article["url"],
                "summary": summary,
            }
            self._save_summary_cache(cache)
            return self._format_summary(article["title"], article["url"], summary)
        else:
            return self._format_fallback(article)

    # ==================== 知乎专栏抓取 ====================

    async def _fetch_latest_article(self) -> Optional[Dict]:
        """从知乎专栏获取最新的 AI 早报文章。"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.zhihu.com/",
                "Accept": "application/json, text/plain, */*",
            }

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            ) as session:
                # 尝试使用知乎专栏 API
                article = await self._fetch_from_column_api(session, headers)
                if article:
                    return article

                # API 失败，尝试从专栏页面HTML抓取
                logger.warning("知乎专栏 API 获取失败，尝试从页面获取...")
                article = await self._fetch_from_column_page(session, headers)
                if article:
                    return article

        except Exception as e:
            logger.error(f"获取知乎专栏文章失败: {e}")

        return None

    async def _fetch_from_column_api(
        self, session: aiohttp.ClientSession, headers: dict
    ) -> Optional[Dict]:
        """通过知乎 v4 API 获取专栏文章列表。"""
        try:
            url = f"{ZHIHU_COLUMN_API}?limit=10&offset=0"
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"知乎专栏 API 返回状态码 {resp.status}")
                    return None

                data = await resp.json()

                # v4 API 返回格式: {"data": [...], "paging": {...}}
                articles = data.get("data", [])

                for item in articles:
                    title = item.get("title", "")
                    if ZHIHU_ARTICLE_KEYWORD in title:
                        article_id = str(item.get("id", ""))
                        content = item.get("content", "")
                        if not content:
                            # 如果 API 没返回全文，通过文章 API 获取
                            content = await self._fetch_article_by_id(
                                session, article_id, headers
                            )

                        return {
                            "id": article_id,
                            "title": title,
                            "url": f"https://zhuanlan.zhihu.com/p/{article_id}",
                            "content": self._clean_html(content),
                            "created": item.get("created", 0),
                        }

        except Exception as e:
            logger.warning(f"知乎专栏 API 请求异常: {e}")

        return None

    async def _fetch_from_column_page(
        self, session: aiohttp.ClientSession, headers: dict
    ) -> Optional[Dict]:
        """回退方案：通过旧版专栏 API 获取文章列表。"""
        try:
            # 尝试旧版 zhuanlan API
            old_api_url = f"https://zhuanlan.zhihu.com/api/columns/{ZHIHU_COLUMN_ID}/articles?limit=10&offset=0"
            async with session.get(old_api_url, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"知乎旧版 API 返回状态码 {resp.status}")
                    return None

                data = await resp.json()

                # 旧版 API 可能直接返回列表或 {"data": [...]}
                articles = data if isinstance(data, list) else data.get("data", [])

                for item in articles:
                    title = item.get("title", "")
                    if ZHIHU_ARTICLE_KEYWORD in title:
                        article_id = str(item.get("id", ""))
                        content = item.get("content", "")

                        if not content:
                            content = await self._fetch_article_by_id(
                                session, article_id, headers
                            )

                        return {
                            "id": article_id,
                            "title": title,
                            "url": f"https://zhuanlan.zhihu.com/p/{article_id}",
                            "content": self._clean_html(content),
                            "created": item.get("created", 0),
                        }

        except Exception as e:
            logger.warning(f"知乎旧版 API 请求异常: {e}")

        return None

    async def _fetch_article_by_id(
        self, session: aiohttp.ClientSession, article_id: str, headers: dict
    ) -> str:
        """通过文章 ID 获取单篇知乎文章的正文内容。"""
        try:
            # 使用 zhuanlan API 获取单篇文章
            api_url = f"https://zhuanlan.zhihu.com/api/posts/{article_id}"
            async with session.get(api_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data.get("content", "")
                    if content:
                        return content

            # 回退：从文章页面 HTML 提取
            page_url = f"https://zhuanlan.zhihu.com/p/{article_id}"
            async with session.get(page_url, headers=headers) as resp:
                if resp.status != 200:
                    return ""
                html = await resp.text()

            # 从 js-initialData 提取
            data_match = re.search(
                r'<script\s+id="js-initialData"\s+type="text/json">(.*?)</script>',
                html,
                re.DOTALL,
            )
            if data_match:
                init_data = json.loads(data_match.group(1))
                articles = (
                    init_data.get("initialState", {})
                    .get("entities", {})
                    .get("articles", {})
                )
                for _, article in articles.items():
                    content = article.get("content", "")
                    if content:
                        return content

        except Exception as e:
            logger.warning(f"获取文章内容失败 (ID: {article_id}): {e}")

        return ""

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
            f"🔗 原文链接：{article['url']}\n"
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
