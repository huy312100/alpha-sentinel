"""Structured logging must scrub secrets from fields and tracebacks, not just msg."""

from __future__ import annotations

import io
import json
import logging

from core import logging as app_logging


def _capture(json_output: bool) -> tuple[io.StringIO, logging.Logger]:
    stream = io.StringIO()
    app_logging.configure_logging("INFO", json_output=json_output, stream=stream)
    return stream, logging.getLogger("tests.redaction")


def _log_with_every_leak(logger: logging.Logger) -> None:
    try:
        raise RuntimeError("boom with supersecret-token in the traceback")
    except RuntimeError:
        logger.error(
            "send failed for supersecret-token",
            extra={"url": "https://api.telegram.org/botsupersecret-token/sendMessage"},
            exc_info=True,
        )


def test_json_output_redacts_message_extra_and_traceback():
    app_logging.clear_secrets()
    app_logging.register_secret("supersecret-token")
    stream, logger = _capture(json_output=True)
    try:
        _log_with_every_leak(logger)
        line = stream.getvalue().strip().splitlines()[-1]
    finally:
        app_logging.clear_secrets()

    assert "supersecret-token" not in line
    payload = json.loads(line)
    assert app_logging.REDACTED in payload["msg"]
    assert app_logging.REDACTED in payload["url"]
    assert app_logging.REDACTED in payload["exc"]


def test_console_output_redacts_tracebacks_too():
    app_logging.clear_secrets()
    app_logging.register_secret("supersecret-token")
    stream, logger = _capture(json_output=False)
    try:
        _log_with_every_leak(logger)
        text = stream.getvalue()
    finally:
        app_logging.clear_secrets()

    assert "supersecret-token" not in text
    assert app_logging.REDACTED in text


def test_short_values_are_not_registered():
    app_logging.clear_secrets()
    app_logging.register_secret("abc")
    assert app_logging.redact("abc is a common string") == "abc is a common string"
