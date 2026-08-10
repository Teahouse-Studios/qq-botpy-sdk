<div align="center">

![botpy](https://socialify.git.ci/tencent-connect/botpy/image?description=1&font=Source%20Code%20Pro&forks=1&issues=1&language=1&logo=https%3A%2F%2Fgithub.com%2Ftencent-connect%2Fbot-docs%2Fblob%2Fmain%2Fdocs%2F.vuepress%2Fpublic%2Ffavicon-64px.png%3Fraw%3Dtrue&owner=1&pattern=Circuit%20Board&pulls=1&stargazers=1&theme=Light)

[![Language](https://img.shields.io/badge/language-python-green.svg?style=plastic)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-orange.svg?style=plastic)](https://github.com/tencent-connect/botpy/blob/master/LICENSE)
![Python](https://img.shields.io/badge/python-3.10+-blue)
![PyPI](https://img.shields.io/pypi/v/qq-botpy)
[![BK Pipelines Status](https://api.bkdevops.qq.com/process/api/external/pipelines/projects/qq-guild-open/p-713959939bdc4adca0eea2d4420eef4b/badge?X-DEVOPS-PROJECT-ID=qq-guild-open)](https://devops.woa.com/process/api-html/user/builds/projects/qq-guild-open/pipelines/p-713959939bdc4adca0eea2d4420eef4b/latestFinished?X-DEVOPS-PROJECT-ID=qq-guild-open)

_✨ 基于 [机器人开放平台API](https://bot.q.qq.com/wiki/develop/api/) 实现的机器人框架 ✨_

_✨ 为开发者提供一个易使用、开发效率高的开发框架 ✨_

[文档](https://bot.q.qq.com/wiki/develop/pythonsdk/)
·
[下载](https://github.com/tencent-connect/botpy/tags)
·
[安装](https://bot.q.qq.com/wiki/develop/pythonsdk/#sdk-安装)

</div>

## 准备工作

### 安装

```bash
pip install qq-botpy
```

更新包时添加 `--upgrade`。当前支持 Python 3.10 及以上版本。

### 使用

需要使用的地方`import botpy`

```python
import botpy
```

### 兼容提示

> 原机器人的老版本`qq-bot`仍然可以使用，但新接口的支持上会逐渐暂停，此次升级不会影响线上使用的机器人 

本次协议层改造的行为变化见 [MIGRATION.md](./MIGRATION.md)，新增高层接口见
[API 参考](./docs/API.md)，可运行的组合示例见
[examples/demo_modern_client.py](./examples/demo_modern_client.py)。与参考 Node SDK 的能力对照见
[Node SDK 能力对照](./docs/NODE_SDK_PARITY.md)。

下游使用 Loguru 时，可直接传入 `Client(loguru_logger=logger)`；完整配置、轮转和结构化上下文示例见
[Loguru 配置指南](./docs/LOGURU.md)。

## 版本更新说明
### v1.1.5
1. 更新鉴权方式。 新版本通过AppID + AppSecret进行鉴权，需要使用者进行适配。AppSecret见[QQ机器人开发设置页](https://q.qq.com/qqbot/#/developer/developer-setting)中的AppSecret字段。具体适配方式见示例  [鉴权配置示例](./examples/config.example.yaml) [鉴权传参接口变更示例](./examples/demo_at_reply.py)
2. 增加群和好友内发消息能力。可参考[群内发消息示例](./examples/demo_group_reply_text.py) [好友内发消息示例](./examples/demo_c2c_reply_text.py)
3. 增加群和好友内发送富媒体消息能力，目前支持图片、视频、语音类型。可参考  [群内发富媒体消息示例](./examples/demo_group_reply_file.py)   [好友内发富媒体消息示例](./examples/demo_c2c_reply_file.py)

## 使用方式

### 快速入门

#### 步骤1

通过继承实现`bot.Client`, 实现自己的机器人Client 

#### 步骤2

实现机器人相关事件的处理方法,如 `on_at_message_create`， 详细的事件监听列表，请参考 [事件监听.md](./docs/事件监听.md)

如下，是定义机器人被@的后自动回复:

```python
import botpy
from botpy.message import Message

class MyClient(botpy.Client):
    async def on_at_message_create(self, message: Message):
        await message.reply(content=f"机器人{self.robot.name}收到你的@消息了: {message.content}")
```

``注意:每个事件会下发具体的数据对象，如`message`相关事件是`message.Message`的对象 (部分事件透传了后台数据，暂未实现对象缓存)``

#### 步骤3

设置机器人需要监听的事件通道，并启动`client`

```python
import botpy
from botpy.message import Message

class MyClient(botpy.Client):
    async def on_at_message_create(self, message: Message):
        await self.api.post_message(channel_id=message.channel_id, content="content")

intents = botpy.Intents(public_guild_messages=True) 
client = MyClient(intents=intents)
client.run(appid="12345", secret="xxxx")
```

### 连接恢复（可选）

默认情况下，Gateway 的 `session_id` 和最新序列号只保存在内存中。需要在进程重启后继续尝试 Resume 时，
可以配置 JSON Session Store：

```python
import botpy
from botpy.protocol import JsonFileSessionStore

intents = botpy.Intents(public_guild_messages=True)
session_store = JsonFileSessionStore("./.botpy-sessions")
client = MyClient(intents=intents, session_store=session_store)
client.run(appid="12345", secret="xxxx")
```

Session 默认保存 5 分钟，并按机器人 AppID 和分片隔离。失效、过期或分片数量不匹配的数据会自动丢弃。

### 使用 Webhook 接收事件

除了默认的 WebSocket Gateway，也可以让客户端启动 HTTP Webhook 服务。SDK 会完成回调地址验证、
Ed25519 请求验签和 `op: 12` ACK，并继续触发现有事件回调、`on_message` 与 `on_raw_event`：

```python
import botpy

intents = botpy.Intents(public_guild_messages=True)
client = MyClient(
    intents=intents,
    transport="webhook",
    webhook_host="0.0.0.0",
    webhook_port=8080,
    webhook_path="/callback",
)
client.run(appid="12345", secret="xxxx")
```

将平台回调地址配置为可访问该监听地址的公网 HTTPS URL，例如
`https://bot.example.com/callback`。生产环境建议在 SDK 前使用反向代理终止 TLS。

### 统一消息中间件

WebSocket 与 Webhook 标准化后的消息可以经过同一套中间件，再进入 `on_message`。中间件默认不启用，
因此不会改变已有事件回调行为：

```python
import botpy
from botpy.middleware import content_sanitizer, message_filter

client = MyClient(intents=botpy.Intents(public_guild_messages=True))
client.use(
    message_filter(window_seconds=5),
    content_sanitizer(collapse_whitespace=True, parse_face_tags=True),
)
client.run(appid="12345", secret="xxxx")
```

也可以编写 Koa 风格的异步中间件。调用 `await next_call()` 继续执行，不调用或使用
`context.stop(reason)` 可以短路后续中间件和 `on_message`：

```python
async def ignore_empty_message(context, next_call):
    if not context.message.content:
        context.stop("empty-message")
        return
    context.state["checked"] = True
    await next_call()

client.use(ignore_empty_message)
```

中间件仅处理新的统一消息回调；`on_at_message_create`、`on_c2c_message_create` 等旧回调仍按原方式触发。

常用的生产保护中间件也可以组合使用：

```python
from botpy.middleware import (
    RateLimitTier,
    ScopePolicy,
    access_policy,
    concurrency_guard,
    mention_gate,
    rate_limiter,
)

client.use(
    access_policy(group=ScopePolicy(mode="allowlist", allow=("group-openid",))),
    rate_limiter(per_sender=RateLimitTier(max_requests=5, window_seconds=60)),
    concurrency_guard(strategy="queue", max_queue=3, max_processing_seconds=120),
    mention_gate(require_mention_in_group=True),
)
```

`concurrency_guard` 支持 `queue`、`drop`、`abort` 和 `merge` 四种策略。它按回复目标隔离，
避免同一个用户或群的多个处理任务同时发送流式消息。

Slash Command 会解析参数、检查作用域和权限，并通过统一的 `Client.send_text()` 自动回复：

```python
from botpy.middleware import SlashCommand, slash_command

commands = slash_command(auto_help=True)
commands.register(
    SlashCommand(
        name=("echo", "say"),
        description="复读输入内容",
        handler=lambda context: " ".join(context.command.args),
    )
)
client.use(commands.middleware)
```

### 历史、引用与模型上下文

History Buffer 会保存同一个群最近的消息，Quote Ref 会通过 `msg_idx/ref_msg_idx` 解析引用内容，
Envelope Formatter 则把这些信息组合为可直接交给模型的上下文：

```python
from botpy.middleware import envelope_formatter, history_buffer, quote_ref

client.use(
    history_buffer(limit=10),
    quote_ref(max_size=1000),
    envelope_formatter(history_limit=5),
)
```

需要读取中间件状态时，可以覆盖 `on_message_context`。默认实现仍会调用原有的 `on_message`：

```python
class MyClient(botpy.Client):
    async def on_message_context(self, context):
        envelope = context.state.get("envelope", context.message.content)
        response = await call_your_model(envelope)
        await self.send_text(context.reply_target, response)
```

如果群聊要求必须 @ 机器人才处理，但仍希望记录所有群消息，应将 `history_buffer()` 放在
`mention_gate()` 前面。`MemoryHistoryStore` 与 `MemoryRefIndexStore` 可以替换成 Redis、SQL 等自定义 Store。

C2C 长耗时任务还可以启用输入状态续期：

```python
from botpy.middleware import typing_indicator

client.use(typing_indicator(duration_seconds=60, keepalive=True))
```

`Client.send_text()` 会为同一条被回复消息自动递增 `msg_seq`，避免多次回复因序号重复被平台拒绝。

### 统一发送、Markdown 与媒体

`Client.send()` 可以根据 `ReplyTarget` 自动选择 C2C、群聊、频道或频道私信接口。C2C/群聊省略
`msg_type` 时，会根据 `markdown`、`ark`、`embed`、`media` 自动推断消息类型：

```python
from botpy.protocol import MessageType

await client.send(context.reply_target, content="普通文本")
await client.send(
    context.reply_target,
    msg_type=MessageType.MARKDOWN,
    markdown={"content": "# Markdown"},
    keyboard={"content": {"rows": []}},
)

# 新平台字段尚未被 SDK 建模时，可以通过 extra 原样透传。
await client.send(context.reply_target, content="唤醒", extra={"is_wakeup": True})
```

常用消息可以直接使用 `send_markdown()`、`send_text_with_keyboard()` 和仅支持 C2C 的
`send_wakeup()`。撤回消息使用 `recall_message(target, message_id)`；当前已确认支持 C2C、群聊和频道，
频道私信没有已确认的撤回路由，因此会明确抛出 `ValueError`。

C2C 与群聊媒体支持 URL、Base64、内存字节或本地文件四种互斥来源。内存字节和本地文件达到
5 MiB 时会自动使用分片上传，避免将大文件整体转换成 Base64；URL 与 Base64 仍使用单次上传：

```python
from botpy.protocol import MediaFileType

upload = await client.upload_media(
    context.reply_target,
    MediaFileType.IMAGE,
    local_path="./image.png",
)

result = await client.send_image(
    context.reply_target,
    url="https://example.com/image.png",
    content="图片说明",
)
print(result.upload["file_info"], result.message["id"])
```

还提供 `send_video()`、`send_voice()`、`send_file()` 和通用的 `send_media()`。普通文件会传递经过
清洗的 `file_name`；本地文件在读取前会检查是否为常规文件、是否为符号链接以及大小是否超限。
图片、视频、语音和普通文件的上限分别为 30、100、20、100 MiB。

分片上传会计算 `md5`、`sha1` 与平台要求的 `md5_10m`，根据服务端建议并发度上传并调用
`on_progress(uploaded_bytes, total_bytes)`。例如：

```python
await client.send_video(
    context.reply_target,
    local_path="./video.mp4",
    on_progress=lambda uploaded, total: print(f"{uploaded}/{total}"),
)
```

分片确认遇到业务码 `40093001` 时会在服务端给出的时限内持续重试；准备阶段业务码 `40093002`
会抛出 `UploadDailyLimitExceededError`。已确认的分片完成协议不支持 `srv_send_msg=True`，需要直接发送时
应使用 `send_image()`、`send_video()`、`send_voice()`、`send_file()` 或 `send_media()`。

### C2C 流式消息

C2C 的 `stream_messages` 接口可以通过 `open_stream()` 管理。每次 `update()` 必须传入截至当前的
完整文本，而不是单个增量；会话会使用 replace 模式、复用同一个 `msg_seq`、自动递增 `index`，并将
高频更新限制为至少 300ms 一次：

```python
stream = client.open_stream(context.reply_target, throttle_ms=500)
full_text = ""
try:
    async for chunk in model_stream():
        full_text += chunk
        await stream.update(full_text)
    await stream.complete()
except Exception:
    stream.cancel()
    raise
```

流式消息只支持带入站 `message_id` 的 C2C `ReplyTarget`。`complete()` 会发送 `input_state=10` 的
最终帧；遇到 HTTP 429 或平台错误码 `50002` 时会退避重试，并为重试帧分配新的 `index`。

### 统一持久化 KV Store

Gateway Session、群历史和引用索引可以共用一个 JSON 文件 KV Store：

```python
from botpy.middleware import history_buffer, quote_ref
from botpy.storage import (
    JsonFileKVStore,
    KVHistoryStore,
    KVRefIndexStore,
    KVSessionStore,
)

kv = JsonFileKVStore("./.botpy-data", save_throttle=1)
session_store = KVSessionStore(kv, ttl=300)

client = MyClient(intents=intents, session_store=session_store)
client.use(
    history_buffer(store=KVHistoryStore(kv, ttl=24 * 60 * 60)),
    quote_ref(store=KVRefIndexStore(kv, ttl=7 * 24 * 60 * 60)),
)
client.run(appid="12345", secret="xxxx")
```

`JsonFileKVStore` 使用临时文件原子替换并支持 TTL、写入节流、前缀清理和显式 `flush()`。
它适合单进程轻量部署；多进程环境应实现同一 `KVStore` 接口并接入 Redis、SQL 或云 KV。
`KVSessionStore.close()` 会自动 flush 共享 Store；如果只使用 History/Ref Adapter，应在退出前调用
`await kv.close()`。

### 备注

也可以通过预设置的类型，设置需要监听的事件通道

```python
import botpy

intents = botpy.Intents.none()
intents.public_guild_messages=True
```

### 使用API

如果要使用`api`方法，可以参考如下方式:

```python
import botpy
from botpy.message import Message

class MyClient(botpy.Client):
    async def on_at_message_create(self, message: Message):
        await self.api.post_message(channel_id=message.channel_id, content="content")
```

## 示例机器人

[`examples`](./examples/) 目录下存放示例机器人，具体使用可参考[`Readme.md`](./examples/README.md) 

    examples/
    .
    ├── README.md
    ├── config.example.yaml          # 示例配置文件（需要修改为config.yaml）
    ├── demo_announce.py             # 机器人公告API使用示例
    ├── demo_api_permission.py       # 机器人授权查询API使用示例
    ├── demo_at_reply.py             # 机器人at被动回复async示例
    ├── demo_at_reply_ark.py         # 机器人at被动回复ark消息示例
    ├── demo_at_reply_embed.py       # 机器人at被动回复embed消息示例
    ├── demo_at_reply_command.py     # 机器人at被动使用Command指令装饰器回复消息示例
    ├── demo_at_reply_file_data.py   # 机器人at被动回复本地图片消息示例
    ├── demo_at_reply_keyboard.py    # 机器人at被动回复md带内嵌键盘的示例
    ├── demo_at_reply_markdown.py    # 机器人at被动回复md消息示例
    ├── demo_at_reply_reference.py   # 机器人at被动回复消息引用示例
    ├── demo_dms_reply.py            # 机器人私信被动回复示例
    ├── demo_get_reaction_users.py   # 机器人获取表情表态成员列表示例
    ├── demo_guild_member_event.py   # 机器人频道成员变化事件示例
    ├── demo_interaction.py          # 机器人互动事件示例（未启用）
    ├── demo_pins_message.py         # 机器人消息置顶示例
    ├── demo_recall.py               # 机器人消息撤回示例
    ├── demo_schedule.py             # 机器人日程相关示例

# 参与开发

## 环境配置

```bash
pip install -r requirements.txt   # 安装依赖的pip包

pre-commit install                 # 安装格式化代码的钩子
```

## 单元测试

代码库提供API接口测试和 websocket 的单测用例，位于 `tests` 目录中。如果需要自己运行，可以在 `tests` 目录重命名 `.test.yaml` 文件后添加自己的测试参数启动测试：

### 单测执行方法

先确保已安装 `pytest` ：

```bash
pip install pytest
```

然后在项目根目录下执行单测：

```bash
pytest
```

## 致谢

感谢感谢以下开发者对 `botpy` 作出的贡献：

<a href="https://github.com/tencent-connect/botpy/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=tencent-connect/botpy" />
</a>

# 加入官方社区

欢迎扫码加入**QQ 频道开发者社区**。

![开发者社区](https://guild-1251316161.cos.ap-guangzhou.myqcloud.com/miniapp/icons/qq_guild_developer_doc.png)
