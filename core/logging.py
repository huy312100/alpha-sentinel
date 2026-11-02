"""Structured logging with mandatory secret redaction.

Every log line is JSON so it can be shipped to Loki/CloudWatch/Datadog without a
parser. A filter walks the *rendered* message and replaces any registered secret
with ``***REDACTED***`` - defence in depth against the classic
``logger.info("calling %s", settings.endpoints.url("telegram.send_message", ...))``
leak.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Set

REDACTED = "***REDACTED***"
_SECRETS: Set[str] = set()
_JSON_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
#: ``scheme://host/path`` followed by a query string or fragment. Closing brackets
#: are excluded so a URL wrapped in prose keeps its punctuation.
_URL_WITH_QUERY_RE = re.compile(
    r"(?i)\b(https?://[^\s'\"<>?#()\[\]{}]+)[?#][^\s'\"<>()\[\]{}]*"
)
_STANDARD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "thread", "threadName", "taskName",
    }
)
_CONFIGURED = False


def register_secret(value: str | None) -> None:
    """Remember a secret so its every occurrence is scrubbed from logs."""
    if not value:
        return
    value = value.strip()
    # Very short values ("1", "auto") would redact unrelated text.
    if len(value) < 6:
        return
    _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def scrub_url_params(text: str) -> str:
    """Drop query strings and fragments from every URL inside ``text``.

    Several vendors authenticate with a query parameter (CryptoPanic's
    ``auth_token``) and ``aiohttp`` exception text embeds the *real* URL, query
    included. Provider errors are both logged and stored on the signal, so the
    query is removed before either happens; the host and path that make the error
    actionable survive. This complements :func:`redact`, which can only scrub
    values it was told about and misses a truncated URL.
    """
    return _URL_WITH_QUERY_RE.sub(rf"\1?{REDACTED}", text)


def _redact_json_strings(text: str) -> str:
    """Scrub decoded JSON strings without replacing syntax or scalar values."""
    if not _SECRETS:
        return text
    return _JSON_STRING_RE.sub(
        lambda match: json.dumps(redact(json.loads(match.group())), ensure_ascii=False),
        text,
    )


def clear_secrets() -> None:
    """Test helper."""
    _SECRETS.clear()


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - stdlib signature
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed %-formatting
            return True
        scrubbed = redact(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    """Compact single-line JSON. ``extra={...}`` keys are merged at top level."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                try:
                    encoded = json.dumps(value, ensure_ascii=False)
                    payload[key] = json.loads(_redact_json_strings(encoded))
                except (TypeError, ValueError):
                    payload[key] = redact(repr(value))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        # Also scrub metadata and keys in the final line, decoding escaped strings
        # so secrets containing quotes, backslashes or newlines cannot leak.
        return _redact_json_strings(json.dumps(payload, ensure_ascii=False, default=str))


class ConsoleFormatter(logging.Formatter):
    """Human-friendly fallback for local development."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)-28s %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


#: Levels an operator may name in ``LOG_LEVEL``. Anything else falls back.
_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def env_log_level(default: str = "INFO") -> str:
    """The ``LOG_LEVEL`` environment setting, or ``default`` if it is unset or bogus.

    A typo must not stop a service, and it must not be raised either - logging is
    configured before anything can log, so the only honest channel is the level
    itself. Falling back to INFO is what happened before this function existed, and
    it is what will keep happening; the difference is that a correct value now works.
    """
    value = os.getenv("LOG_LEVEL", "").strip().upper()
    return value if value in _LOG_LEVELS else default


def configure_logging(level: str | None = None, json_output: bool | None = None, stream: Any = None) -> None:
    """Idempotent root-logger setup. Safe to call from bot, CLI and dashboard.

    Both settings resolve the same way: an explicit argument wins, otherwise the
    environment decides. Leaving ``level`` as ``None`` is therefore meaningful - a
    caller that passes ``"INFO"`` pins the level and silences ``LOG_LEVEL``, exactly
    as passing ``json_output=False`` once silenced ``LOG_JSON`` on the daemon.
    """
    global _CONFIGURED

    if level is None:
        level = env_log_level()

    if json_output is None:
        json_output = os.getenv("LOG_JSON", "true").strip().lower() not in {"0", "false", "no"}

    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else ConsoleFormatter())
    handler.addFilter(SecretRedactionFilter())
    root.addHandler(handler)

    # Third-party noise control.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)


@contextmanager
def log_duration(logger: logging.Logger, event: str, **fields: Any) -> Iterator[Dict[str, Any]]:
    """``with log_duration(log, "fetch", layer="dex") as meta: ...`` adds latency_ms."""
    started = time.perf_counter()
    try:
        yield fields
    finally:
        fields["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        logger.info(event, extra=fields)


__all__ = [
    "ConsoleFormatter",
    "JsonFormatter",
    "REDACTED",
    "SecretRedactionFilter",
    "clear_secrets",
    "configure_logging",
    "get_logger",
    "log_duration",
    "redact",
    "register_secret",
    "scrub_url_params",
]
