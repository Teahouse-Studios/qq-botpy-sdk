# 迁移指南

本文记录协议层、连接管理和高层发送接口改造后需要关注的行为变化。

## 运行环境与依赖

- 独立维护版发布包名为 `qq-botpy-sdk`，Python 导入名仍为 `botpy`。
- 最低 Python 版本仍为 3.10；HTTP 客户端由 `httpx` 提供，Gateway WebSocket 由 `httpx-ws` 提供。
- `cryptography` 现在是 Webhook Ed25519 签名校验所需的正式运行时依赖。
- uv 通过 `pyproject.toml` 管理依赖；首次执行 `uv sync` 会生成用于复现开发环境的 `uv.lock`。
- Webhook 默认使用无额外框架依赖的 `AsyncioWebhookServer`；旧的 `AiohttpWebhookServer` 名称仍作为兼容别名保留。

## Gateway 与生命周期

- TLS 证书校验默认开启，不再接受无提示的无证书校验连接。私有 CA 请通过
  `Client(ssl=ssl_context)` 显式配置。
- Gateway 会依据关闭码选择 Resume、Identify、刷新 token、退避重连或停止；致命关闭码不会无限重试。
- 心跳必须收到 Opcode 11 ACK。超过一个心跳周期未确认时会主动断开并恢复连接。
- WebSocket 模式下，消息发送会在连接或重连期间等待所有分片 READY/RESUMED。
  `gateway_send_timeout` 默认为 30 秒，`None` 表示一直等待；首次请求前超时时 HTTP 请求尚未发出。
- `Client.close()` 会关闭事件传输、流式会话、WebSocket、Session Store、Token 和 HTTP Session。

## HTTP 请求

- HTTP 错误现在携带状态码、平台错误码、trace id、请求方法、URL、响应体和 `Retry-After`。
- 默认只自动重试 GET、HEAD、OPTIONS、PUT 和 DELETE。POST/PATCH 不会自动重试，避免消息等
  非幂等接口在网络抖动时重复执行。
- 常规 C2C/群聊消息中，仅携带 `msg_id` 的被动回复会在不确定的传输失败后重试一次；
  主动消息仍不重试。
- 已确认可安全重复的分片上传 prepare/finish/complete 流程会显式开启 POST 重试。
- 401 会强制刷新一次 access token 后重试一次原请求。
- `TransportError` 保留 `method`、`url`、原始 `cause` 和 `attempts`；`attempts=0` 表示请求未发出，
  重试前等待 Gateway 超时则保留此前的实际请求次数。
- 登录成功后会启动后台 token 提前刷新循环，长时间没有 HTTP 流量时也能保证后续 Gateway 重连使用新 token。
- `Client(proxy=...)` 可统一为 REST API、access token 和 Gateway WebSocket 配置 HTTP 代理；
  非法代理配置会在构造 `Client` 时立即抛出 `ValueError`。
- 消息和媒体 payload 会过滤值为 `None` 的字段。
- **默认启用出站请求整流**：`Client(rate_limit=...)` 默认为 `None`，等价于按官方文档核对后的
  接口频率限制排队限速。每个接口、Bot 维度、单关系维度各自维护令牌桶并按到达顺序排队，收到
  `429` 时按 `Retry-After` 让同通道请求一起退避。行为上可能出现此前不会发生的等待（最坏约
  60 秒），因此显式指定 `rate_limit=False` 可恢复“不做任何整流、直接发送”的旧行为；也可以传入
  `RateLimiter` 实例或参数字典自定义。每日额度（窗口大于 `max_pacing_window`，默认 1 小时）
  只提示不阻塞。旧的 `BotHttp(timeout=...)` 直接构造方式同样默认启用整流，可传
  `BotHttp(..., rate_limit=False)` 关闭。
- **主动消息配额默认按「已认证」档位**（单聊 10 QPS、群聊 60 QPM）。平台没有查询认证等级的
  接口，档位只能由使用者声明：已认证机器人用默认值即可，**未认证机器人必须显式降档**
  （`rate_limit={"certification": "unverified"}`，单聊 5 QPS + 30 QPM、群聊 30 QPM），
  否则会按超出自身配额的速率发出，触发 `40034100 主动消息发送超过频控限制`。SDK 会在构造
  整流器时打印当前档位并提醒未认证机器人降档。

