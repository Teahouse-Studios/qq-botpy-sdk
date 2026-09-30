# 消息发送失败处理

平台把消息发送的业务错误放在响应体的 `err_code` 里。文档明确要求「请不要依据 `message`
来判定一个请求是否失败……建议根据 `err_code` 判断请求是否失败」，因此 SDK 不再只看 HTTP
状态码，而是按错误码决定下一步动作。

- 运行时实现：`botpy/protocol/send_policy.py`
- 覆盖核对脚本：`generated_docs/extract_send_errors.py`（输出 `generated_docs/SEND_ERRORS.md`）
- 相关配置：`Client(send_policy=MessageSendPolicy(...))`

## err_code 优先

`botpy/protocol/errors.py` 的 `ApiError.from_response()` 现在优先读取 `err_code`，其次才是
旧接口/网关的 `code`，因此 `ApiError.code` 一定能拿到平台错误码。

另外，`201/202 异步操作成功，虽然说成功，但是会返回一个 error body，需要特殊处理`：
响应体里出现 `err_code` 且非 0 时同样按失败处理，但下列「异步受理成功」错误码必须当作成功：

| 错误码 | 含义 |
| --- | --- |
| 304023 | PUSH_MSG_ASYNC_OK 推送消息异步调用成功，等待人工审核 |
| 304024 | REPLY_MSG_ASYNC_OK 回复消息异步调用成功，等待人工审核 |

## 四类处置

| 分类 | 触发 | 动作 |
| --- | --- | --- |
| `passive_fallback` | 被动回复上下文已失效 | 去掉 `msg_id`/`event_id`/`msg_seq`，回退为主动消息重发 |
| `duplicate` | 消息被去重（`40054005`） | 首次发送换一个 `msg_seq` 重发一次；仍失败直接抛错 |
| `transient` | 平台提示「请重试 / 请稍后重试」或短时服务异常 | 按 `3 × 2ⁿ` 秒指数退避重发 |
| `unreachable` | 已知不可达 / 需要人工处理 | 立即抛错，绝不重试 |

未知错误码按「不可重试」处理（fail-safe，绝不重复投递）。

### 1. 回退为主动消息

以下错误码表示 `msg_id` / `event_id` 已经不可用，去掉被动回复上下文后重发：

| 错误码 | 含义 |
| --- | --- |
| 304103 | 消息ID已过期，不能回复 |
| 304026 | MSG_ID 回复的消息 id 错误 |
| 304027 | MSG_EXPIRE 回复的消息过期 |
| 304028 | MSG_PROTECT 非 At 当前用户的消息不允许回复 |
| 40034005 | 回复消息msg_id已过期 |
| 40034024 | 请求参数msg_id无效或越权 |
| 40034025 | 请求参数event_id无效 |
| 40034026 | 请求参数event_id已过期 |
| 40034027 | 该事件不支持回复消息 |
| 40034128 | 被动回复时间或次数超限 |

回退只发生一次；如果消息本来就是主动消息，错误直接抛给调用方。回退成功后**不会**
占用被动回复次数（`ReplyLimiter` 只在真正以被动回复送达时记账）。

### 2. 消息被去重 `40054005`

`40054005`（消息被去重，文档建议「请确保每次请求使用不同的 msgseq 值」）单独处理：

1. **首次发送**遇到时，换一个新的 `msg_seq` 重发一次；
2. 仍返回 `40054005` 就**直接抛错**，不再回退为主动消息，也不继续重试；
3. 如果是在**重发过程中**（网络退避之后）遇到，立即停止：此时消息可能已经发出但 id 不可知，
   继续重试只会造成重复投递，因此同样对外抛错。

### 3. 网络原因的指数退避重发

`httpx` 返回超时、连接中断、协议错误或代理错误时，按指数退避重发：

| 第 n 次重发 | 等待 |
| --- | --- |
| 1 | 3 秒 |
| 2 | 6 秒 |
| 3 | 12 秒 |
| 4 | 24 秒 |
| 5 | 48 秒（会超出预算，不再等待） |

公式为 `3 × 2ⁿ⁻¹`，`MessageSendPolicy(backoff_base=...)` 可改基数。

**总时长上限 60 秒**：从第一次尝试开始计时，一旦下一次退避会越过预算（或单次尝试已经
让总时长越界且未成功），就停止并抛出 `MessageSendTimeoutError`（`TransportError` 的子类，
携带 `attempts` / `elapsed` / `timeout` / `last_code` / `cause`）。

只有**真正的网络失败**才退避重发。botpy 的 `TransportError` 也用于表达 SDK 自身状态
（Gateway 未就绪 `attempts=0`、重试被中止、客户端已关闭），这些不会被重发，避免无谓等待。

### 4. 立即抛错

`40034101`（机器人非群成员）、`40054002`（机器人被禁言）、`40054003`（机器人不是群成员）
等已知不可达的错误码，以及内容违规、参数错误、权限不足、配额类限频等确定性失败，都会立即
抛出 `ApiError`，不做任何重试。

## 覆盖率核对

`generated_docs/extract_send_errors.py` 从本地文档副本抓取：

- 群聊 / 单聊消息接口「错误码」表的全部错误码（48 个）
- 全局错误码表中与消息相关的编码段（`304xxx` / `4003xxx` / `4005xxx` / `5005xxx`，45 个）

并与运行时分类表比对。当前结果：**89 个错误码全部分类完毕，0 个未覆盖**。新增错误码可以
通过重跑该脚本发现（接口表里出现未分类错误码时脚本以非 0 退出）。

## 配置

```python
from botpy.protocol import MessageSendPolicy

client = MyClient(
    intents=intents,
    send_policy=MessageSendPolicy(
        total_timeout=60.0,   # 单次逻辑发送的总时长预算
        backoff_base=3.0,     # 指数退避基数：3、6、12、24……
        max_attempts=None,    # 可选硬上限，None 表示只受总时长约束
    ),
)
```

## 与既有重试逻辑的关系

| 层级 | 职责 |
| --- | --- |
| `ApiClient` | HTTP 层的安全重试（GET 等幂等方法、401 刷新、429/5xx 指数退避） |
| `MessageSendPolicy` | 业务层的 `err_code` 处置与消息发送退避重发，60 秒预算 |

POST 消息发送在 `ApiClient` 层依旧不会自动重放 5xx（避免非幂等重复），业务层的重发只由
`err_code` 语义和网络异常驱动。
