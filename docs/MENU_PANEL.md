# 自定义菜单与指令面板

SDK 支持把全局自定义菜单和指令面板直接声明在 `Client` 上。客户端启动后会将声明与平台配置同步，适合把
机器人界面配置和应用代码一起版本管理。

```python
import botpy
from botpy.configuration import Menu, Panel


menu = Menu(
    items=[
        Menu.message("帮助", "/help"),
        Menu.link("官网", "https://example.com"),
        Menu.switch("搜索", "search", default=False),
        Menu.submenu(
            "更多",
            [
                Menu.sub.message("设置", "/settings"),
                Menu.sub.link("文档", "https://example.com/docs"),
            ],
        ),
    ]
)

panels = [
    Panel(
        "c2c-main",
        scope="c2c",
        items=[
            Panel.command("查询天气", desc="查询当前天气"),
            Panel.link(
                "更多服务",
                "https://example.com/services",
                desc="打开服务页面",
            ),
        ],
        remark="C2C 常用功能",
    ),
    Panel(
        "group-admin",
        scope="group",
        target_type="specific",
        group_openids=["group-openid-1", "group-openid-2"],
        items=[
            Panel.command("群签到", desc="每日签到"),
            Panel.command("管理设置", only_admin=True),
        ],
        remark="指定群管理面板",
    ),
]

client = botpy.Client(
    intents=botpy.Intents(public_messages=True),
    menu=menu,
    panels=panels,
    config_sync_strict=False,
)
```

`Client.__init__()` 只保存声明，不发送网络请求。客户端启动并取得 access token 后才会读取平台配置并执行同步；
因此，修改声明后需要重启相应实例才能自动应用。同步使用与其他 REST API 相同的鉴权、限流和错误处理机制。
不传 `menu` 或 `panels` 时，SDK 不会同步对应资源；传入空的 `panels=[]` 也不会删除平台上的已有面板。

## Menu

平台对一个机器人只提供一份全局菜单。菜单仅用于 C2C（单聊），设置后对所有用户生效，不能按用户分别配置。
因此 `Client` 接受一个 `menu`，而不是菜单列表。

| 构造方法 | 用途 |
| --- | --- |
| `Menu.message(name, content)` | 点击后将 `content` 填入聊天输入框 |
| `Menu.link(name, url)` | 打开 HTTPS 链接 |
| `Menu.switch(name, switch_id, default=False)` | 展示开关并设置默认状态 |
| `Menu.submenu(name, items)` | 创建包含二级菜单项的折叠菜单 |
| `Menu.sub.message(...)` / `Menu.sub.link(...)` | 创建仅允许出现在二级菜单中的菜单项 |

菜单最多包含 10 个一级项。一级名称最多 10 个平台字符单位，其中一个中文字符按两个单位计算。二级菜单最多
包含 5 项，名称最多 14 个平台字符单位，只能使用 `Menu.sub.message()` 和 `Menu.sub.link()`，不能继续嵌套。
所有链接必须以 `https://` 开头。

用户打开 switch 后，平台发送的消息会在 `ext` 中携带 `<switch_id>=1`；关闭时不携带该标记。平台当前没有为
菜单开关定义独立的事件类型。

启动同步时，SDK 会比较声明和当前全局菜单，并在需要时全量覆盖远端菜单。全局菜单不是按实例或版本隔离的：
同一机器人下最后一次成功同步的声明会成为所有 C2C 用户看到的配置。

## Panel

一个 `Client` 可以声明多块指令面板。每块面板的第一个参数 `key` 是 SDK 的稳定管理标识：

```python
Panel(
    "weather-panel",
    scope="channel",
    items=[Panel.command("查询天气", desc="输入城市名查询天气")],
    remark="天气功能",
)
```

SDK 会把 `key` 编码到面板 `remark` 的受管标记中，以便下次启动时找到对应的远端 `panel_id`。同一 Client 的
Panel key 必须唯一；上线后应保持稳定。修改 key 会被视为一块新的面板，而不是重命名已有面板。不要在平台侧
删除或改写 SDK 的受管标记，否则下次同步可能无法识别原面板。

`Panel` 构造参数如下：

```python
Panel(
    key,
    *,
    scope,
    items,
    target_type="all",
    user_openids=None,
    group_openids=None,
    remark=None,
)
```

`scope` 支持 `c2c`、`group`、`channel` 和 `dm`。`target_type="all"` 表示对该场景全局生效；只有 c2c 和
group 支持 `specific`：

