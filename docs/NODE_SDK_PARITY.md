# Node SDK 能力对照

参考目录：`qqbot-nodejs-main/`。本表聚焦 SDK 核心协议和通用高层能力，不把 Node 运行时专属实现机械复制到 Python。

| 能力 | Python 状态 | 说明 |
| --- | --- | --- |
| access token 缓存、并发合并、提前刷新 | 已补齐 | 首次同步获取，后台刷新，401 强刷一次 |
| 任意 REST API gateway | 已补齐 | `client.api.get/post/put/patch/delete/request/get_token` |
| Gateway 心跳 ACK、Resume、关闭码、退避 | 已补齐 | 支持 Session 持久化和致命错误停止 |
| WebSocket / Webhook 统一事件 | 已补齐 | 包含 RawEvent、InboundMessage、InteractionContext |
| 中间件、历史、引用、LLM envelope、typing | 已补齐 | Python 中间件实现与 Node 语义对齐 |
| 统一消息、Markdown、Keyboard、Wakeup、撤回 | 已补齐 | 支持 C2C、群、频道和频道私信 |
| 被动回复限制及主动回退 | 已补齐 | 默认每条入站消息每小时 4 次 |
| 5000 字符自动切分 | 已补齐 | `send_text()` 和 `chunk_text()` |
| 媒体上传、5 MiB 自动分片、100 MiB 上限 | 已补齐 | 含哈希、并发、进度、业务码重试 |
| `file_info` TTL 缓存 | 已补齐 | MD5 + scope + target + file type |
| C2C 流式消息 | 已补齐 | replace mode、节流、完成/取消和限流重试 |
| 出站 `ref_idx` hook | 已补齐 | `on_message_sent` / `set_message_sent_hook()` |
| base URL、User-Agent、自定义 CA/SSLContext | 已补齐 | Client 高层配置 |
| Data URL、MIME、媒体类型和目标解析 | 已补齐 | `botpy.protocol` 纯函数工具 |
| 图片尺寸和 QQ Markdown 图片 | 已补齐 | PNG/JPEG/GIF/WebP 头解析 |
| PCM/WAV 和 FFmpeg PCM 转换 | 已补齐 | FFmpeg 为系统可选依赖，不随包安装 |

## 仍保留的实现差异

- Node 版可选加载 `silk-wasm`、`mpg123-decoder`。Python 版没有强制引入对应 WASM 包，只提供
  原生 WAV/MP3/SILK 直传、PCM/WAV 工具和系统 FFmpeg 转 PCM。需要 SILK 编码时应由应用注入自己的
  编码器，避免 SDK 强绑定体积大、平台相关的二进制依赖。
- Node 的 `media-tags`、`voice-text`、`text-parsing` 和部分 payload 工具面向其上层 Agent/LLM
  集成。Python 已通过统一附件模型、History/Quote 中间件和 `extra` payload 透传覆盖核心用途，未复制
  Node 特定的标签文本协议。
- 图片 URL 尺寸探测没有内置联网 fetcher，避免 SDK 默认产生 SSRF 风险。调用方可先执行自己的
  域名/IP 安全策略并下载头部字节，再调用 `parse_image_size()`。

上述差异不影响 QQ 开放平台的 Gateway、REST、消息、媒体、Interaction、Webhook 和流式消息协议能力。
