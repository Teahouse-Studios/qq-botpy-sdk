# -*- coding: utf-8 -*-

import os
import sys
import json
import inspect
import yaml
import logging
import logging.config
from typing import Any, Dict, List, Optional, Union
from logging.handlers import TimedRotatingFileHandler

LOG_COLORS_CONFIG = {
    "DEBUG": "cyan",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "red",
}

DEFAULT_LOGGER_NAME = "botpy"

DEFAULT_PRINT_FORMAT = "\033[1;33m[%(levelname)s]\t(%(filename)s:%(lineno)s)%(funcName)s\t\033[0m%(message)s"
DEFAULT_FILE_FORMAT = "%(asctime)s\t[%(levelname)s]\t(%(filename)s:%(lineno)s)%(funcName)s\t%(message)s"
logging.basicConfig(format=DEFAULT_PRINT_FORMAT)

DEFAULT_FILE_HANDLER = {
    # 要实例化的Handler
    "handler": TimedRotatingFileHandler,
    # 可选 Default to DEFAULT_FILE_FORMAT
    "format": "%(asctime)s\t[%(levelname)s]\t(%(filename)s:%(lineno)s)%(funcName)s\t%(message)s",
    # 可选 Default to DEBUG
    "level": logging.DEBUG,
    # 以下是Handler相关参数
    "when": "D",
    "backupCount": 7,
    "encoding": "utf-8",
    # *特殊* 对于filename参数，其中如有 %(name)s 会在实例化阶段填入相应的日志name
    "filename": os.path.join(os.getcwd(), "%(name)s.log"),
}

# 存放已经获取的Logger
logs: Dict[str, logging.Logger] = {}

# 追加的handler
_ext_handlers: List[dict] = []


_LOG_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)

# 解决Windows系统cmd运行日志输出不会显示颜色问题
os.system("")


class LoguruHandler(logging.Handler):
    """将标准库日志记录转发给 Loguru，且不强制依赖 ``loguru`` 包。"""

    def __init__(self, loguru_logger: Any = None, level: int = logging.NOTSET) -> None:
        super().__init__(level=level)
        if loguru_logger is None:
            try:
                from loguru import logger as loguru_logger
            except ImportError as exc:
                raise RuntimeError(
                    "Loguru is not installed; install it with `pip install loguru`"
                ) from exc
        self.loguru_logger = loguru_logger

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = self.loguru_logger.level(record.levelname).name
        except (AttributeError, ValueError, TypeError):
            level = record.levelno

        frame = inspect.currentframe()
        depth = 0
        while frame is not None and frame.f_code.co_filename in {__file__, logging.__file__}:
            frame = frame.f_back
            depth += 1

        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _LOG_RECORD_FIELDS and key not in {"message", "asctime"}
        }
        extra.setdefault("stdlib_logger", record.name)
        target = self.loguru_logger.bind(**extra) if hasattr(self.loguru_logger, "bind") else self.loguru_logger
        target.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_loguru(
    loguru_logger: Any = None,
    *,
    logger_name: Optional[str] = DEFAULT_LOGGER_NAME,
    level: int = logging.DEBUG,
    remove_handlers: bool = True,
    propagate: bool = False,
) -> LoguruHandler:
    """把 botpy 或根标准库 logger 转发到 Loguru。

    ``logger_name='botpy'`` 只接管 SDK 日志；传 ``None`` 时接管根 logger。
    Loguru 的 sink、格式、轮转与异步队列仍由下游应用自行配置。
    """

    global _ext_handlers

    target_logger = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    if remove_handlers:
        for handler in tuple(target_logger.handlers):
            target_logger.removeHandler(handler)
            handler.close()
        if logger_name == DEFAULT_LOGGER_NAME:
            _ext_handlers.clear()

    handler = LoguruHandler(loguru_logger, level=level)
    target_logger.addHandler(handler)
    target_logger.setLevel(level)
    target_logger.propagate = propagate if logger_name else False
    if logger_name:
        logs[logger_name] = target_logger
    return handler


def get_handler(handler, name=DEFAULT_LOGGER_NAME):
    """
    将handler字典实例化
    :param handler: handler配置
    :param name: 动态路径参数
    :return: Handler
    """
    handler = handler.copy()
    if "filename" in handler:
        handler["filename"] = handler["filename"] % {"name": name}

    lever = handler.get("level") or logging.DEBUG
    _format = handler.get("format") or DEFAULT_FILE_FORMAT

    for k in ["level", "format"]:
        if k in handler:
            handler.pop(k)

    handler = handler.pop("handler")(**handler)
    handler.setLevel(lever)
    handler.setFormatter(logging.Formatter(_format))
    return handler


def get_logger(name=None):
    global logs

    if not name:
        name = DEFAULT_LOGGER_NAME
    if name in logs:
        return logs[name]

    logger = logging.getLogger(name)
    # 从用户命令行接收是否打印debug日志
    argv = sys.argv
    if "-d" in argv or "--debug" in argv:
        logger.setLevel(level=logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    # 添加额外handler
    if _ext_handlers:
        for handler in _ext_handlers:
            logger.addHandler(get_handler(handler, name))

    logs[name] = logger
    return logger


def configure_logging(
        config: Union[str, dict] = None,
        _format: str = None,
        level: int = None,
        bot_log: Union[bool, None] = True,
        ext_handlers: Union[dict, List, bool] = None,
        force: bool = False
) -> None:
    """
    修改日志配置
    :param config: logging.config.dictConfig
    :param _format: logging.basicConfig(format=_format)
    :param level: 控制台输出level
    :param bot_log: 是否启用bot日志 True/启用 None/禁用拓展 False/禁用拓展+控制台输出
    :param ext_handlers: 额外的handler，格式参考 DEFAULT_FILE_HANDLER。Default to True(使用默认handler)
    :param force: 是否在已追加handler(_ext_handlers)不为空时继续追加(避免因多次实例化Client类导致重复添加)
    """
    global _ext_handlers

    if config is not None:
        if isinstance(config, dict):
            logging.config.dictConfig(config)
        elif config.endswith(".json"):
            with open(config) as file:
                loaded_config = json.load(file)
                logging.config.dictConfig(loaded_config)
        elif config.endswith((".yaml", ".yml")):
            with open(config) as file:
                loaded_config = yaml.safe_load(file)
                logging.config.dictConfig(loaded_config)
        else:
            # See the note about fileConfig() here:
            # https://docs.python.org/3/library/logging.config.html#configuration-file-format
            logging.config.fileConfig(
                config, disable_existing_loggers=False
            )

    if _format is not None:
        logging.basicConfig(format=_format, force=True)

    if level is not None:
        for name, logger in logs.items():
            logger.setLevel(level)

    if not bot_log:
        logger = logging.getLogger(DEFAULT_LOGGER_NAME)
        if bot_log is False:
            logger.propagate = False
        if DEFAULT_LOGGER_NAME in logs:
            logs.pop(DEFAULT_LOGGER_NAME)

        for handler in tuple(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    if ext_handlers and (not _ext_handlers or force):
        if ext_handlers is True:
            ext_handlers = [DEFAULT_FILE_HANDLER]
        elif not isinstance(ext_handlers, list):
            ext_handlers = [ext_handlers]

        _ext_handlers.extend(ext_handlers)

        for name, logger in logs.items():
            for handler in ext_handlers:
                logger.addHandler(get_handler(handler, name))