## 消息发送

- `Client.send_text()` 超过 5000 字符时会自动切分；单段仍返回单个响应，多段返回响应列表。
- `Client(markdown_support=True)` 会让 `send_text()` 使用 Markdown 消息；未获平台权限时保持默认值 `False`。
- C2C/群聊对同一入站 `msg_id` 默认最多发送 4 次被动回复。超过一小时或次数上限后，SDK 会移除
  `msg_id`、`event_id` 和 `msg_seq`，自动转为主动消息。
- **消息发送改为按平台 `err_code` 决策**：`ApiError.from_response()` 现在优先读取 `err_code`
  （此前只读 `code`，导致 `ApiError.code` 常为 `None`）。`Client.send()` 会据此
  ① 在 `msg_id`/`event_id` 失效时回退为主动消息重发一次，② 在 `40054005` 去重时首次换
  `msg_seq` 重发一次，③ 在网络超时/连接中断以及文档标注「请重试」的错误码上按
  `3 × 2ⁿ` 秒指数退避重发（总时长上限 60 秒，超时抛 `MessageSendTimeoutError`），
  ④ 对其余错误码（含 `40034101`/`40054002`/`40054003`）立即抛错不再重试。
  此前这些错误会原样抛出且不做任何回退，因此失败率会下降、但发送耗时上限提高到 60 秒；
  可通过 `Client(send_policy=MessageSendPolicy(...))` 调整或注入自定义策略。
  详见 [docs/SEND_ERRORS.md](./docs/SEND_ERRORS.md)。
- `Client.send()` 仍是显式低层入口；通过 `extra` 可透传平台新增字段。

## 媒体上传

- 空文件、超出媒体类型限制的文件、符号链接和非法文件名会在请求前被拒绝。
- bytes、base64 和本地文件达到 5 MiB 时自动使用分片协议；`upload_media(..., force_chunked=True)`
  可对小文件强制分片，`url` 源不支持强制分片。
- `upload_media_url()` 强制分片上传并返回 `MediaUrlResult(upload, raw_url, ttl)`，用于 Markdown 等需要
  临时直链的场景；平台未返回 `raw_url` 时抛出 `RuntimeError`。
- 相同内容只会在相同 scope、target 和 file type 下复用上传结果，并在服务端 TTL 前 60 秒失效。
- 缓存现在保存完整响应字段（`file_info`、`file_uuid`、`ttl`、`raw_url`）。命中缓存时返回的 `ttl`
  是剩余有效秒数而不是 `0`；`UploadCache.get_response()` 是读取完整字段的入口，`get()` 仍只返回
  `file_info`。`upload_media_url()` 不会复用缺少 `raw_url` 的缓存条目。
- 缓存仅适用于 SDK 能计算内容摘要的 bytes、base64 和本地文件；URL 上传不会缓存。
- `upload_prepare` 响应中的 `block_size` 允许是字符串，`concurrency` 与 `retry_timeout` 允许位于
  `upload_config` 子对象，`parts[].index` 允许是 0-based 或 1-based；SDK 统一归一化为整数和
  1-based 索引。`upload_part_finish` 以实际接口行为为准发送 1-based 索引与 JSON 数字。
- `Media` 新增可选字段 `raw_url: NotRequired[str]`，表示随 `ttl` 过期的临时直链。

## 新增入口

- `client.api.get/post/put/patch/delete/request()`：调用尚未封装的 REST API。
- `await client.api.get_token()`：获取当前有效 access token。
- `await client.acknowledge_interaction(...)` 和 `Interaction.acknowledge(...)`：高层 Interaction ACK。
- `on_interaction_context(context)`：收到包含 `client`、原始事件、`state` 和接收时间的统一上下文。
- `UploadCache`、`ReplyLimiter`、`chunk_text()`：可单独导入和替换默认实现。
- 新增 Data URL/MIME/媒体类型、图片尺寸与 Markdown、目标解析、音频/FFmpeg 和格式化工具。
- `on_message_sent` / `set_message_sent_hook()` 可收集平台返回的出站 `ref_idx`。

完整签名和示例见 [docs/API.md](./docs/API.md)。
