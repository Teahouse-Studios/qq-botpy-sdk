import asyncio
import logging
import unittest
from unittest.mock import patch

from botpy.client import Client
from botpy.flags import Intents
from botpy.logging import LoguruHandler, configure_loguru


class FakeLevel:
    def __init__(self, name):
        self.name = name


class FakeLoguru:
    def __init__(self, records=None, extra=None):
        self.records = records if records is not None else []
        self.extra = dict(extra or {})
        self.options = {}

    def level(self, name):
        return FakeLevel(name)

    def bind(self, **extra):
        return FakeLoguru(self.records, {**self.extra, **extra})

    def opt(self, **options):
        self.options = options
        return self

    def log(self, level, message):
        self.records.append((level, message, self.extra, self.options))


class LoguruCompatibilityTests(unittest.TestCase):
    def test_handler_forwards_rendered_message_exception_and_extra(self):
        target = FakeLoguru()
        handler = LoguruHandler(target)
        record = logging.LogRecord(
            "botpy.protocol",
            logging.WARNING,
            __file__,
            10,
            "retry %s",
            (3,),
            None,
        )
        record.request_id = "request-1"

        handler.emit(record)

        level, message, extra, options = target.records[0]
        self.assertEqual("WARNING", level)
        self.assertEqual("retry 3", message)
        self.assertEqual("botpy.protocol", extra["stdlib_logger"])
        self.assertEqual("request-1", extra["request_id"])
        self.assertIn("exception", options)

    def test_configure_loguru_can_bridge_a_named_standard_logger(self):
        target = FakeLoguru()
        name = "botpy.loguru-test"
        std_logger = logging.getLogger(name)
        previous_handlers = tuple(std_logger.handlers)
        previous_level = std_logger.level
        previous_propagate = std_logger.propagate
        try:
            configure_loguru(target, logger_name=name)
            std_logger.info("hello %s", "loguru")

            self.assertEqual("hello loguru", target.records[0][1])
            self.assertFalse(std_logger.propagate)
            self.assertIsInstance(std_logger.handlers[0], LoguruHandler)
        finally:
            for handler in tuple(std_logger.handlers):
                std_logger.removeHandler(handler)
                handler.close()
            for handler in previous_handlers:
                std_logger.addHandler(handler)
            std_logger.setLevel(previous_level)
            std_logger.propagate = previous_propagate

    def test_client_loguru_mode_disables_default_file_handler(self):
        target = FakeLoguru()
        with patch("botpy.client.logging.configure_logging") as configure_std, patch(
            "botpy.client.logging.configure_loguru"
        ) as configure_bridge:
            client = Client(Intents.none(), loguru_logger=target)

        self.assertFalse(configure_std.call_args.kwargs["ext_handlers"])
        configure_bridge.assert_called_once_with(target)
        if client._owns_loop:
            client.loop.close()
            asyncio.set_event_loop(None)


if __name__ == "__main__":
    unittest.main()
