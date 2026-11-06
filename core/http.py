"""Shared aiohttp plumbing: one session, bounded concurrency, honest retries.

Every scanner receives the *shared* session created here. Creating a session per
request is the single most common asyncio performance bug in scrapers - it
defeats connection pooling and leaks sockets under load.
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

import aiohttp

from core.logging import get_logger, scrub_url_params
from core.models import utcnow

log = get_logger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
#: Statuses that mean "you are over your window" rather than "the server is sad".
#: The distinction is what the retry is sized around: a 503 may clear in a second, a
#: 429 from a per-minute free tier will not, so the wait has to be the window.
THROTTLE_STATUS = frozenset({429, 503})
USER_AGENT = "AlphaSentinel/1.0 (+https://github.com/alpha-sentinel; research bot)"

#: A vendor's ``Retry-After`` may lengthen the wait, never shorten it below this
#: floor. Measured on GeckoTerminal: 19 of 40 requests in a burst were rejected and
#: *every one* of them said ``Retry-After: 0`` - a server can honestly mean "this
#: request may be retried" while still refusing it, because what it is actually
#: metering is a window. Honouring that literally is precisely the storm being
#: punished, so a throttle response always costs at least one estimated window.
#: The whole penalty is clamped to :data:`MAX_THROTTLE_WAIT_SECONDS` so one hostile
#: ``Retry-After: 86400`` cannot wedge an unattended daemon that is meant to keep
#: collecting.
MAX_THROTTLE_WAIT_SECONDS = 60.0
DEFAULT_THROTTLE_WAIT_SECONDS = 15.0


class HttpError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        #: Seconds the server asked us to wait, already clamped. ``None`` when the
        #: response said nothing, which is not the same as ``0``.
        self.retry_after = retry_after


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
    limiter: RateLimiter | None = None,
) -> Any:
    """GET/POST JSON with exponential backoff + jitter on transient failures.

    Non-retryable 4xx errors raise immediately so a bad API key fails fast
    instead of burning the scan window.

    Retries are limited to safe methods by default. A POST such as a Telegram
    ``sendMessage`` may already have been accepted before the client times out;
    replaying it would deliver the alert twice. Callers that know their POST is
    safe to replay (an LLM completion, for example) pass ``idempotent=True``.

    ``limiter`` is the caller's per-host token bucket. It is not used to space this
    request - the shared client acquires before calling - it is used so that a 429
    *teaches the bucket*: the penalty is recorded once, on shared state, and every
    other coroutine heading at the same host waits instead of discovering the closed
    window for itself one request at a time.
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
                        retry_after=(
                            parse_retry_after(response.headers.get("Retry-After"))
                            if response.status in THROTTLE_STATUS
                            else None
                        ),
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
            status = getattr(exc, "status", None)
            penalty = throttle_penalty(status, getattr(exc, "retry_after", None))
            # Record the window *before* deciding whether to try again. The attempt
            # that gives up is the one holding the information everyone else needs:
            # penalising only inside the retry path leaves a ``retries=0`` caller, or
            # a final 429, teaching the host nothing while it is still shut.
            if penalty is not None and limiter is not None:
                limiter.penalize(penalty)
            if attempt >= retries or not retryable or not retry_allowed:
                break
            delay = backoff ** attempt + random.uniform(0, 0.3)
            if penalty is not None:
                # A per-minute window does not reopen in 1.5 seconds.
                delay = max(delay, penalty)
            log.warning(
                "http_retry",
                extra={
                    "url": url,
                    "attempt": attempt + 1,
                    "delay_s": round(delay, 2),
                    # The difference between "transient" and "we are over our quota"
                    # has to be readable in the log, or a rate-limit problem looks
                    # exactly like a flaky vendor for as long as it takes to notice.
                    "throttle_s": round(penalty, 1) if penalty is not None else None,
                    # ``aiohttp`` puts the real URL (query included) in ``str(exc)``.
                    "error": scrub_url_params(str(exc))[:200],
                },
            )
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


