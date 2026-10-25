"""Structured logging must scrub secrets from fields and tracebacks, not just msg."""

from __future__ import annotations

import io
import json
import logging

import pytest

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


@pytest.mark.parametrize("secret", [
    'token"with-quotes',
    "token\\with\\slashes",
    "token\nwith\ttabs",
    "token-雪🔑",
    "false, true",
    'ERROR", "logger": "tests.redaction',
])
@pytest.mark.parametrize("json_output", [True, False])
def test_redaction_preserves_escaped_values_and_json_structure(secret, json_output):
    class Unserializable:
        def __repr__(self):
            return secret

    app_logging.clear_secrets()
    app_logging.register_secret(secret)
    stream, logger = _capture(json_output=json_output)
    try:
        try:
            raise RuntimeError(secret)
        except RuntimeError:
            logger.error(
                "failed: %s", secret,
                extra={"details": {"tokens": [secret], "flags": [False, True], "count": 2},
                       "fallback": Unserializable()},
                exc_info=True,
            )
        text = stream.getvalue()
    finally:
        app_logging.clear_secrets()

    if json_output:
        payload = json.loads(text)
        assert payload["level"] == "ERROR"
        assert payload["logger"] == "tests.redaction"
        assert payload["msg"] == "failed: " + app_logging.REDACTED
        assert payload["details"] == {"tokens": [app_logging.REDACTED], "flags": [False, True], "count": 2}
        assert payload["fallback"] == app_logging.REDACTED
        assert secret not in payload["exc"]
        assert app_logging.REDACTED in payload["exc"]
    else:
        assert secret not in text
        assert app_logging.REDACTED in text


def test_json_formatter_redacts_metadata_and_extra_keys():
    secret = 'token"with\\escapes'
    record = logging.LogRecord(secret, logging.INFO, __file__, 1, "safe", (), None)
    setattr(record, secret, {"nested": [secret]})
    app_logging.clear_secrets()
    app_logging.register_secret(secret)
    try:
        payload = json.loads(app_logging.JsonFormatter().format(record))
    finally:
        app_logging.clear_secrets()

    assert payload["logger"] == app_logging.REDACTED
    assert payload[app_logging.REDACTED] == {"nested": [app_logging.REDACTED]}


def test_short_values_are_not_registered():
    app_logging.clear_secrets()
    app_logging.register_secret("abc")
    assert app_logging.redact("abc is a common string") == "abc is a common string"
