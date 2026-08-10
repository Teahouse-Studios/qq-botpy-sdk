# Loguru 配置指南

botpy 内部继续使用标准库 `logging`，通过 `LoguruHandler` 将记录转发给下游 Loguru。这样不会把
Loguru 变成 SDK 的强制依赖，也不会要求协议层各模块持有同一个全局 Loguru 对象。

## 推荐配置

```bash
pip install loguru
```

```python
import sys

import botpy
from loguru import logger


logger.remove()
logger.configure(extra={"stdlib_logger": "app"})
logger.add(
    sys.stderr,
    level="INFO",
    enqueue=True,
    backtrace=True,
    diagnose=False,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{extra[stdlib_logger]}</cyan> | <level>{message}</level>"
    ),
)
logger.add(
    "logs/bot-{time:YYYY-MM-DD}.log",
    rotation="00:00",
    retention="14 days",
    compression="zip",
    enqueue=True,
    encoding="utf-8",
)


client = botpy.Client(
    intents=botpy.Intents(public_messages=True),
    loguru_logger=logger,
)
```

提供 `loguru_logger` 后，`Client` 不会再启用默认的 `botpy.log` 文件 handler，避免同一条日志同时由
标准库和 Loguru 写入。Loguru 的 sink、级别、轮转、保留时间和异步队列完全由下游配置。

## 已有 Client 或多 Client 项目

也可以在创建 Client 前全局桥接：

```python
import botpy
from loguru import logger

botpy.configure_loguru(logger)

client_a = botpy.Client(intents_a, ext_handlers=False)
client_b = botpy.Client(intents_b, ext_handlers=False)
```

`configure_loguru()` 默认只接管 `botpy` logger，并设置 `propagate=False`，不会拦截 aiohttp、应用代码或
其他第三方库。

## 接管所有标准库日志

如果应用希望把所有标准库 logging 都交给 Loguru：

```python
botpy.configure_loguru(logger, logger_name=None)
client = botpy.Client(intents, ext_handlers=False)
```

这种模式会接管根 logger。建议同时检查 uvicorn、gunicorn、aiohttp 等框架是否安装了自己的 handler，
避免框架重复输出。

## 结构化上下文

标准库 `extra` 字段会传给 Loguru 的 `bind()`，原始 logger 名保存在 `stdlib_logger`：

```python
import logging

logging.getLogger("botpy.worker").info(
    "任务开始",
    extra={"request_id": "req-123", "account_id": "bot-a"},
)
```

Loguru 格式可以使用 `{extra[stdlib_logger]}`。对于不是每条日志都存在的自定义字段，建议使用
`logger.configure(extra={"request_id": "-", "account_id": "-"})` 设置默认值，或者使用自定义 format
函数安全读取。

## 异常与自定义级别

- `exc_info` 会转发为 Loguru 的 `exception`，异常堆栈不会丢失。
- DEBUG、INFO、WARNING、ERROR、CRITICAL 会按名称映射。
- Loguru 未注册的标准库自定义级别会按数字级别转发。
- `%s` 参数会先由标准库渲染，Loguru 收到的是最终消息文本。

## 不使用 Client 的底层协议对象

`ApiClient`、`TokenManager`、Session Store 等底层对象仍接受 logger 参数，可以直接传标准库 logger。
如果希望它们也进入 Loguru，使用由 `configure_loguru()` 接管的标准库 logger：

```python
import logging

botpy.configure_loguru(logger)
protocol_logger = logging.getLogger("botpy.protocol.custom")
api_client = ApiClient(token_manager, logger=protocol_logger)
```
