"""Shared aiohttp plumbing: one session, bounded concurrency, honest retries.

Every scanner receives the *shared* session created here. Creating a session per
request is the single most common asyncio performance bug in scrapers - it
defeats connection pooling and leaks sockets under load.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Mapping

import aiohttp

from core.logging import get_logger
from core.models import utcnow

log = get_logger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
USER_AGENT = "AlphaSentinel/1.0 (+https://github.com/alpha-sentinel; research bot)"


class HttpError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


def build_session(*, timeout_seconds: float = 25.0, limit: int = 100) -> aiohttp.ClientSession:
    """Create the process-wide session. Caller owns its lifecycle."""
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=min(10.0, timeout_seconds / 2))
    connector = aiohttp.TCPConnector(limit=limit, ttl_dns_cache=300, enable_cleanup_closed=True)
    return aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        trust_env=True,
    )


async def request_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    method: str = "GET",
    json_body: Mapping[str, Any] | None = None,
    retries: int = 2,
    backoff: float = 1.5,
    idempotent: bool | None = None,
) -> Any:
    """GET/POST JSON with exponential backoff + jitter on transient failures.

    Non-retryable 4xx errors raise immediately so a bad API key fails fast
    instead of burning the scan window.

    Retries are limited to safe methods by default. A POST such as a Telegram
    ``sendMessage`` may already have been accepted before the client times out;
    replaying it would deliver the alert twice. Callers that know their POST is
    safe to replay (an LLM completion, for example) pass ``idempotent=True``.
    """
    verb = method.upper()
    retry_allowed = idempotent if idempotent is not None else verb in {"GET", "HEAD", "OPTIONS"}
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with session.request(
                method,
                url,
                params=params,
                headers=headers,
                json=json_body,
            ) as response:
                if response.status in RETRYABLE_STATUS:
                    body = await response.text()
                    raise HttpError(
                        f"transient HTTP {response.status} from {url}: {body[:200]}",
                        status=response.status,
                        url=url,
                    )
                if response.status >= 400:
                    body = await response.text()
                    raise HttpError(
                        f"HTTP {response.status} from {url}: {body[:200]}",
                        status=response.status,
                        url=url,
                    )
                return await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, HttpError, ValueError) as exc:
            last_error = exc
            retryable = not isinstance(exc, HttpError) or (exc.status in RETRYABLE_STATUS)
            if attempt >= retries or not retryable or not retry_allowed:
                break
            delay = backoff ** attempt + random.uniform(0, 0.3)
            log.warning(
                "http_retry",
                extra={"url": url, "attempt": attempt + 1, "delay_s": round(delay, 2), "error": str(exc)[:200]},
            )
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


def to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    number = to_float(value)
    return int(number) if number is not None else None


class RateLimiter:
    """Simple async token bucket, per-host. Prevents 429 storms from free tiers."""

    def __init__(self, rate_per_second: float, burst: int = 1) -> None:
        self._interval = 1.0 / max(rate_per_second, 0.001)
        self._burst = max(burst, 1)
        self._tokens = float(burst)
        self._updated = utcnow().timestamp()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = utcnow().timestamp()
            elapsed = now - self._updated
            self._tokens = min(self._burst, self._tokens + elapsed / self._interval)
            self._updated = now
            if self._tokens < 1.0:
                await asyncio.sleep((1.0 - self._tokens) * self._interval)
                self._tokens = 0.0
                self._updated = utcnow().timestamp()
            else:
                self._tokens -= 1.0


__all__ = [
    "HttpError",
    "RateLimiter",
    "USER_AGENT",
    "build_session",
    "request_json",
    "to_float",
    "to_int",
]
