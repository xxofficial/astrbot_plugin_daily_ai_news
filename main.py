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
RSS_URL = "https://daily.juya.uk/rss.xml"

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
    "1.02",
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

        ai_enabled = "已启用" if self.config.get("enable_ai_summary", True) else "已关闭"

        # 获取当前使用的供应商信息
        provider_id = self.config.get("summary_provider_id", "").strip()
        if provider_id:
            provider_info = f"指定供应商 (ID: {provider_id})"
        else:
            provider_info = "默认供应商"

        status_text = (
            "📊 **每日AI资讯推送状态**\n"
            f"📡 数据源：RSS 订阅（橘鸦 AI 日报）\n"
            f"⏰ 首次检查时间：每天 {hour:02d}:{minute:02d}\n"
            f"🔄 轮询间隔：{poll_interval} 秒\n"
            f"🤖 AI 总结：{ai_enabled}\n"
            f"🔧 总结供应商：{provider_info}\n"
            f"📋 指令订阅数：{cmd_sub_count}\n"
            f"📋 配置群聊数：{cfg_group_count}\n"
            f"📋 配置私聊数：{cfg_user_count}\n"
            f"📚 已推送日期缓存：{len(self._sent_dates)} 天\n"
            f"📚 已推送文章缓存：{len(self._sent_links)} 篇"
        )
        yield event.plain_result(status_text)

    @filter.command("ainews_providers")
    async def cmd_list_providers(self, event: AstrMessageEvent):
        """列出所有可用的 LLM 供应商"""
        try:
            providers = self.context.get_all_providers()
            if not providers:
                yield event.plain_result("😞 当前没有配置任何 LLM 供应商，请先在 AstrBot 管理面板中添加。")
                return

            # 获取当前配置的供应商 ID
            current_id = self.config.get("summary_provider_id", "").strip()
            default_provider = self.context.get_using_provider()

            lines = ["🔧 **可用的 LLM 供应商列表**\n"]
            for p in providers:
                try:
                    meta = p.meta()
                    pid = meta.id if hasattr(meta, 'id') else str(id(p))
                    model = p.get_model() if hasattr(p, 'get_model') else '未知'
                    ptype = meta.type if hasattr(meta, 'type') else '未知'

                    # 标记当前选中的和默认的
                    markers = []
                    if current_id and pid == current_id:
                        markers.append("✅ 当前选中")
                    if default_provider and p == default_provider:
                        markers.append("⭐ 默认")
                    marker_str = f" ({', '.join(markers)})" if markers else ""

                    lines.append(
                        f"• ID: `{pid}`{marker_str}\n"
                        f"  模型: {model}\n"
                        f"  类型: {ptype}"
                    )
                except Exception:
                    lines.append(f"• (无法获取供应商信息)")

            lines.append(f"\n💡 使用方法：")
            lines.append(f"  发送 /ainews_setprovider <ID> 快速切换")
            lines.append(f"  或在插件配置中设置 summary_provider_id")

            yield event.plain_result("\n".join(lines))

        except Exception as e:
            logger.error(f"列出供应商失败: {e}")
            yield event.plain_result(f"❌ 获取供应商列表失败: {e}")

    @filter.command("ainews_setprovider")
    async def cmd_set_provider(self, event: AstrMessageEvent):
        """设置 AI 总结使用的供应商 ID"""
        # 获取指令参数（event.message_str 包含完整指令文本，需去掉指令名）
        raw = event.message_str.strip()
        # 去掉开头的指令名称部分
        if raw.lower().startswith("ainews_setprovider"):
            msg_str = raw[len("ainews_setprovider"):].strip()
        else:
            msg_str = raw
        if not msg_str:
            current_id = self.config.get("summary_provider_id", "").strip()
            if current_id:
                yield event.plain_result(
                    f"🔧 当前总结供应商 ID: `{current_id}`\n\n"
                    f"用法：\n"
                    f"  /ainews_setprovider <供应商ID> - 切换供应商\n"
                    f"  /ainews_setprovider default - 恢复使用默认供应商\n"
                    f"  /ainews_providers - 查看所有可用供应商"
                )
            else:
                yield event.plain_result(
                    f"🔧 当前使用默认供应商进行 AI 总结\n\n"
                    f"用法：\n"
                    f"  /ainews_setprovider <供应商ID> - 切换供应商\n"
                    f"  /ainews_providers - 查看所有可用供应商"
                )
            return

        provider_id = msg_str.strip()

        # 恢复默认
        if provider_id.lower() == "default":
            self.config["summary_provider_id"] = ""
            yield event.plain_result("✅ 已恢复使用默认供应商进行 AI 总结。")
            return

        # 验证供应商 ID 是否有效
        try:
            provider = self.context.get_provider_by_id(provider_id)
            if provider is None:
                yield event.plain_result(
                    f"❌ 未找到 ID 为 `{provider_id}` 的供应商。\n"
                    f"请使用 /ainews_providers 查看所有可用的供应商 ID。"
                )
                return

            self.config["summary_provider_id"] = provider_id

            model = provider.get_model() if hasattr(provider, 'get_model') else '未知'
            yield event.plain_result(
                f"✅ AI 总结供应商已切换为:\n"
                f"  ID: `{provider_id}`\n"
                f"  模型: {model}\n\n"
                f"💡 发送 /ainews_setprovider default 可恢复默认供应商"
            )
        except Exception as e:
            logger.error(f"设置供应商失败: {e}")
            yield event.plain_result(f"❌ 设置供应商失败: {e}")

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
        # 检查是否开启 AI 总结
        if not self.config.get("enable_ai_summary", True):
            logger.info("未开启 AI 总结，直接使用原文摘要")
            return self._format_fallback(article, article_date)

        # 检查缓存
        cache = await self._read_summary_cache()
        cached = cache.get(article_date)
        if cached:
            logger.info(f"使用缓存的 AI 总结 ({article_date})")
            return self._format_summary(
                cached["title"], cached["url"], cached["summary"], article_date
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
            }
            await self._save_summary_cache(cache)
            return self._format_summary(
                article["title"], article["link"], summary, article_date
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
            encoded_content = item.findtext(
                "{http://purl.org/rss/1.0/modules/content/}encoded", ""
            ).strip()
            pub_date = item.findtext("pubDate", "").strip()

            if not title:
                logger.warning("RSS 文章标题为空")
                return None

            # 优先使用 content:encoded 完整正文，缺失时回退到 description 摘要
            content = self._clean_html(encoded_content or description)

            logger.info(f"RSS 获取到最新文章：{title}")
            return {
                "title": title,
                "link": link,
                "content": content,
                "pub_date": pub_date,
            }

        except ET.ParseError as e:
            logger.error(f"RSS XML 解析失败: {e}")
        except Exception as e:
            logger.error(f"RSS 获取失败: {type(e).__name__}: {e!r}")

        return None

    def _parse_article_date(self, article: Dict) -> str:
        """从文章中解析日期，优先使用标题日期，回退 pubDate，最后使用当天日期。"""
        # 优先：从标题提取 YYYY-MM-DD（橘鸦 AI 日报标题即日期）
        title = article.get("title", "")
        match = re.search(r"(\d{4}-\d{2}-\d{2})", title)
        if match:
            return match.group(1)

        # 回退：解析 pubDate（RFC 2822 格式）
        pub_date = article.get("pub_date", "")
        if pub_date:
            try:
                dt = parsedate_to_datetime(pub_date)
                return dt.strftime("%Y-%m-%d")
            except Exception:
                pass

        # 最后回退：当天日期
        return datetime.now().strftime("%Y-%m-%d")

    # ==================== AI 总结 ====================

    def _get_summary_provider(self):
        """获取用于 AI 总结的 LLM 供应商。
        
        优先使用配置中指定的供应商 ID，如果未指定或获取失败则回退到默认供应商。
        """
        provider_id = self.config.get("summary_provider_id", "").strip()

        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
                if provider is not None:
                    model = provider.get_model() if hasattr(provider, 'get_model') else '未知'
                    logger.info(f"使用指定的供应商进行 AI 总结 (ID: {provider_id}, 模型: {model})")
                    return provider
                else:
                    logger.warning(
                        f"配置的供应商 ID '{provider_id}' 未找到，将回退到默认供应商。"
                        f"请使用 /ainews_providers 查看可用的供应商。"
                    )
            except Exception as e:
                logger.warning(f"获取指定供应商 '{provider_id}' 失败: {e}，将回退到默认供应商")

        # 回退到默认供应商
        provider = self.context.get_using_provider()
        if provider is not None:
            model = provider.get_model() if hasattr(provider, 'get_model') else '未知'
            logger.info(f"使用默认供应商进行 AI 总结 (模型: {model})")
        return provider

    async def _summarize_with_ai(self, content: str) -> Optional[str]:
        """使用 AstrBot 内置 LLM 对内容进行总结。
        
        支持通过 summary_provider_id 配置指定供应商，
        未指定或指定的供应商不可用时自动回退到默认供应商。
        """
        if not content or len(content.strip()) < 50:
            logger.warning("文章内容过短，跳过 AI 总结")
            return None

        try:
            # 内容过长时截断，避免超过模型上下文限制
            max_len = 8000
            if len(content) > max_len:
                content = content[:max_len] + "\n...(内容过长已截断)"

            prompt = SUMMARY_PROMPT.format(content=content)

            # 获取供应商（支持手动选择）
            provider = self._get_summary_provider()
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
            # 如果使用了指定供应商失败，提示用户可以尝试其他供应商
            provider_id = self.config.get("summary_provider_id", "").strip()
            if provider_id:
                logger.info(
                    f"提示：当前使用供应商 '{provider_id}' 总结失败，"
                    f"可尝试 /ainews_setprovider default 切换回默认供应商，"
                    f"或 /ainews_providers 查看其他可用供应商。"
                )
            return None

    # ==================== 格式化输出 ====================

    def _format_summary(
        self, title: str, url: str, summary: str, article_date: str
    ) -> str:
        """格式化 AI 总结后的推送文本。"""
        return (
            f"📰 AI 早报速递 | {article_date}\n"
            f"{'=' * 28}\n\n"
            f"📌 原文：{title}\n\n"
            f"🤖 AI 总结：\n\n"
            f"{summary}\n\n"
            f"{'=' * 28}\n"
            f"🔗 原文链接：{url}\n"
            f"💡 发送 /ainews 随时获取最新资讯"
        )

    def _format_fallback(self, article: Dict, article_date: str) -> str:
        """当 AI 总结失败时，使用原文摘要。"""
        content = article.get("content", "")
        if len(content) > 500:
            content = content[:500] + "..."

        return (
            f"📰 AI 早报 | {article_date}\n"
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
        """从配置中获取手动填写的群聊列表（支持 {机器人名称}:{群聊ID} 格式或直接填纯数字）。"""
        groups_text = self.config.get("subscribed_groups", "")
        if not groups_text or not groups_text.strip():
            return []
        return [g.strip() for g in groups_text.strip().split("\n") if g.strip()]

    def _get_config_users(self) -> List[str]:
        """从配置中获取手动填写的私聊账号列表（支持 {机器人名称}:{私聊账号ID} 格式或直接填纯数字）。"""
        users_text = self.config.get("subscribed_users", "")
        if not users_text or not users_text.strip():
            return []
        return [u.strip() for u in users_text.strip().split("\n") if u.strip()]

    def _get_all_targets(self) -> Set[str]:
        """获取所有推送目标。"""
        targets = set(self._cmd_subscriptions)

        cfg_groups = self._get_config_groups()
        for group_id in cfg_groups:
            parts = group_id.split(":")
            if len(parts) == 2:
                umo = f"{parts[0]}:GroupMessage:{parts[1]}"
                targets.add(umo)
            elif len(parts) == 3:
                targets.add(group_id)
            else:
                umo = f"default:GroupMessage:{group_id}"
                targets.add(umo)

        cfg_users = self._get_config_users()
        for user_id in cfg_users:
            parts = user_id.split(":")
            if len(parts) == 2:
                umo = f"{parts[0]}:FriendMessage:{parts[1]}"
                targets.add(umo)
            elif len(parts) == 3:
                targets.add(user_id)
            else:
                umo = f"default:FriendMessage:{user_id}"
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
