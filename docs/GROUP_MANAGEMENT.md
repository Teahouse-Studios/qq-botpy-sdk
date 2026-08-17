# 群管理 API 与事件

这些接口使用群 OpenID，并要求机器人具备对应的群权限。入群申请、禁言和自动审批策略通常要求机器人是群管理员；
群基本信息和机器人群内状态接口还可能需要平台白名单权限。

## API 列表

| SDK 方法 | 平台接口 |
| --- | --- |
| `get_group_info()` | 获取群基本信息 |
| `get_group_bot_state()` | 获取机器人群内状态 |
| `get_group_join_requests()` | 分页拉取入群申请 |
| `approve_group_join_request()` | 通过入群申请 |
| `decline_group_join_request()` | 拒绝入群申请，可同时拉黑 |
| `handle_group_join_request()` | 使用 `approve` / `decline` 执行通用审批 |
| `get_group_mute_setting()` | 查询全员及成员禁言状态 |
| `set_group_member_mutes()` | 批量增加、更新或解除成员禁言 |
| `get_group_join_approval_strategies()` | 查询自动审批策略 |
| `create_group_join_approval_strategy()` | 创建自动审批策略 |
| `update_group_join_approval_strategy()` | 修改自动审批策略 |
| `delete_group_join_approval_strategy()` | 删除自动审批策略 |
| `execute_group_join_approval_strategy()` | 对关联群执行全量扫描 |
| `update_group_join_approval_whitelist()` | 增删策略白名单 QQ 号码 |

## 入群申请

```python
requests = await client.api.get_group_join_requests(
    group_openid,
    cursor=None,
    limit=20,
)

for request in requests["list"]:
    await client.api.approve_group_join_request(
        group_openid,
        request["member_openid"],
        join_request_id=request["join_request_id"],
    )
```

拒绝并加入群黑名单：

```python
await client.api.decline_group_join_request(
    group_openid,
    member_openid,
    join_request_id=join_request_id,
    reject_reason="未通过验证",
    add_to_member_blacklist=True,
)
```

## 成员禁言

```python
setting = await client.api.get_group_mute_setting(group_openid)

await client.api.set_group_member_mutes(
    group_openid,
    [
        {
            "op": "add",
            "member_openid": member_openid,
            "mute_expire_at": "2026-08-12T12:00:00+08:00",
        }
    ],
)

await client.api.set_group_member_mutes(
    group_openid,
    [{"op": "del", "member_openid": member_openid, "mute_expire_at": ""}],
)
```

单次最多设置 10 个成员。`op` 支持 `add`、`update` 和 `del`。

## 自动审批策略

创建策略时，`group_openids` 和 `group_ids` 必须二选一：

```python
strategy = await client.api.create_group_join_approval_strategy(
    group_openids=[group_openid],
    is_enable="on",
    remark="内部用户自动审批",
)

strategy_id = strategy["strategy_id"]
await client.api.update_group_join_approval_whitelist(
    strategy_id,
    op="add",
    whitelist_users=["1234567", "1234568"],
)
await client.api.execute_group_join_approval_strategy(strategy_id)
```

每个机器人最多创建 20 个策略。关联群列表单次最多 100 个，白名单单次最多修改 10000 个号码。
白名单号码应使用字符串，避免跨语言调用时发生整数精度问题。

## 群事件

群机器人管理和入群申请事件使用 `GROUP_AND_C2C_EVENT (1 << 25)`，群成员加入、退出事件使用 `GROUP_MEMBER_EVENT (1 << 24)`：

```python
intents = botpy.Intents(public_messages=True, group_member_event=True)
```

| 回调 | 事件对象 | Intent |
| --- | --- | --- |
| `on_group_add_robot` | `GroupManageEvent` | `public_messages` |
| `on_group_del_robot` | `GroupManageEvent` | `public_messages` |
| `on_group_msg_receive` | `GroupManageEvent` | `public_messages` |
| `on_group_msg_reject` | `GroupManageEvent` | `public_messages` |
| `on_group_member_add` | `GroupMemberEvent` | `group_member_event` |
| `on_group_member_remove` | `GroupMemberEvent` | `group_member_event` |
| `on_group_join_request` | `GroupJoinRequestEvent` | `public_messages` |

```python
from botpy.manage import GroupJoinRequestEvent, GroupMemberEvent


class MyClient(botpy.Client):
    async def on_group_member_add(self, event: GroupMemberEvent):
        print(event.group_openid, event.member_openid, event.user_openid)

    async def on_group_join_request(self, event: GroupJoinRequestEvent):
        if event.auto_approved:
            print("自动审批策略：", event.auto_approved["strategy_id"])
            return
        await event.approve()
```

`GroupJoinRequestEvent.approve()` 和 `decline()` 会自动携带事件中的群 OpenID、成员 OpenID 与申请 ID。
例如：`await event.decline(reject_reason="未通过验证", add_to_member_blacklist=True)`。

旧版本曾将成员事件分发为 `on_message_group_member_add` 和 `on_message_group_member_remove`。新代码应使用
`on_group_member_add` 和 `on_group_member_remove`；SDK 在未定义新回调时仍会回退调用旧名称。
