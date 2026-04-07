# astrbot_plugin_daily_ai_news

每日 AI 资讯自动推送插件 - 为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 开发

通过 RSS 订阅 [橘鸦 AI 日报](https://imjuya.github.io/juya-ai-daily/) 获取最新 AI 早报，并支持配置多个镜像 RSS 源以规避网络问题。默认优先使用多个 RSSHub 公共实例，国内环境下通常更容易命中可用源，经 **AI 总结** 后自动推送到 QQ 群 / 私聊。

## ✨ 功能

- 📰 **每日自动推送**：每天定时（默认早 8:00）自动推送最新 AI 资讯
- 📡 **镜像源故障切换**：支持配置多个 RSS 镜像源，前一个失败时自动切换下一个
- 🤖 **AI 智能总结**：调用 AstrBot 内置 LLM，将长篇早报精炼为 5-8 条关键要点
- 🔄 **手动获取**：发送 `/ainews` 随时获取最新 AI 资讯
- 📋 **灵活订阅**：支持配置文件填写群号/QQ号 + 群内指令订阅两种方式
- ⏳ **智能轮询**：若 RSS 尚未更新当日内容，插件会自动间隔轮询直到获取到当日文章
- 💾 **缓存去重**：AI 总结结果会缓存到本地，避免重复调用 LLM；已推送文章自动去重

## 📝 指令列表

| 指令             | 说明                              |
| ---------------- | --------------------------------- |
| `/ainews`        | 立即获取最新 AI 资讯（AI 总结版） |
| `/ainews_sub`    | 订阅每日推送（群聊/私聊均可使用） |
| `/ainews_unsub`  | 取消每日推送订阅                  |
| `/ainews_status` | 查看推送状态与订阅信息            |

## ⚙️ 配置说明

安装插件后，在 AstrBot 管理面板中可配置以下选项：

| 配置项                             | 说明                                                              | 默认值 |
| ---------------------------------- | ----------------------------------------------------------------- | ------ |
| `push_hour` 每日推送时间（小时）   | 0-23，设定首次检查 RSS 的小时                                     | 8      |
| `push_minute` 每日推送时间（分钟） | 0-59，设定首次检查 RSS 的分钟                                     | 0      |
| `rss_poll_interval` RSS 轮询间隔   | 当 RSS 尚未更新当日内容时，每隔多少秒重新检查一次（默认 10 分钟） | 600    |
| `rss_sources` RSS 镜像源列表       | 每行一个镜像地址，优先尝试用户填写的源，失败后继续使用内置默认源 | 空     |
| `subscribed_groups` 订阅群号       | 手动填写需要推送的 QQ 群号，每行一个                              | 空     |
| `subscribed_users` 订阅 QQ 号      | 手动填写需要私聊推送的 QQ 号，每行一个                            | 空     |

### RSS 镜像源配置示例

```text
https://rsshub.rssforever.com/github/issue/ImJuYa/juya-ai-daily
https://rsshub.feeded.xyz/github/issue/ImJuYa/juya-ai-daily
https://rsshub.app/github/issue/ImJuYa/juya-ai-daily
https://rsshub.moeyy.cn/github/issue/ImJuYa/juya-ai-daily
https://rsshub.pseudoyu.com/github/issue/ImJuYa/juya-ai-daily
https://imjuya.github.io/juya-ai-daily/rss.xml
```

- 每行填写一个同内容的 RSS 镜像地址
- 插件会先按你填写的顺序依次请求，再继续尝试内置默认源
- 当前一个地址超时、连接失败、返回非 200 或 XML 异常时，会自动切换到下一个地址
- 默认已内置多个 RSSHub 公共实例，适合国内网络环境下做故障切换
- 如部分公共实例不可用，可在配置中删除失效地址，或替换成你自己的自建 / 常用镜像

## 📦 安装

1. 在 AstrBot 管理面板中搜索 `astrbot_plugin_daily_ai_news` 安装
2. 或手动将本仓库克隆到 `addons/plugins/` 目录下
3. 重启 AstrBot 即可生效

## 📌 注意事项

- **需要配置 LLM**：AI 总结功能依赖 AstrBot 中已配置的 LLM Provider，请确保至少启用了一个 LLM 服务
- 若 AI 总结失败，插件会自动回退使用原文摘要进行推送
- 插件在重启后会自动补偿检查：若已过推送时间且当天未推送，会立即尝试拉取并推送
- 若主 RSS 地址访问失败，插件会自动按顺序切换到后续镜像源继续尝试
- 配置群号方式需要填写 QQ 群号码（纯数字），指令方式需在群内发送 `/ainews_sub`
- 两种订阅方式（配置文件 + 指令）可同时使用，插件会自动合并去重
- AI 总结缓存最多保留最近 10 天的记录

## 📝 更新日志

- ✨ 新增了多平台的支持。
- ⚙️ 新增了开启/关闭 AI 总结的选项支持。

## 📄 License

[GPL-3.0](LICENSE)
