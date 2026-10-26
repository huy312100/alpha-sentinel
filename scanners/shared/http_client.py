"""One HTTP client for every scanner: rate-limited, retried, fail-closed.

Two responsibilities, both about *not* letting a vendor decide the outcome of a scan:

1. **Transport.** :func:`request_json` is the single entry point layers use. It
   delegates retries/backoff to :mod:`core.http` (which already knows not to
   replay a non-idempotent POST) and adds a per-host token bucket, so a free-tier
   API answering 429 cannot burn the whole scan window or trigger a ban.
2. **Containment.** :func:`fetch_provider` awaits one vendor read and converts any
   exception into a :class:`ProviderResult` carrying an error string. A layer can
   then keep the providers that answered and report ``DEGRADED`` - the pipeline
   never sees an exception from a provider, only a thinner layer.

The polling helpers (:func:`first_float`, :func:`first_int`, :func:`first_row`)
absorb the shape drift between vendor API revisions, which is the other half of
"one dead field must not empty a layer".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Mapping
from urllib.parse import urlparse

from core.http import RateLimiter, request_json as _core_request_json
from core.http import to_float, to_int
from core.logging import get_logger, redact, scrub_url_params

log = get_logger(__name__)

#: Conservative default for public/free tiers. Callers may raise it per call when
#: they own a paid key, or lower it for the strictest vendor.
DEFAULT_REQUESTS_PER_SECOND = 8.0

#: Live token buckets, one per host. Module-level on purpose: the limiter must
#: survive individual scans, otherwise every cycle gets a fresh burst.
_LIMITERS: dict[str, RateLimiter] = {}
#: The rate each live bucket was built with, so a stricter caller can replace it.
_RATES: dict[str, float] = {}


def limiter_for(url: str, rate_per_second: float = DEFAULT_REQUESTS_PER_SECOND) -> RateLimiter:
    """Return (and lazily create) the token bucket for ``url``'s host.

    One bucket per host, but the *strictest* caller wins: asking for a later,
    lower ``rate_per_second`` replaces the cached bucket instead of being ignored,
    otherwise the first caller's rate would silently govern every other one.
    """
    host = urlparse(url).netloc.lower() or "unknown"
    limiter = _LIMITERS.get(host)
    if limiter is None or rate_per_second < _RATES.get(host, rate_per_second):
        limiter = RateLimiter(rate_per_second, burst=max(1, int(rate_per_second)))
        _LIMITERS[host] = limiter
        _RATES[host] = rate_per_second
    return limiter


async def request_json(
    session: Any,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    method: str = "GET",
    json_body: Mapping[str, Any] | None = None,
    retries: int = 2,
    backoff: float = 1.5,
    idempotent: bool | None = None,
    rate_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
) -> Any:
    """Rate-limited JSON request. Signature-compatible with ``core.http``."""
    await limiter_for(url, rate_per_second).acquire()
    return await _core_request_json(
        session,
        url,
        params=params,
        headers=headers,
        method=method,
        json_body=json_body,
        retries=retries,
        backoff=backoff,
        idempotent=idempotent,
    )


async def get_json(
    session: Any,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    retries: int = 2,
    rate_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
) -> Any:
    """Convenience wrapper for the common read-only call."""
    return await request_json(
        session,
        url,
        params=params,
        headers=headers,
        method="GET",
        retries=retries,
        rate_per_second=rate_per_second,
    )


async def post_json(
    session: Any,
    url: str,
    *,
    json_body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    retries: int = 0,
    idempotent: bool = False,
    rate_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
) -> Any:
    """POST that is *not* retried unless the caller declares it replay-safe."""
    return await request_json(
        session,
        url,
        json_body=json_body,
        headers=headers,
        method="POST",
        retries=retries,
        idempotent=idempotent,
        rate_per_second=rate_per_second,
    )


# ---------------------------------------------------------------------------
# Fail-closed provider wrapper
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ProviderResult:
    """One vendor read, successful or not."""

    source: str
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.metrics)


async def fetch_provider(source: str, awaitable: Awaitable[dict[str, Any]]) -> ProviderResult:
    """Await one provider coroutine, converting any failure into an error field.

    The layer keeps whatever other providers returned; this is the seam where a
    vendor outage becomes a thinner signal instead of a failed scan.
    """
    try:
        metrics = await awaitable
    except Exception as exc:  # noqa: BLE001 - providers fail in creative ways
        # Both sinks matter: the log line and ``ProviderResult.error``, which is
        # stored on the signal and rendered by the dashboard. Vendor error text can
        # carry the request URL - and with it a query-string credential.
        message = f"{type(exc).__name__}: {redact(scrub_url_params(str(exc)))[:160]}"
        log.info("provider_failed", extra={"source": source, "error": message})
        return ProviderResult(source=source, error=message)
    if not isinstance(metrics, dict) or not metrics:
        return ProviderResult(source=source, error="no usable data")
    return ProviderResult(source=source, metrics=metrics)


# ---------------------------------------------------------------------------
# Response polling helpers (tolerant across API revisions)
# ---------------------------------------------------------------------------
def first_float(mapping: Any, *keys: str) -> float | None:
    """Return the first numeric value among ``keys``."""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def first_int(mapping: Any, *keys: str) -> int | None:
    """Integer counterpart of :func:`first_float` (truncates, never rounds up)."""
    value = first_float(mapping, *keys)
    return int(value) if value is not None else None


def first_row(payload: Any) -> list[Any]:
    """Extract the first list of rows from the common provider envelopes."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "result", "rows", "transfers", "results", "timeseries"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for inner in ("rows", "data", "list"):
                nested = value.get(inner)
                if isinstance(nested, list):
                    return nested
    return []


__all__ = [
    "DEFAULT_REQUESTS_PER_SECOND",
    "ProviderResult",
    "fetch_provider",
    "first_float",
    "first_int",
    "first_row",
    "get_json",
    "limiter_for",
    "post_json",
    "request_json",
    "to_float",
    "to_int",
]
