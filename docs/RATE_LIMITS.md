# 请求整流与接口限速

本文说明 botpy 在框架层如何对发出的 REST 请求整流（rate limiting），以及这些配额是如何
从 QQ 机器人开放平台官方文档逐条核对得到的。

- 文档来源：<https://bot.q.qq.com/wiki/develop/api-v2/>
- 运行时实现：`botpy/protocol/ratelimit.py`
- 抓取与核对脚本：`generated_docs/fetch_docs.py`、`generated_docs/extract_rate_limits.py`
  （输出到被 git 忽略的 `generated_docs/`，可随时重新生成）

## 为什么需要整流

平台为每个接口声明了 `接口频率限制`，消息类接口还会叠加「主动消息」的 Bot 维度、单关系维度
和每日额度。突发流量下即使平均值达标，瞬时并发也会触发 `HTTP 429`；而 `429` 之后各协程各自
重试又会继续撞墙，形成雪崩。botpy 因此在传输层之上引入**队列化的令牌桶**：

1. 每个「逻辑通道」各自持有独立的令牌桶，配额来自官方文档；
2. 同一通道上的请求用 `asyncio.Lock` 串行排队，**先到先发**，到达速率被整形成均匀间隔；
3. 收到 `429` 时按 `Retry-After` 对该通道追加惩罚时间，让后续请求一起退避；
4. 未在文档中单独标注限制的接口回落到保守的同族默认值（50 QPS），保证不会无限速发出。

## 通道划分

| 通道键 | 含义 | 说明 |
| --- | --- | --- |
| `("route", "METHOD /path/{}")` | 接口级 | 文档中的 `接口频率限制` |
| `("bot", "proactive", kind)` | Bot 维度 | 主动消息配额，全 Bot 共享，与接收方无关 |
| `("proactive", route, resource)` | 单关系维度 | 主动消息配额，按群 / 用户 / 子频道分别计数 |
| `("relationship", route, resource)` | 单关系维度（始终生效） | 例如「子频道每 1s 最多 5 条」对主动与被动消息都生效 |

路由模板中的参数名会被统一成 `{}`，因此 `{group_openid}` 与 `{target_id}` 视为同一接口；
匹配时静态片段更多的模板优先，`/v2/groups/join_approval_strategy` 不会被
`/v2/groups/{}/...` 抢走。

### 主动消息的判定

没有 `msg_id` 也没有 `event_id` 的消息即为主动消息，因此只有主动消息才会消耗 Bot 维度与
单关系维度配额；被动回复只受接口级和「始终生效」配额约束。

### 认证档位

主动消息的 Bot 维度配额取决于认证等级，通过 `RateLimiter(certification=...)` 选择：

| 认证类型 | 单聊 | 群聊 |
| --- | --- | --- |
| 企业认证 / 个人认证（`"certified"`，**默认**） | 10 QPS | 60 QPM |
| 未认证（`"unverified"`） | 5 QPS + 30 QPM | 30 QPM |

**默认取 `"certified"`**。平台没有提供查询认证等级的接口：`/users/@me` 只返回
`id` / `username` / `avatar` / `bot` / `union_openid`，全部 api-v2 文档里「认证类型」只出现在
单聊与群聊消息接口的主动消息配额表中，因此档位只能由使用者声明。

这个默认值是有风险的，需要注意的是：

- 已认证机器人取默认值 → 正确，按 10 QPS / 60 QPM 发送；
- **未认证机器人取默认值 → 会按超出自身配额的速率发出**，触发
  `40034100 主动消息发送超过频控限制`，消息发不出去。

所以未认证的机器人必须显式降档：

```python
client = MyClient(intents=intents, rate_limit={"certification": "unverified"})
```

SDK 在构造整流器时会打一条 INFO 日志说明当前档位，并在已认证档位下提醒未认证机器人降档：

```
[botpy] 主动消息按「已认证」档位整流（单聊 10/1s；群聊 60/60s）。
若机器人未通过企业/个人认证，请设置 rate_limit={'certification': 'unverified'}，否则会超出平台配额被限流。
```

单关系维度对所有档位一致：单聊与群聊均为 20 QPM，且每个用户 / 每个群每天最多 1000 条。

## 每日额度的处理

窗口长于 `max_pacing_window`（默认 3600 秒）的配额被视作「配额」而非「速率」。用尽时 SDK 会
记录一条 `WARNING` 并仍然发送请求，由平台返回结构化错误（例如 `304045 子频道主动消息数限频`），
而不会让消息发送挂起数小时。若确实需要硬等待，可调大 `max_pacing_window`。

## 文档显式标注的接口限制

下表由 `generated_docs/extract_rate_limits.py` 从本地文档副本提取，并与
`botpy.protocol.ratelimit.DEFAULT_ROUTE_RULES` 逐条比对（当前 47/47 一致）。

