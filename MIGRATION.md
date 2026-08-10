# 迁移指南

本文记录协议层、连接管理和高层发送接口改造后需要关注的行为变化。

## 运行环境与依赖

- 独立维护版发布包名为 `qq-botpy-sdk`，Python 导入名仍为 `botpy`。
- 最低 Python 版本为 3.10；这是最新 `aiohttp 3.14.x` 的最低版本要求。
- `cryptography` 现在是 Webhook Ed25519 签名校验所需的正式运行时依赖。
- Poetry、`setup.py` 和 `requirements.txt` 使用同一组依赖范围。

## Gateway 与生命周期

- TLS 证书校验默认开启，不再接受无提示的无证书校验连接。私有 CA 请通过
  `Client(ssl=ssl_context)` 显式配置。
- Gateway 会依据关闭码选择 Resume、Identify、刷新 token、退避重连或停止；致命关闭码不会无限重试。
- 心跳必须收到 Opcode 11 ACK。超过一个心跳周期未确认时会主动断开并恢复连接。
- `Client.close()` 会关闭事件传输、流式会话、WebSocket、Session Store、Token 和 HTTP Session。

## HTTP 请求

- HTTP 错误现在携带状态码、平台错误码、trace id、请求方法、URL、响应体和 `Retry-After`。
- 默认只自动重试 GET、HEAD、OPTIONS、PUT 和 DELETE。POST/PATCH 不会自动重试，避免消息等
  非幂等接口在网络抖动时重复执行。
- 已确认可安全重复的分片上传 prepare/finish/complete 流程会显式开启 POST 重试。
- 401 会强制刷新一次 access token 后重试一次原请求。
- 登录成功后会启动后台 token 提前刷新循环，长时间没有 HTTP 流量时也能保证后续 Gateway 重连使用新 token。
- 消息和媒体 payload 会过滤值为 `None` 的字段。

## 消息发送

- `Client.send_text()` 超过 5000 字符时会自动切分；单段仍返回单个响应，多段返回响应列表。
- `Client(markdown_support=True)` 会让 `send_text()` 使用 Markdown 消息；未获平台权限时保持默认值 `False`。
- C2C/群聊对同一入站 `msg_id` 默认最多发送 4 次被动回复。超过一小时或次数上限后，SDK 会移除
  `msg_id`、`event_id` 和 `msg_seq`，自动转为主动消息。
- `Client.send()` 仍是显式低层入口；通过 `extra` 可透传平台新增字段。

## 媒体上传

- 空文件、超出媒体类型限制的文件、符号链接和非法文件名会在请求前被拒绝。
- bytes 和本地文件达到 5 MiB 时自动使用分片协议。
- 相同内容只会在相同 scope、target 和 file type 下复用 `file_info`，并在服务端 TTL 前 60 秒失效。
- 缓存仅适用于 SDK 能计算内容摘要的 bytes、base64 和本地文件；URL 上传不会缓存。

## 新增入口

- `client.api.get/post/put/patch/delete/request()`：调用尚未封装的 REST API。
- `await client.api.get_token()`：获取当前有效 access token。
- `await client.acknowledge_interaction(...)` 和 `Interaction.acknowledge(...)`：高层 Interaction ACK。
- `on_interaction_context(context)`：收到包含 `client`、原始事件、`state` 和接收时间的统一上下文。
- `UploadCache`、`ReplyLimiter`、`chunk_text()`：可单独导入和替换默认实现。
- 新增 Data URL/MIME/媒体类型、图片尺寸与 Markdown、目标解析、音频/FFmpeg 和格式化工具。
- `on_message_sent` / `set_message_sent_hook()` 可收集平台返回的出站 `ref_idx`。

完整签名和示例见 [docs/API.md](./docs/API.md)。