def throttle_penalty(status: int | None, requested: float | None) -> float | None:
    """Seconds to close the host for, or ``None`` when nothing was throttled.

    Three cases, and the middle one is the reason this is a function rather than a
    status check:

    * **429** - a metered window, always. Even when the header says
      ``Retry-After: 0``: measured on GeckoTerminal, every one of 19 rejections in a
      40-request burst said 0 while still refusing, because the request *is*
      immediately retryable and the *window* is what says no. Honouring that literally
      restarts the storm the throttle exists to end.
    * **any response with a ``Retry-After``** - the server named a window; honour it.
    * **a bare 500/503** - a hiccup, and deliberately *not* a penalty. Closing a host
      for 15s because one request bounced would stall every concurrent caller of it
      on a fault nobody declared, which is the failure this whole change is meant to
      remove.
    """
    if status == 429:
        return max(DEFAULT_THROTTLE_WAIT_SECONDS, requested or 0.0)
    if requested is not None:
        return min(requested, MAX_THROTTLE_WAIT_SECONDS)
    return None


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


def parse_retry_after(value: Any) -> float | None:
    """Seconds from a ``Retry-After`` header: delta-seconds or an HTTP-date.

    Returns ``None`` for absent or unparseable values, so the caller keeps its own
    backoff rather than trusting a garbage header. Both forms are in the standard and
    free tiers use both: a bare ``"30"`` and a ``Wed, 21 Oct 2026 07:28:00 GMT``
    deadline. Clamped to :data:`MAX_THROTTLE_WAIT_SECONDS` in either case.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    number = to_float(text)
    if number is None:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:  # an HTTP-date is GMT by definition
            when = when.replace(tzinfo=timezone.utc)
        number = (when - utcnow()).total_seconds()
    return max(0.0, min(number, MAX_THROTTLE_WAIT_SECONDS))


class RateLimiter:
    """Simple async token bucket, per-host. Prevents 429 storms from free tiers."""

    def __init__(self, rate_per_second: float, burst: int = 1) -> None:
        self._interval = 1.0 / max(rate_per_second, 0.001)
        self._burst = max(burst, 1)
        self._tokens = float(burst)
        self._updated = utcnow().timestamp()
        self._lock = asyncio.Lock()
        #: Monotonic deadline set by a 429, shared by every caller of this host.
        self._blocked_until = 0.0

    def penalize(self, seconds: float) -> None:
        """Close the host for ``seconds``. Idempotent to the *longest* wait asked for.

        The first 429 is the only one that has to be discovered. Recording it here
        means the eleven other coroutines already heading at the same vendor wait for
        the window instead of each spending a retry budget learning it independently.
        Uses the monotonic clock: a vendor whose clock is wrong, or an NTP step, must
        not be able to make the bucket reopen early.
        """
        horizon = time.monotonic() + max(0.0, min(seconds, MAX_THROTTLE_WAIT_SECONDS))
        self._blocked_until = max(self._blocked_until, horizon)

    def reconfigure(self, rate_per_second: float, burst: int | None = None) -> None:
        """Change the pacing of *this* bucket, keeping the window it already holds.

        A per-host limiter is handed out to callers that keep the reference, so
        "replace it with a stricter one" would drop the penalty on the floor and let
        the old and new buckets issue to the same vendor concurrently - two buckets is
        not one rate limit. Mutating in place keeps one clock for the host.

        Deliberately not lock-guarded: the fields are independent floats read under
        the lock's own serialisation, a rate change happens at configuration time and
        not per request, and the worst case is one request paced at the interval that
        was current when it looked.
        """
        self._interval = 1.0 / max(rate_per_second, 0.001)
        requested = burst if burst is not None else max(1, int(rate_per_second))
        self._burst = max(requested, 1)
        # Never let a loosened burst keep more credit than the new ceiling allows.
        self._tokens = min(self._tokens, float(self._burst))

    @property
    def blocked_for(self) -> float:
        """Seconds this host is still closed for. Observability and tests, not logic."""
        return max(0.0, self._blocked_until - time.monotonic())

    async def _wait_for_window(self) -> None:
        blocked = self.blocked_for
        if blocked > 0:
            await asyncio.sleep(blocked)

    async def acquire(self) -> None:
        # Wait out any penalty *before* taking the lock: holding it while asleep
        # would serialise every waiter through one sleep each, turning a shared 60s
        # window into sixty of them.
        await self._wait_for_window()
        async with self._lock:
            # Re-check: another caller can have been penalised during the wait.
            await self._wait_for_window()
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
    "parse_retry_after",
    "request_json",
    "throttle_penalty",
    "to_float",
    "to_int",
]
