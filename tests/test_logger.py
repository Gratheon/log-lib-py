import io
import json
import pathlib
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from gratheon_log_lib import (
    LoggerConfig,
    OtlpConfig,
    bind_context,
    clear_context,
    create_logger,
    fatal,
    request_context,
)


class LoggerTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_context()

    def test_info_includes_context(self) -> None:
        logger = create_logger(LoggerConfig())
        bind_context(req_id="abc")
        stream = io.StringIO()
        with redirect_stdout(stream):
            logger.info("hello", {"user_id": "42"})

        output = stream.getvalue()
        self.assertIn("hello", output)
        self.assertIn('"req_id": "abc"', output)
        self.assertIn('"user_id": "42"', output)

    def test_debug_is_not_exported_to_otlp(self) -> None:
        logger = create_logger(LoggerConfig(otlp=OtlpConfig(enabled=True, endpoint="http://example.com")))
        with patch.object(logger, "_push_otlp") as push_otlp:
            logger.debug("debugging")
        push_otlp.assert_not_called()

    def test_error_enriched_prints_prefix(self) -> None:
        logger = create_logger(LoggerConfig())
        stream = io.StringIO()
        with redirect_stdout(stream):
            try:
                raise RuntimeError("boom")
            except RuntimeError as exc:
                logger.error_enriched("job failed", exc, {"job": "sync"})
        output = stream.getvalue()
        self.assertIn("job failed: boom", output)
        self.assertIn('"job": "sync"', output)

    def test_request_context_restores_previous_values(self) -> None:
        bind_context(req_id="outer")
        with request_context(req_id="inner", user_id="42"):
            self.assertEqual("inner", request_context_value("req_id"))
            self.assertEqual("42", request_context_value("user_id"))
        self.assertEqual("outer", request_context_value("req_id"))
        self.assertIsNone(request_context_value("user_id"))

    def test_otlp_payload_uses_resource_attributes(self) -> None:
        logger = create_logger(
            LoggerConfig(
                otlp=OtlpConfig(
                    enabled=True,
                    endpoint="http://collector:4318",
                    service="svc",
                    resource_attributes={"team": "platform"},
                )
            )
        )
        with patch("threading.Thread.start") as start_mock:
            logger.info("hello", {"trace_id": "abc", "span_id": "def"})
        start_mock.assert_called_once()
        payload = logger._otlp_payload_for_test("info", "hello", {}, "")
        body = json.loads(payload.decode("utf-8"))
        self.assertEqual("hello", body["body"])
        self.assertEqual("INFO", body["severity_text"])
        self.assertEqual(9, body["severity_number"])

    def test_fatal_raises_system_exit(self) -> None:
        with self.assertRaises(SystemExit):
            fatal("stop now")


def request_context_value(key: str):
    from gratheon_log_lib import get_context

    return get_context().get(key)


if __name__ == "__main__":
    unittest.main()
