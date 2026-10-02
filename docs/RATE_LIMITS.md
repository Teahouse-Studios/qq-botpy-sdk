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
2. 同一通道上的请求用一个**按优先级排队的门**串行取令牌（见「队列优先级」）：
   同优先级内部先到先发，高优先级可以插到低优先级前面；
3. 收到 `429` 时按 `Retry-After` 对该通道追加惩罚时间，让后续请求一起退避；
4. 未在文档中单独标注限制的接口回落到保守的同族默认值（50 QPS），保证不会无限速发出。

令牌的消费时机很关键：并发槽位在**取令牌之前**获取，令牌紧挨着真正发送才被消耗。反过来的
话，等待槽位期间会堆积令牌，槽位一释放就成串发出，反而绕过速率约束。

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

## 队列优先级

同一通道内只有当前持有者能取令牌，其余请求排队。早期实现直接用 `asyncio.Lock`，而它是严格
FIFO 的：一批批量推送排进队列后，后面到达的对话消息会被堵在队尾，等前面几百条推送逐条发完
才能发出，也就是被「饿死」。

SDK 因此把等待队列换成按优先级排序的队列，高优先级插到队头：

| 优先级 | `RequestPriority` | 判定方式 | 典型场景 |
| --- | --- | --- | --- |
| 0 | `INTERACTIVE` | 消息体带 `msg_id` 或 `event_id` | 被动回复，用户正在等响应 |
| 1 | `NORMAL` | 其余接口 | 普通 API 调用 |
| 2 | `BULK` | 消息体没有被动回复标记 | 主动消息 / 批量推送 |

优先级只决定**谁先拿到下一个令牌**，不改变令牌桶的速率约束：整体吞吐不变，改变的只是延迟。
已经在取令牌的请求不会被抢占，所以插队不会打断进行中的请求。

需要明确的边界：一个请求可能同时占用多个通道（例如 Bot 维度 + 单关系 + 接口），当前实现按
排序后的通道**逐个**取令牌，因此：

- 高优先级请求在**每一条**通道上都会排在同优先级/低优先级等待者前面，但它在某条通道上仍可能
  排在正持有该通道的请求之后；
- 因此"少等一个令牌间隔"只是单通道、单个持有者下的量级估计，不是任意负载下的时延上界；
- 实际延迟还受惩罚时间、单次 HTTP 耗时、以及多条通道中最慢那条的影响。

### 防止低优先级被反向饿死

严格优先级会把问题反过来：持续到来的对话消息可以让批量推送永远拿不到令牌。因此实现里加了
一道保护：**连续放行 `low_priority_interval`（默认 4）个高/普通优先级请求后，强制放行一个
等待最久的低优先级请求**。

注意这道保护约束的是**放行次数**，不是绝对时延：它保证低优先级不会被无限期推迟，但单次等待
多久仍取决于令牌补充速率、惩罚时间和单次 HTTP 耗时。设为 `None` 即关闭该保护，退化为严格
优先级。

```python
from botpy.protocol import RateLimiter

RateLimiter(low_priority_interval=4)      # 默认：每放行 4 个高优先级请求，插 1 个批量推送
RateLimiter(low_priority_interval=None)   # 严格优先级，低优先级可能被无限期推迟
```

### 显式指定优先级

消息发送接口的优先级会自动判定，无需配置。自定义接口可以在低层入口显式传入：

```python
from botpy.protocol import RequestPriority

await client.api.request(
    "POST",
    "/custom/endpoint",
    json=payload,
    priority=RequestPriority.INTERACTIVE,
)
```

`priority` 依次穿过 `ClientAPI.request` → `BotHttp.request` → `ApiClient.request` →
`RateLimiter.acquire`；省略（默认）时由整流器按请求语义自动判定。

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
            "pending": 12,
            "waiting_by_priority": {"interactive": 3, "normal": 0, "bulk": 9},
            "budgets": ["100/1s"],
            "available": [97.412],
        },
    ],
}
```

`RateLimiter.plan_for(method, path, json_body=...)` 会返回本次请求将要占用的通道与配额，
可用于配置核对与测试断言。

### 通道注册表与回收

`max_buckets`（默认 4096）限制通道注册表的规模，超出后按 LRU 回收**完全恢复**空闲通道。
只有满足以下条件的通道才可以被回收：没有等待者、没有持有者、所有令牌桶都已补满、且没有
仍在生效的 `429` 惩罚。

回收一个已经消耗过令牌的桶会把平台配额静默清零（再次访问会拿到满令牌的新桶，本该等待的
请求会立即放行），因此**当所有通道都还有未恢复的状态时，注册表允许超过 `max_buckets`**：
宁可多占一点内存，也不绕过平台限速。

### 并发槽位与令牌的先后顺序

`max_concurrency`（默认 `None`，即不限制）对应的并发槽位在**取令牌之前**获取，令牌紧挨着
真正发送才被消耗。这一点很关键：如果先取令牌再等槽位，等待期间令牌会持续堆积，槽位一释放
就会成串发出，实测可以把 `1/s` 的接口打成 `0.02s` 间隔的突发，反而绕过速率约束。

代价是：启用 `max_concurrency` 后，等待限流令牌的请求也会占用一个槽位。对绝大多数场景这是
正确的取舍——`max_concurrency` 表达的是"同时在途的请求数"。

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
        "low_priority_interval": 4,     # 每 4 个高优先级请求插 1 个批量推送；None 关闭
        "route_rules": {                # 覆盖或补充默认限制表
            "POST /v2/groups/{}/messages": RouteRule((RateLimitBudget(20, 60.0),)),
        },
    },
)

client = MyClient(intents=intents, rate_limit=False)  # 完全关闭整流
```
