# qq-botpy-sdk

基于 QQ 机器人开放平台的异步 Python SDK 与机器人开发框架。

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](./LICENSE)
[![PyPI](https://img.shields.io/pypi/v/qq-botpy-sdk)](https://pypi.org/project/qq-botpy-sdk/)
[![Repository](https://img.shields.io/badge/GitHub-Teahouse--Studios%2Fqq--botpy--sdk-181717?logo=github)](https://github.com/Teahouse-Studios/qq-botpy-sdk)
[![Status](https://img.shields.io/badge/status-community%20maintained-orange)](#项目状态)

## 项目状态

本仓库最初 fork 自腾讯维护的
[tencent-connect/botpy](https://github.com/tencent-connect/botpy)，现已转为由 Teahouse Studios 独立维护的
社区项目。PyPI 发布包名为 `qq-botpy-sdk`，Python 导入名继续保持为 `botpy`。

请特别注意：

- 本项目不是腾讯官方 SDK，也不代表腾讯或 QQ 机器人开放平台。
- 问题反馈、功能请求和代码贡献请提交到
  [Teahouse-Studios/qq-botpy-sdk](https://github.com/Teahouse-Studios/qq-botpy-sdk)，不要提交给历史上游。
- 独立维护期间会继续整理版本策略和公开 API，升级前请阅读 [迁移指南](./MIGRATION.md)。
- QQ、QQ 机器人及相关名称和商标归其权利人所有。

## 为什么维护这个分支

维护这个项目的直接原因是历史上游已经长期缺乏维护，同时原框架在连接稳定性、协议完整性、资源生命周期、
错误处理和现代群聊/C2C 场景中存在一系列问题，无法仅靠零散补丁解决。因此，本项目选择独立维护并对协议层、
高层 API 和工程结构进行系统改造，重点解决：

- Gateway 心跳 ACK、Resume、关闭码分类和稳定重连。
- 长时间运行时的 Token 生命周期和网络资源释放。
- C2C、群聊、频道、频道私信的统一事件和消息发送模型。
- WebSocket 与 Webhook 双事件传输。
- 中间件、持久化、媒体分片上传、流式消息等生产能力。
- 结构化错误、非幂等请求安全和可测试的协议组件。

## 主要能力

### Gateway 与传输

- 按服务端 `heartbeat_interval` 发送心跳并携带最新序列号。
- 检测 Opcode 11 Heartbeat ACK，超时后主动恢复连接。
- 支持 Identify、Resume、服务端重连指令和 Invalid Session。
- 按关闭码选择刷新 Token、清理 Session、退避重连或停止。
- 支持 JSON 文件或自定义 Store 持久化 Gateway Session。
- 支持 WebSocket、内置 Webhook 服务和自定义事件传输适配器。
- Webhook 支持回调地址验证、Ed25519 验签和事件 ACK。

### HTTP 与鉴权

- 并发安全的 access token 缓存、提前刷新和后台刷新。
- 401 强制刷新一次 Token 后重试。
- 结构化 API、认证、限流和传输异常。
- 解析 `Retry-After`，安全方法支持指数退避。
- POST/PATCH 默认不自动重试，避免非幂等消息重复发送。
- 可配置 API 地址、Token 地址、User-Agent 和 SSLContext/私有 CA。
- 通过 `client.api.request/get/post/put/patch/delete()` 调用尚未封装的 REST API。

### 消息、媒体与 Interaction

- 统一的 `RawEvent`、`InboundMessage`、`ReplyTarget` 和 `InteractionContext`。
- 统一发送文本、Markdown、Ark、Embed、Keyboard 和媒体消息。
- 长文本超过 5000 字符时自动切分。
- 被动回复次数或时间窗口超限后自动转主动消息。
- 支持 Typing、Wakeup、消息撤回和出站 `ref_idx` hook。
- 支持图片、视频、语音、普通文件的 URL、Base64、bytes 和本地文件来源。
- 5 MiB 以上自动切换分片上传，支持哈希、并发、进度回调和业务错误重试。
- 按内容、目标和媒体类型缓存服务端 `file_info`。
- 支持 C2C replace-mode 流式消息。
- 支持群信息、入群申请审批、成员禁言与自动审批策略管理。
- 支持声明式全局菜单和多指令面板，并在启动时同步平台配置。
- 提供高层 Interaction ACK。

### 中间件与存储

- 消息过滤、内容清洗、访问策略、限流、并发保护和 Mention Gate。
- Slash Command、Typing Indicator、错误处理和恢复中间件。
- History Buffer、Quote Ref 和 LLM Envelope。
- 通用异步 KV Store，以及 Session、History、Ref Index 适配器。

### 工程集成

- 保留历史 botpy 事件回调，便于渐进迁移。
- 支持标准库 logging 和 Loguru。
- 支持自定义 Webhook Server、Session Store、KV Store 和中间件。
- 核心协议组件可独立测试和使用。

## 环境要求

- Python 3.10 或更高版本。
- QQ 机器人开放平台提供的 AppID 和 AppSecret。
- 使用 WebSocket 时，运行环境需要能够访问 QQ Gateway 和 REST API。
- 使用 Webhook 时，需要平台可访问的公网 HTTPS 回调地址。

## 安装

### 从 PyPI 安装

```bash
pip install --upgrade qq-botpy-sdk
```

安装包名是 `qq-botpy-sdk`，Python 导入名仍然是：

```python
import botpy
```

### 从当前仓库安装

需要使用尚未发布的最新代码时，可以直接从仓库安装：

```bash
pip install "git+https://github.com/Teahouse-Studios/qq-botpy-sdk.git"
```

Poetry 项目可以使用：

```bash
poetry add "git+https://github.com/Teahouse-Studios/qq-botpy-sdk.git"
```

> `qq-botpy-sdk` 是当前独立维护版的发布包名。历史 `qq-botpy` 包属于不同的发布来源，不代表本仓库。

### 本地开发安装

```bash
git clone https://github.com/Teahouse-Studios/qq-botpy-sdk.git
cd qq-botpy-sdk
poetry install
```

## 快速开始

下面的示例使用统一消息回调，可同时处理标准化后的 C2C、群聊、频道和频道私信消息：

```python
import os

import botpy


class MyClient(botpy.Client):
    async def on_message_context(self, context):
        message = context.message
        print(message.event_type, message.author_id, message.content)
        await self.send_text(
            message.reply_target,
            f"收到：{message.content}",
        )

    async def on_ready(self):
        print(f"机器人 {self.robot.name} 已连接")


intents = botpy.Intents(
    public_messages=True,
    public_guild_messages=True,
    direct_message=True,
    interaction=True,
)

client = MyClient(intents=intents)
client.run(
    appid=os.environ["QQBOT_APP_ID"],
    secret=os.environ["QQBOT_APP_SECRET"],
)
```

历史事件回调仍然可用，例如 `on_at_message_create`、`on_c2c_message_create`、
`on_group_at_message_create` 和 `on_interaction_create`。新项目建议优先使用统一消息与原始事件回调，
旧项目可以渐进迁移。

## 常用配置

### 持久化 Gateway Session

```python
from botpy.protocol import JsonFileSessionStore

client = MyClient(
    intents=intents,
    session_store=JsonFileSessionStore("./.botpy-sessions"),
)
```

进程重启后，客户端会优先尝试 Resume，并从保存的序列号之后补发事件。失效、过期或分片数量不匹配的
Session 会被自动清理。

### Webhook 模式

```python
client = MyClient(
    intents=intents,
    transport="webhook",
    webhook_host="0.0.0.0",
    webhook_port=8080,
    webhook_path="/callback",
)
```

生产环境建议在 SDK 前使用反向代理终止 TLS，并将平台回调地址配置为公网 HTTPS URL。

### 中间件

```python
from botpy.middleware import (
    RateLimitTier,
    ScopePolicy,
    access_policy,
    concurrency_guard,
    content_sanitizer,
    rate_limiter,
)

client.use(
    content_sanitizer(collapse_whitespace=True, parse_face_tags=True),
    access_policy(group=ScopePolicy(mode="allowlist", allow=("group-openid",))),
    rate_limiter(per_sender=RateLimitTier(max_requests=5, window_seconds=60)),
    concurrency_guard(strategy="queue", max_queue=3),
)
```

### 发送 Markdown 和媒体

```python
await client.send_markdown(
    context.reply_target,
    "# Markdown 消息",
)

result = await client.send_image(
    context.reply_target,
    local_path="./image.png",
    content="图片说明",
)

print(result.upload["file_info"], result.message["id"])
```

### C2C 流式消息

```python
stream = client.open_stream(context.reply_target, throttle_ms=500)
full_text = ""

try:
    async for token in model_stream():
        full_text += token
        await stream.update(full_text)
    await stream.complete()
except Exception:
    stream.cancel()
    raise
```

流式消息只支持携带入站 `message_id` 的 C2C 目标。

### 调用未封装的 REST API

```python
guilds = await client.api.get(
    "/users/@me/guilds",
    params={"limit": 100},
)

token = await client.api.get_token()
```

### 自定义菜单与指令面板

```python
from botpy.configuration import Menu, Panel

client = MyClient(
    intents=intents,
    menu=Menu(
        items=[
            Menu.message("帮助", "/help"),
            Menu.link("官网", "https://example.com"),
        ]
    ),
    panels=[
        Panel(
            "main",
            scope="c2c",
            items=[Panel.command("查询天气", desc="查询当前天气")],
        )
    ],
    config_sync_strict=False,
)
```

SDK 在客户端启动后同步声明的单个全局菜单和多个受管面板。多副本部署、strict 模式和非破坏性同步边界见
[自定义菜单与指令面板](./docs/MENU_PANEL.md)。

### 使用 Loguru

```python
import sys

import botpy
from loguru import logger

logger.remove()
logger.configure(extra={"stdlib_logger": "app"})
logger.add(
    sys.stderr,
    enqueue=True,
    format="{time} | {level} | {extra[stdlib_logger]} | {message}",
)

client = MyClient(
    intents=intents,
    loguru_logger=logger,
)
```

完整说明见 [Loguru 配置指南](./docs/LOGURU.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [API 参考](./docs/API.md) | 新高层接口、统一发送、媒体、REST 和 Interaction |
| [自定义菜单与指令面板](./docs/MENU_PANEL.md) | 声明式 Menu/Panel、启动同步和多副本部署风险 |
| [群管理 API 与事件](./docs/GROUP_MANAGEMENT.md) | 群信息、入群审批、成员禁言、自动审批策略和群成员事件 |
| [迁移指南](./MIGRATION.md) | 协议层改造后的行为变化与不兼容项 |
| [Loguru 配置指南](./docs/LOGURU.md) | 日志桥接、轮转、结构化字段和根 logger 接管 |
| [发布指南](./docs/RELEASING.md) | GitHub Release、版本校验和 PyPI Trusted Publishing |
| [事件监听](./docs/事件监听.md) | 历史事件回调列表 |
| [示例目录](./examples/README.md) | 频道、群聊、C2C、媒体和管理事件示例 |

## 示例

[`examples`](./examples/) 目录保留了历史 API 示例，并增加了现代协议层组合示例：

- `demo_modern_client.py`：统一消息、Session、Interaction、缓存和限流。
- `demo_group_reply_text.py` / `demo_group_reply_file.py`：群聊消息与媒体。
- `demo_c2c_reply_text.py` / `demo_c2c_reply_file.py`：C2C 消息与媒体。
- `demo_at_reply.py`：频道 AT 消息。
- `demo_dms_reply.py`：频道私信。
- `demo_recall.py`：消息撤回。

部分历史示例直接读取 `examples/config.yaml`。不要提交包含真实 AppSecret 的配置文件。

## 开发与测试

安装依赖：

```bash
poetry install
```

运行纯本地测试：

```bash
poetry run python -m unittest discover -s tests -p "test_[!a]*.py"
```

`tests/test_api.py` 使用真实平台凭证，并包含创建、修改或删除线上资源的 API 测试。除非你明确准备了隔离的
测试机器人和资源，否则不要运行该文件。

编译和项目元数据检查：

```bash
poetry run python -m compileall -q botpy examples
poetry check
```

## 当前限制

- Python 版没有捆绑 Node 参考实现中的 `silk-wasm` 或 MP3 WASM 解码器；提供原生格式直传、
  PCM/WAV 工具和可选系统 FFmpeg 支持。
- 内置 JSON Session/KV Store 面向单进程轻量部署；多进程部署应接入 Redis、SQL 或其他共享存储。
- QQ 开放平台仍可能增加事件和字段；未知事件可通过 `on_raw_event` 获取，未知发送字段可通过
  `Client.send(..., extra={...})` 透传。

## 来源与许可证

本项目派生自 [tencent-connect/botpy](https://github.com/tencent-connect/botpy)。感谢原项目维护者和历史
贡献者提供的基础实现。

本仓库继续采用 [MIT License](./LICENSE)。原始版权声明保留在许可证文件和相关源文件中。
