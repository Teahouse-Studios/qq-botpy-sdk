# 新增 API 参考

## Client 配置

```python
client = botpy.Client(
    intents=intents,
    menu=menu,
    panels=panels,
    config_sync_strict=False,
    markdown_support=False,
    base_url="https://api.sgroup.qq.com",
    token_base_url="https://bots.qq.com",
    user_agent="my-bot/1.0",
    ssl=ssl_context,
    upload_cache=UploadCache(),
    reply_limiter=ReplyLimiter(limit=4, ttl_seconds=3600),
    on_message_sent=lambda ref_idx, meta: save_ref(ref_idx, meta),
    loguru_logger=logger,
)
```

`ssl` 会原样传给 aiohttp，可使用 `ssl.SSLContext`、aiohttp Fingerprint 或布尔值。生产环境不要使用
`False`；自定义 CA 应使用 `ssl.create_default_context(cafile=...)`。

首次登录会同步获取 token，随后启动后台提前刷新循环；`Client.close()` 会停止该任务。

`loguru_logger` 是可选兼容入口，不会让 Loguru 成为强制依赖。配置方法见 [LOGURU.md](./LOGURU.md)。

`menu` 和 `panels` 用于声明机器人启动时要同步的全局菜单与指令面板。`config_sync_strict=False` 时同步失败会
记录错误并继续启动；设为 `True` 时同步失败将中止启动。构造方法、同步边界与多副本部署注意事项见
[自定义菜单与指令面板](./MENU_PANEL.md)。

## 统一消息发送

- `await client.send(target, content=..., msg_type=..., markdown=..., media=..., extra=...)`
- `await client.send_text(target, content)`
- `await client.send_markdown(target, content, keyboard=None)`
- `await client.send_text_with_keyboard(target, content, keyboard)`
- `await client.send_wakeup(target, content)`
- `await client.recall_message(target, message_id)`

`send_text()` 自动切分超过 5000 字符的文本。单段返回一个平台响应，多段返回响应列表。C2C 和群聊携带
`message_id` 时会执行被动回复限制；超限后自动转主动发送。

## 媒体

```python
await client.send_image(target, local_path="cat.png")
await client.send_video(target, data=video_bytes, on_progress=progress)
await client.send_voice(target, url="https://example.com/voice.silk")
await client.send_file(target, local_path="report.pdf", content="报告")
```

`upload_media()` 返回上传响应；`send_media()` 和四个类型便捷方法返回 `MediaSendResult(upload, message)`。
相同 bytes、base64 或本地文件会按内容 MD5、聊天 scope、目标和媒体类型缓存。

## 通用 REST API

```python
guilds = await client.api.get("/users/@me/guilds", params={"limit": 100})
result = await client.api.post("/future/endpoint", {"enabled": True})
token = await client.api.get_token()
```

POST/PATCH 默认不重试。只有确认接口具有幂等语义时，才传 `retry_unsafe=True`。

## 群管理

```python
info = await client.api.get_group_info(group_openid)
requests = await client.api.get_group_join_requests(group_openid, limit=20)
mute_setting = await client.api.get_group_mute_setting(group_openid)
strategies = await client.api.get_group_join_approval_strategies(limit=20)
```

群信息、入群申请审批、成员禁言、自动审批策略和新增群事件的完整说明见
[群管理 API 与事件](./GROUP_MANAGEMENT.md)。

## 自定义菜单与指令面板

```python
from botpy.configuration import Menu, Panel

menu = Menu(items=[Menu.message("帮助", "/help")])
panel = Panel(
    "main",
    scope="c2c",
    items=[Panel.command("查询天气", desc="查询当前天气")],
)

client = botpy.Client(
    intents=intents,
    menu=menu,
    panels=[panel],
)
```

平台只提供一个 C2C 全局菜单，但一个机器人可以配置多个不同场景的 Panel。SDK 使用 Panel 的稳定 key 匹配受管
资源，启动时创建或更新当前声明，不自动删除未声明或非受管面板。完整说明见
[自定义菜单与指令面板](./MENU_PANEL.md)。

底层 REST 方法包括 `get_global_menu()`、`update_global_menu()`、`get_panels()`、`create_panel()`、
`get_panel()`、`update_panel()`、`update_panel_targets()` 和 `delete_panel()`。

## Interaction

```python
class MyClient(botpy.Client):
    async def on_interaction_context(self, context):
        interaction_id = context.event.data["id"]
        context.state["handled"] = True
        await self.acknowledge_interaction(interaction_id, code=0)

    async def on_interaction_create(self, interaction):
        await interaction.acknowledge(0)
```

旧的 `on_interaction_create` 继续可用；新的 Context 回调适合共享状态和后续中间件扩展。

## 独立工具

```python
from botpy.protocol import ReplyLimiter, UploadCache, chunk_text, compute_file_hash
```

- `UploadCache`：TTL/LRU 有界内存缓存，默认最大 500 项。
- `ReplyLimiter`：默认每条入站消息一小时内最多 4 次被动回复。
- `chunk_text(text, limit=5000)`：固定字符窗口切分。
- `compute_file_hash(data)`：生成上传缓存使用的 MD5。
- `parse_data_url()`、`guess_mime_type()`、`detect_media_kind()`：Data URL、MIME 和媒体类型识别。
- `parse_image_size()`、`format_qqbot_markdown_image()`：图片头尺寸和 QQ Markdown 图片语法。
- `parse_target()`、`normalize_target()`：解析 `qqbot:c2c/group/channel:<id>` 目标。
- `pcm_to_wav()`、`ffmpeg_to_pcm()`、`detect_ffmpeg()`：语音格式基础转换；FFmpeg 不随 SDK 捆绑。
- `format_error_message()`、`format_duration()`、`format_file_size()`：通用格式化工具。

当平台响应包含 `ext_info.ref_idx` 时，`on_message_sent` 会收到该索引和 scope、target、payload 元数据；
也可在运行中通过 `client.set_message_sent_hook()` 替换。