| 接口 | 文档原文 | 整流参数 |
| --- | --- | --- |
| `DELETE /channels/{}` | 50 QPS | 50/1s |
| `GET /channels/{}` | 50 QPS | 50/1s |
| `PATCH /channels/{}` | 50 QPS | 50/1s |
| `GET /gateway` | 2 QPM / 10 QPM burst | 2/60s（burst 10） |
| `GET /guilds/{}` | 50 QPS | 50/1s |
| `GET /guilds/{}/channels` | 50 QPS | 50/1s |
| `POST /guilds/{}/channels` | 50 QPS | 50/1s |
| `PUT /interactions/{}` | 50 QPS | 50/1s |
| `GET /users/@me` | 50 QPS | 50/1s |
| `GET /users/@me/guilds` | 50 QPS | 50/1s |
| `POST /v2/generate_url_link` | 50 QPS | 50/1s |
| `GET /v2/groups/join_approval_strategy` | 60 QPM | 60/60s |
| `POST /v2/groups/join_approval_strategy` | 60 QPM | 60/60s |
| `DELETE /v2/groups/join_approval_strategy/{}` | 60 QPM | 60/60s |
| `PATCH /v2/groups/join_approval_strategy/{}` | 60 QPM | 60/60s |
| `POST /v2/groups/join_approval_strategy/{}/execute` | 60 QPM | 60/60s |
| `POST /v2/groups/join_approval_strategy/{}/whitelist_users` | 60 QPM | 60/60s |
| `POST /v2/groups/{}/approval_join_request/{}` | 60 QPM | 60/60s |
| `POST /v2/groups/{}/batch_remove_members` | 30 QPM | 30/60s |
| `GET /v2/groups/{}/bot_state` | 30 QPM | 30/60s |
| `POST /v2/groups/{}/files` | 50 QPS | 50/1s |
| `GET /v2/groups/{}/info` | 30 QPM | 30/60s |
| `GET /v2/groups/{}/join_request_list` | 30 QPM | 30/60s |
| `GET /v2/groups/{}/member_blacklist` | 30 QPM | 30/60s |
| `POST /v2/groups/{}/member_blacklist` | 60 QPM | 60/60s |
| `GET /v2/groups/{}/members` | 60 QPM | 60/60s |
| `GET /v2/groups/{}/members/{}` | 30 QPM | 30/60s |
| `POST /v2/groups/{}/messages` | 100 QPS | 100/1s |
| `DELETE /v2/groups/{}/messages/{}` | 10 QPS | 10/1s |
| `GET /v2/groups/{}/restrict_chat_setting` | 30 QPM | 30/60s |
| `POST /v2/groups/{}/restrict_chat_setting` | 60 QPM | 60/60s |
| `POST /v2/groups/{}/upload_part_finish` | 10 QPS | 10/1s |
| `POST /v2/groups/{}/upload_prepare` | 10 QPS | 10/1s |
| `GET /v2/menu` | 30 QPM | 30/60s |
| `PUT /v2/menu` | 5 QPM | 5/60s |
| `GET /v2/panels` | 30 QPM | 30/60s |
| `POST /v2/panels` | 10 QPM | 10/60s |
| `DELETE /v2/panels/{}` | 10 QPM | 10/60s |
| `GET /v2/panels/{}` | 30 QPM | 30/60s |
| `PUT /v2/panels/{}` | 10 QPM | 10/60s |
| `PUT /v2/panels/{}/target` | 60 QPM | 60/60s |
| `POST /v2/users/{}/files` | 50 QPS | 50/1s |
| `POST /v2/users/{}/messages` | 100 QPS，包括主动、被动等所有消息类型 | 100/1s |
| `DELETE /v2/users/{}/messages/{}` | 10 QPS | 10/1s |
| `POST /v2/users/{}/stream_messages` | 50 QPS | 50/1s |
| `POST /v2/users/{}/upload_part_finish` | 10 QPS | 10/1s |
| `POST /v2/users/{}/upload_prepare` | 10 QPS | 10/1s |
## 未显式标注限制的接口

SDK 声明了 88 条 REST 路由，其中 42 条能在文档中找到显式限制，其余按以下方式整流：

| 情况 | 处理 |
| --- | --- |
| 同族接口有文档限制 | 沿用同族值，例如 `/channels/{}/audio`、`/guilds/{}/roles` 等按 50 QPS |
| `GET /gateway/bot` | 对应文档中的 `GET /gateway`：2 QPM，burst 10 |
| `POST /channels/{}/messages` | 接口级 50 QPS，另加「每子频道 1s 最多 5 条」 |
| `POST /dms/{}/messages` | 私信场景：每 Bot 每天 200 条、每个用户每天 2 条 |
| 完全未匹配的路径 | `default_budgets`，默认 50 QPS |

完整的未匹配清单见 `generated_docs/RATE_LIMITS.md` 第四节。

## 可观测性

`RateLimiter.snapshot()` 返回各通道的排队深度与剩余令牌，便于接入监控：

```python
{
    "enabled": True,
    "certification": "unverified",
    "rules": 51,
    "channels": [
        {
            "key": "route POST /v2/groups/{}/messages",
            "pending": 3,
            "budgets": ["100/1s"],
            "available": [97.412],
        },
    ],
}
```

`RateLimiter.plan_for(method, path, json_body=...)` 会返回本次请求将要占用的通道与配额，
可用于配置核对与测试断言。

## 不在整流范围内的请求

- **access token**：`TokenManager` 自身做了缓存（默认 7200 秒）与 single-flight 刷新，
  调用频率天然极低，且平台未在本篇文档中声明其限制。
- **媒体预签名地址**：只对配置的 `base_url`（`api.bot.qq.com`）上的请求计入接口配额，
  上传到 CDN 的预签名地址不会占用接口额度，也不会被接口队列拖慢。

## 相关配置

```python
from botpy.protocol import RateLimitBudget, RateLimiter, RouteRule

client = MyClient(
    intents=intents,
    rate_limit={
        "certification": "unverified",  # 未认证机器人必须降档；默认是 certified
        "max_concurrency": 8,           # 全局在途请求上限，None 表示不限制
        "max_buckets": 4096,            # 令牌桶注册表上限（按 LRU 回收空闲通道）
        "default_penalty": 1.0,         # 429 未带 Retry-After 时的默认惩罚秒数
        "max_pacing_window": 3600.0,    # 超过该窗口的配额只提示不阻塞
        "route_rules": {                # 覆盖或补充默认限制表
            "POST /v2/groups/{}/messages": RouteRule((RateLimitBudget(20, 60.0),)),
        },
    },
)

client = MyClient(intents=intents, rate_limit=False)  # 完全关闭整流
```
