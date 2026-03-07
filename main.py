import asyncio
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import List, Dict, Set, Optional

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event import MessageChain
from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star, register
from astrbot.api.star import StarTools
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
    "xx",
    "订阅橘鸦AI日报并进行AI总结",
    "3.0.0",
    "https://github.com/xxofficial/astrbot_plugin_daily_ai_news",
)
class DailyAINewsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._task: Optional[asyncio.Task] = None

        # 使用框架规范的数据目录
        self._data_dir = StarTools.get_data_dir("astrbot_plugin_daily_ai_news")
        self._subscriptions_file = self._data_dir / "subscriptions.json"
        self._sent_file = self._data_dir / "sent_news.json"
        self._cache_file = self._data_dir / "summary_cache.json"

        # 通过指令订阅的 unified_msg_origin 集合
        self._cmd_subscriptions: Set[str] = set()
        # 已推送的日期和链接（分离存储）
        self._sent_dates: Set[str] = set()
        self._sent_links: Set[str] = set()

        # 文件读写互斥锁
        self._file_lock = asyncio.Lock()

    async def initialize(self):
        """插件初始化：加载持久化数据，启动定时推送任务。"""
        os.makedirs(self._data_dir, exist_ok=True)
        await self._load_subscriptions()
        await self._load_sent_news()

        self._task = asyncio.create_task(self._schedule_loop())
        logger.info("每日AI资讯推送插件已初始化（RSS 订阅 + AI 总结模式）")

    # ==================== 指令处理 ====================

    @filter.command("ainews")
    async def cmd_ainews(self, event: AstrMessageEvent):
        """手动获取最新 AI 早报"""
        yield event.plain_result("🔄 正在从 RSS 获取最新 AI 早报，请稍候...")
        article = await self._fetch_rss_latest()
        if not article:
            yield event.plain_result("😞 暂时未能获取到 AI 早报，请稍后再试。")
            return

        # 使用文章实际日期作为缓存 key，而非当天日期
        article_date = self._parse_article_date(article)

        # 检查缓存
        cache = await self._read_summary_cache()
        cached = cache.get(article_date)
        if cached:
            logger.info(f"使用缓存的 AI 总结 ({article_date})")
            text = self._format_summary(
                cached["title"], cached["url"], cached["summary"], article_date
            )
            yield event.plain_result(text)
            return

        text = await self._get_or_create_summary(article, article_date)
        if not text:
            yield event.plain_result("😞 AI 总结失败，请稍后再试。")
            return
        yield event.plain_result(text)

    @filter.command("ainews_sub")
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """订阅每日 AI 资讯推送（在群聊中使用）"""
        umo = event.unified_msg_origin
        logger.info(f"订阅状态: {umo}")
        if umo in self._cmd_subscriptions:
            yield event.plain_result("📢 当前会话已订阅每日AI资讯推送。")
            return
        self._cmd_subscriptions.add(umo)
        await self._save_subscriptions()
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
        await self._save_subscriptions()
        yield event.plain_result("✅ 已取消每日AI资讯推送订阅。")

    @filter.command("ainews_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看推送状态"""
        hour = self.config.get("push_hour", 8)
        minute = self.config.get("push_minute", 0)
        poll_interval = self.config.get("rss_poll_interval", 600)
        cmd_sub_count = len(self._cmd_subscriptions)
        cfg_groups = self._get_config_groups()
        cfg_group_count = len(cfg_groups)
        cfg_users = self._get_config_users()
        cfg_user_count = len(cfg_users)
        
        # 飞书
        lark_platform_id = self.config.get("lark_platform_id", "")
        lark_users = self._get_lark_users()
        lark_user_count = len(lark_users)
        lark_groups = self._get_lark_groups()
        lark_group_count = len(lark_groups)
        
        enable_ai = self.config.get("enable_ai_summary", True)
        ai_status = "已启用" if enable_ai else "未启用(原文摘要)"

        status_text = (
            "📊 **每日AI资讯推送状态**\n"
            f"📡 数据源：RSS 订阅（橘鸦 AI 日报）\n"
            f"⏰ 首次检查时间：每天 {hour:02d}:{minute:02d}\n"
            f"🔄 轮询间隔：{poll_interval} 秒\n"
            f"🤖 AI 总结：{ai_status}\n"
            f"📋 指令订阅数：{cmd_sub_count}\n"
            f"📋 QQ 群号数：{cfg_group_count}\n"
            f"📋 QQ 私聊数：{cfg_user_count}\n"
            f"📋 飞书平台：{lark_platform_id or '未配置'}\n"
            f"📋 飞书私聊数：{lark_user_count}\n"
            f"📋 飞书群数：{lark_group_count}\n"
            f"📚 已推送日期缓存：{len(self._sent_dates)} 天\n"
            f"📚 已推送文章缓存：{len(self._sent_links)} 篇"
        )
        yield event.plain_result(status_text)

    # ==================== 定时 + 轮询推送 ====================

    async def _schedule_loop(self):
        """后台定时循环：每天在设定时间首次检查 RSS，若未更新则轮询直到获取到当日文章。"""
        # 启动时先执行一次补偿检查
        await self._startup_compensation_check()

        while True:
            try:
                target_hour = self.config.get("push_hour", 8)
                target_minute = self.config.get("push_minute", 0)
                poll_interval = self.config.get("rss_poll_interval", 600)

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
                if today in self._sent_dates:
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

    async def _startup_compensation_check(self):
        """启动时补偿检查：若当前已过推送时间且当天未推送过，立即尝试推送。"""
        try:
            target_hour = self.config.get("push_hour", 8)
            target_minute = self.config.get("push_minute", 0)

            now = datetime.now()
            today = now.strftime("%Y-%m-%d")

            # 只在过了今天的推送时间后才补偿
            target_time = now.replace(
                hour=target_hour, minute=target_minute, second=0, microsecond=0
            )
            if now < target_time:
                logger.info("当前未到推送时间，跳过补偿检查")
                return

            if today in self._sent_dates:
                logger.info(f"今日 ({today}) 已推送过，跳过补偿检查")
                return

            logger.info(f"启动补偿检查：今日 ({today}) 尚未推送，尝试拉取并推送")
            await self._try_fetch_and_push(today)

        except Exception as e:
            logger.error(f"启动补偿检查失败: {e}")

    async def _try_fetch_and_push(self, today: str) -> bool:
        """尝试从 RSS 获取当日文章并推送。返回 True 表示成功推送。"""
        try:
            article = await self._fetch_rss_latest()
            if not article:
                logger.info("RSS 获取失败或无文章")
                return False

            # 解析文章日期
            article_date = self._parse_article_date(article)

            # 检查是否是当日文章
            if article_date != today:
                logger.info(
                    f"RSS 最新文章日期 ({article_date}) 不是今日 ({today})，继续等待"
                )
                return False

            # 检查是否已推送过该文章（基于链接去重）
            if article["link"] in self._sent_links:
                logger.info(f"该文章已推送过：{article['link']}")
                return False

            # 获取 AI 总结并推送
            await self._do_push(article, article_date)
            return True

        except Exception as e:
            logger.error(f"尝试获取并推送失败: {e}")
            return False

    async def _do_push(self, article: Dict, article_date: str):
        """执行一次新闻推送到所有订阅目标。"""
        logger.info(f"开始执行每日AI资讯推送: {article['title']}")

        text = await self._get_or_create_summary(article, article_date)
        if not text:
            logger.warning("未能生成 AI 总结，跳过本次推送")
            return

        # 推送
        targets = self._get_all_targets()
        if not targets:
            logger.info("没有任何推送目标，跳过推送")
            return

        success_count = 0
        for umo in targets:
            try:
                chain = MessageChain().message(text)
                await self.context.send_message(umo, chain)
                logger.info(f"已推送至: {umo}")
                success_count += 1
            except Exception as e:
                logger.error(f"推送到 {umo} 失败: {e}")

        # 仅在至少一个目标发送成功后才标记已推送
        if success_count > 0:
            self._sent_dates.add(article_date)
            self._sent_links.add(article["link"])
            # 裁剪：日期保留最近 30 天，链接保留最近 100 条
            if len(self._sent_dates) > 30:
                sorted_dates = sorted(self._sent_dates)
                self._sent_dates = set(sorted_dates[-30:])
            if len(self._sent_links) > 100:
                self._sent_links = set(list(self._sent_links)[-100:])
            await self._save_sent_news()
            logger.info(
                f"每日AI资讯推送完成，成功推送到 {success_count}/{len(targets)} 个目标"
            )
        else:
            logger.warning("所有推送目标均失败，不标记已推送，后续将重试")

    async def _get_or_create_summary(
        self, article: Dict, article_date: str
    ) -> Optional[str]:
        """获取指定日期的 AI 总结，优先使用缓存。"""
        # 检查是否启用 AI 总结
        enable_ai_summary = self.config.get("enable_ai_summary", True)
        video_links = article.get("video_links", [])
        
        # 如果禁用 AI 总结，直接使用 fallback 格式
        if not enable_ai_summary:
            logger.info("AI 总结功能已禁用，使用原文摘要")
            return self._format_fallback(article, article_date)
        
        # 检查缓存
        cache = await self._read_summary_cache()
        cached = cache.get(article_date)
        if cached:
            logger.info(f"使用缓存的 AI 总结 ({article_date})")
            # 使用缓存的视频链接，如果没有则使用当前的
            cached_video_links = cached.get("video_links", video_links)
            return self._format_summary(
                cached["title"], cached["url"], cached["summary"], article_date, cached_video_links
            )

        # 缓存未命中，进行 AI 总结
        summary = await self._summarize_with_ai(article["content"])
        if summary:
            # 写入缓存
            cache = await self._read_summary_cache()
            cache[article_date] = {
                "title": article["title"],
                "url": article["link"],
                "summary": summary,
                "video_links": video_links,
            }
            await self._save_summary_cache(cache)
            return self._format_summary(
                article["title"], article["link"], summary, article_date, video_links
            )
        else:
            return self._format_fallback(article, article_date)

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
            pub_date = item.findtext("pubDate", "").strip()

            if not title:
                logger.warning("RSS 文章标题为空")
                return None

            # 清理 HTML（description 可能包含 HTML 标签）
            content = self._clean_html(description)
            # 提取视频链接（B站、YouTube等）
            video_links = self._extract_video_links(description)
            
            # 如果 RSS 中没有视频链接，尝试从 B 站 API 获取最新视频
            if not video_links:
                bilibili_video = await self._fetch_bilibili_latest_video()
                if bilibili_video:
                    video_links.append(bilibili_video)

            logger.info(f"RSS 获取到最新文章：{title}")
            return {
                "title": title,
                "link": link,
                "content": content,
                "pub_date": pub_date,
                "video_links": video_links,
            }

        except ET.ParseError as e:
            logger.error(f"RSS XML 解析失败: {e}")
        except Exception as e:
            logger.error(f"RSS 获取失败: {e}")

        return None

    def _parse_article_date(self, article: Dict) -> str:
        """从文章中解析日期，优先使用 pubDate，回退从标题提取，最后使用当天日期。"""
        # 优先：解析 pubDate（RFC 2822 格式）
        pub_date = article.get("pub_date", "")
        if pub_date:
            try:
                dt = parsedate_to_datetime(pub_date)
                return dt.strftime("%Y-%m-%d")
            except Exception:
                pass

        # 回退：从标题提取 YYYY-MM-DD
        title = article.get("title", "")
        match = re.search(r"(\d{4}-\d{2}-\d{2})", title)
        if match:
            return match.group(1)

        # 最后回退：当天日期
        return datetime.now().strftime("%Y-%m-%d")

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

    def _format_summary(
        self, title: str, url: str, summary: str, article_date: str, video_links: List[str] = None
    ) -> str:
        """格式化 AI 总结后的推送文本。"""
        video_section = ""
        if video_links:
            video_section = "\n🎬 相关视频：\n"
            for i, link in enumerate(video_links, 1):
                video_section += f"  {i}. {link}\n"
        
        return (
            f"📰 AI 早报速递 | {article_date}\n"
            f"{'=' * 28}\n\n"
            f"📌 原文：{title}\n\n"
            f"🤖 AI 总结：\n\n"
            f"{summary}\n\n"
            f"{'=' * 28}\n"
            f"🔗 原文链接：{url}\n"
            f"{video_section}"
            f"💡 发送 /ainews 随时获取最新资讯"
        )

    def _format_fallback(self, article: Dict, article_date: str) -> str:
        """当 AI 总结失败时，使用原文摘要。"""
        content = article.get("content", "")
        if len(content) > 500:
            content = content[:500] + "..."
        
        video_links = article.get("video_links", [])
        video_section = ""
        if video_links:
            video_section = "\n🎬 相关视频：\n"
            for i, link in enumerate(video_links, 1):
                video_section += f"  {i}. {link}\n"

        return (
            f"📰 AI 早报 | {article_date}\n"
            f"{'=' * 28}\n\n"
            f"📌 {article['title']}\n\n"
            f"{content}\n\n"
            f"{'=' * 28}\n"
            f"🔗 原文链接：{article.get('link', '')}\n"
            f"{video_section}"
            f"💡 发送 /ainews 随时获取最新资讯"
        )

    # ==================== 工具方法 ====================

    def _clean_html(self, text: str) -> str:
        """去除 HTML 标签，转为纯文本。返回清理后的文本。"""
        if not text:
            return ""
        clean = re.sub(r"<[^>]+>", "", text)
        clean = clean.replace("&nbsp;", " ").replace("&amp;", "&")
        clean = clean.replace("&lt;", "<").replace("&gt;", ">")
        clean = clean.replace("&quot;", '"')
        clean = re.sub(r"\n{3,}", "\n\n", clean)
        return clean.strip()

    def _extract_video_links(self, text: str) -> List[str]:
        """从 HTML 内容中提取视频链接（B站、YouTube等）。"""
        video_links = []
        # B站链接模式：bilibili.com/video 或 b23.tv 短链接
        bilibili_patterns = [
            r'https?://(?:www\.)?bilibili\.com/video/[^\s<>"\']+',
            r'https?://b23\.tv/[^\s<>"\']+',
        ]
        # YouTube 链接模式
        youtube_patterns = [
            r'https?://(?:www\.)?youtube\.com/watch\?v=[^\s<>"\']+',
            r'https?://youtu\.be/[^\s<>"\']+',
        ]
        
        all_patterns = bilibili_patterns + youtube_patterns
        for pattern in all_patterns:
            matches = re.findall(pattern, text)
            for link in matches:
                # 清理链接末尾可能的多余字符
                link = re.sub(r'[.,;:!?）】\)]+$', '', link)
                if link not in video_links:
                    video_links.append(link)
        
        return video_links

    async def _fetch_bilibili_latest_video(self) -> Optional[str]:
        """从 B 站 API 获取橘鸦 Juya 最新视频链接。"""
        # 橘鸦 Juya 的 B 站 mid
        BILIBILI_MID = "285286947"
        
        # 尝试多个 API 端点
        api_endpoints = [
            # API 1: UP 主投稿列表
            f"https://api.bilibili.com/x/space/arc/search?mid={BILIBILI_MID}&ps=5&tid=0&pn=1&order=pubdate",
        ]
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"https://space.bilibili.com/{BILIBILI_MID}/",
        }
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            try:
                async with session.get(api_endpoints[0], headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("code") == 0:
                            vlist = data.get("data", {}).get("list", {}).get("vlist", [])
                            if vlist:
                                # 查找标题包含 "AI 早报" 的最新视频
                                for video in vlist:
                                    title = video.get("title", "")
                                    if "AI 早报" in title or "AI早报" in title:
                                        bvid = video.get("bvid")
                                        if bvid:
                                            video_url = f"https://www.bilibili.com/video/{bvid}"
                                            logger.info(f"获取到 B 站最新视频: {title}")
                                            return video_url
                                
                                # 如果没找到 AI 早报视频，返回最新视频
                                latest = vlist[0]
                                bvid = latest.get("bvid")
                                if bvid:
                                    video_url = f"https://www.bilibili.com/video/{bvid}"
                                    logger.info(f"获取到 B 站最新视频: {latest.get('title', '')}")
                                    return video_url
                        else:
                            logger.warning(f"B 站 API 返回错误: {data.get('message', '未知错误')}")
            except Exception as e:
                logger.warning(f"B 站 API 请求失败: {e}")
        
        # 如果 API 失败，返回 UP 主空间链接
        logger.info("B 站 API 不可用，返回 UP 主空间链接")
        return f"https://space.bilibili.com/{BILIBILI_MID}/"

    def _get_config_groups(self) -> List[str]:
        """从配置中获取手动填写的 QQ 群号列表。"""
        groups_text = self.config.get("subscribed_groups", "")
        if not groups_text or not groups_text.strip():
            return []
        return [g.strip() for g in groups_text.strip().split("\n") if g.strip()]

    def _get_config_users(self) -> List[str]:
        """从配置中获取手动填写的私聊 QQ 号列表。"""
        users_text = self.config.get("subscribed_users", "")
        if not users_text or not users_text.strip():
            return []
        return [u.strip() for u in users_text.strip().split("\n") if u.strip()]

    def _get_lark_users(self) -> List[str]:
        """从配置中获取飞书用户 open_id 列表。"""
        users_text = self.config.get("lark_users", "")
        if not users_text or not users_text.strip():
            return []
        return [u.strip() for u in users_text.strip().split("\n") if u.strip()]

    def _get_lark_groups(self) -> List[str]:
        """从配置中获取飞书群 chat_id 列表。"""
        groups_text = self.config.get("lark_groups", "")
        if not groups_text or not groups_text.strip():
            return []
        return [g.strip() for g in groups_text.strip().split("\n") if g.strip()]

    def _get_all_targets(self) -> Set[str]:
        """获取所有推送目标。"""
        targets = set(self._cmd_subscriptions)

        # QQ 平台 ID，适配器平台前缀
        qq_platform_id = self.config.get("qq_platform_id", "aiocqhttp").strip() or "aiocqhttp"

        # QQ 群
        cfg_groups = self._get_config_groups()
        for group_id in cfg_groups:
            umo = f"{qq_platform_id}:GroupMessage:{group_id}"
            targets.add(umo)

        # QQ 用户
        cfg_users = self._get_config_users()
        for user_id in cfg_users:
            umo = f"{qq_platform_id}:FriendMessage:{user_id}"
            targets.add(umo)

        # 飞书
        lark_platform_id = self.config.get("lark_platform_id", "").strip()
        if lark_platform_id:
            # 飞书用户
            lark_users = self._get_lark_users()
            for open_id in lark_users:
                umo = f"{lark_platform_id}:FriendMessage:{open_id}"
                targets.add(umo)
            
            # 飞书群
            lark_groups = self._get_lark_groups()
            for chat_id in lark_groups:
                umo = f"{lark_platform_id}:GroupMessage:{chat_id}"
                targets.add(umo)

        return targets

    # ==================== 持久化（带锁 + 原子写）====================

    def _atomic_write(self, filepath, data: dict):
        """原子写入 JSON 文件：先写临时文件，再 rename 替换。"""
        dir_path = os.path.dirname(str(filepath))
        try:
            fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, str(filepath))
            except Exception:
                # 清理临时文件
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.error(f"原子写入 {filepath} 失败: {e}")
            raise

    async def _load_subscriptions(self):
        """从文件加载指令订阅列表。"""
        async with self._file_lock:
            try:
                filepath = str(self._subscriptions_file)
                if os.path.exists(filepath):
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._cmd_subscriptions = set(data.get("subscriptions", []))
                    logger.info(f"已加载 {len(self._cmd_subscriptions)} 个指令订阅")
            except Exception as e:
                logger.error(f"加载订阅列表失败: {e}")
                self._cmd_subscriptions = set()

    async def _save_subscriptions(self):
        """将指令订阅列表保存到文件。"""
        async with self._file_lock:
            try:
                self._atomic_write(
                    self._subscriptions_file,
                    {"subscriptions": list(self._cmd_subscriptions)},
                )
            except Exception as e:
                logger.error(f"保存订阅列表失败: {e}")

    async def _load_sent_news(self):
        """加载已推送记录。"""
        async with self._file_lock:
            try:
                filepath = str(self._sent_file)
                if os.path.exists(filepath):
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    # 兼容旧格式：如果是旧的 sent_ids 格式，自动迁移
                    if "sent_ids" in data:
                        old_ids = set(data.get("sent_ids", []))
                        for item in old_ids:
                            if re.match(r"\d{4}-\d{2}-\d{2}$", item):
                                self._sent_dates.add(item)
                            else:
                                self._sent_links.add(item)
                        logger.info("已从旧格式迁移已推送记录")
                    else:
                        self._sent_dates = set(data.get("sent_dates", []))
                        self._sent_links = set(data.get("sent_links", []))
                    logger.info(
                        f"已加载 {len(self._sent_dates)} 个已推送日期，"
                        f"{len(self._sent_links)} 个已推送链接"
                    )
            except Exception as e:
                logger.error(f"加载已推送记录失败: {e}")
                self._sent_dates = set()
                self._sent_links = set()

    async def _save_sent_news(self):
        """保存已推送记录。"""
        async with self._file_lock:
            try:
                self._atomic_write(
                    self._sent_file,
                    {
                        "sent_dates": sorted(self._sent_dates),
                        "sent_links": list(self._sent_links),
                    },
                )
            except Exception as e:
                logger.error(f"保存已推送记录失败: {e}")

    async def _read_summary_cache(self) -> Dict[str, Dict]:
        """从文件读取 AI 总结缓存。"""
        async with self._file_lock:
            try:
                filepath = str(self._cache_file)
                if os.path.exists(filepath):
                    with open(filepath, "r", encoding="utf-8") as f:
                        return json.load(f)
            except Exception as e:
                logger.error(f"读取总结缓存失败: {e}")
            return {}

    async def _save_summary_cache(self, cache: Dict[str, Dict]):
        """保存 AI 总结缓存到文件。"""
        async with self._file_lock:
            try:
                # 仅保留最近 10 条缓存
                if len(cache) > 10:
                    sorted_keys = sorted(cache.keys())
                    cache = {k: cache[k] for k in sorted_keys[-10:]}
                self._atomic_write(self._cache_file, cache)
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