- c2c specific 面板通过 `user_openids` 指定用户。
- group specific 面板通过 `group_openids` 指定群。
- channel 和 dm 只能使用 `target_type="all"`。

面板项使用 `Panel.command(name, desc=None, only_admin=False)` 或
`Panel.link(name, url, desc=None, only_admin=False)` 创建。每块面板最多 20 项；名称最多 14 个平台字符单位，
描述最多 30 个平台字符单位，链接必须使用 HTTPS。`only_admin=True` 只对存在管理员身份的群聊或频道场景有意义。
`remark` 与 SDK 的受管 key 标记合计最多 255 个字符，不会展示给用户；因此 key 越长，可用备注长度越短。

每个机器人最多存在 20 块指令面板。创建 specific 面板以及单次修改关联对象时，OpenID 列表最多包含 20 项。

## 启动同步语义

声明式配置采用非破坏性的 reconcile：

1. 全局菜单存在差异时，SDK 使用本地 `Menu` 完整覆盖它。
2. 对每个声明的 Panel，SDK 根据受管 key 查找远端面板；不存在则创建，存在则更新内容和关联对象。
3. 未在本次 `panels` 中声明的面板不会被自动删除。
4. 没有 SDK 受管标记的面板不会被接管或删除。

这种行为允许声明式面板和平台侧手工维护的面板共存，也降低遗漏一项配置导致线上资源被删除的风险。需要移除
面板时，应显式调用 `await client.api.delete_panel(panel_id)`；仅从 `panels` 列表删除不会删除远端面板。

平台的空面板页可能只返回 `{"is_end": true}` 并省略 `records` 与 `next_cursor`，SDK 会将其兼容为空列表；
非末页响应仍必须包含有效的 `next_cursor`。

`config_sync_strict` 控制启动同步失败时的行为：

- `False`（默认）：记录同步错误并继续启动。机器人仍可收发事件，但菜单或面板可能保留旧配置或只同步了一部分。
- `True`：任一声明同步失败都会使启动失败。适合要求界面配置与当前发布版本一致，并已设置单实例同步或外部协调的部署。

同步涉及多个平台请求，不是跨菜单和所有面板的原子事务。即使使用 strict 模式，失败前已经成功的请求也不会自动
回滚；strict 保证的是“不带着已知同步错误继续运行”，不是事务性更新。

## 多副本部署

同一个机器人使用多个进程、Pod 或地域副本时，每个携带 `menu` 或 `panels` 声明的副本都会在启动时尝试同步。
平台可能返回“指令面板操作进行中”等并发冲突，而且配置不同的副本会互相覆盖全局菜单或来回更新同一个受管面板。

生产环境建议只让一个受协调的副本负责声明式同步，例如使用 leader election、部署前任务或单独的管理实例；其他
副本不要传入 `menu`/`panels`。如果所有副本声明完全一致，默认非 strict 模式可以降低短暂冲突对事件服务的影响，
但不能替代跨副本协调。启用 strict 后，并发冲突会直接导致相应副本启动失败，可能形成集中重启。

滚动发布期间，新旧版本声明可能同时存在。尤其是全局菜单只有一份，旧副本重启后可能把新菜单覆盖回旧版本。
因此滚动发布也应保证只有当前发布的同步负责人能够写入配置。

## 直接调用 REST API

不需要启动同步时，可以直接使用底层 API：

```python
current = await client.api.get_global_menu()
version = await client.api.update_global_menu(menu.to_dict())

page = await client.api.get_panels("c2c", limit=20)
created = await client.api.create_panel(
    "c2c",
    panel.to_dict(),
    target_type="specific",
    user_openids=["user-openid"],
)
detail = await client.api.get_panel(created["panel_id"])
await client.api.update_panel(created["panel_id"], panel.to_dict())
await client.api.update_panel_targets(
    created["panel_id"],
    op="add",
    user_openids=["another-user-openid"],
)
await client.api.delete_panel(created["panel_id"])
```

直接 API 调用会立即修改远端状态，也不会自动更新 `Client` 中保存的声明。若同一进程随后执行启动同步，仍以声明式
配置为准。

需要在运行期间重新协调声明时，可以显式调用：

```python
result = await client.configuration.sync()
```

该调用与启动同步使用相同的差异比较、分批关联对象更新和进程内并发合并逻辑。
