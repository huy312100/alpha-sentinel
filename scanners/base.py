"""Scanner plumbing shared by every data layer.

Every layer obeys the same contract:

* one :class:`BaseScanner` subclass, one ``collect()`` coroutine;
* **never raise** - a provider outage degrades one layer, never the scan;
* always return a :class:`LayerSignal` whose ``status`` says whether the score is
  trustworthy (``OK``), usable-but-thin (``DEGRADED``), missing (``UNAVAILABLE``)
  or not applicable (``SKIPPED``). The scoring engine only consumes contributing
  signals, so an outage can never silently look like a neutral 50.
"""

from __future__ import annotations

import abc
import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from config.settings import Settings
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import Layer, LayerSignal, SignalStatus, TokenSnapshot, clamp

log = get_logger(__name__)


@dataclass(slots=True)
class ScanContext:
    """Shared, request-scoped state: one HTTP session, one settings object."""

    session: aiohttp.ClientSession
    settings: Settings
    shared: dict[str, Any] = field(default_factory=dict)

    def get_shared(self, key: str) -> Any:
        return self.shared.get(key)

    def set_shared(self, key: str, value: Any) -> None:
        self.shared[key] = value


class BaseScanner(abc.ABC):
    """Base class for one data layer."""

    layer: Layer
    weight: float = 1.0
    timeout_seconds: float | None = None
    #: Asset classes this layer can describe. ``None`` means all of them.
    #: A scanner returns ``SKIPPED`` (zero weight) for anything outside its set,
    #: so a layer that does not apply never drags the prior toward neutral.
    asset_classes: frozenset[AssetClass] | None = None

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- contract ----------------------------------------------------------
    def supports(self, token: TokenSnapshot) -> bool:
        """Whether this layer has anything to say about ``token``."""
        if self.asset_classes is None:
            return True
        try:
            return AssetClass.coerce(token.asset_class) in self.asset_classes
        except ValueError:
            return False

    @abc.abstractmethod
    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:
        """Fetch and score this layer. May raise; :meth:`run` contains it."""

    async def run(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:
        if not self.supports(token):
            return self.skipped(token, f"not applicable to {token.asset_class}")
        started = time.perf_counter()
        timeout = self.timeout_seconds or self.settings.scanners.layer_timeout_seconds
        try:
            signal = await asyncio.wait_for(self.collect(token, ctx), timeout=timeout)
        except asyncio.TimeoutError:
            return self.unavailable(token, f"layer timed out after {timeout:.0f}s", started)
        except Exception as exc:  # noqa: BLE001 - providers fail in creative ways
            log.warning(
                "layer_failed",
                extra={
                    "layer": self.layer.value,
                    "token": token.token_address,
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                },
            )
            return self.unavailable(token, f"{type(exc).__name__}: {str(exc)[:200]}", started)
        signal.latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
        signal.weight = self.weight
        return signal

    # -- builders ----------------------------------------------------------
    def signal(
        self,
        token: TokenSnapshot,
        *,
        score: float,
        confidence: float,
        summary: str,
        status: SignalStatus = SignalStatus.OK,
        metrics: dict[str, Any] | None = None,
        flags: list[str] | None = None,
    ) -> LayerSignal:
        return LayerSignal(
            layer=self.layer,
            score=round(clamp(score), 2),
            confidence=round(clamp(confidence, 0.0, 1.0), 3),
            weight=self.weight,
            status=status,
            summary=summary[:500],
            metrics=metrics or {},
            flags=flags or [],
        )

    def unavailable(self, token: TokenSnapshot, reason: str, started: float = 0.0) -> LayerSignal:
        latency = round((time.perf_counter() - started) * 1000.0, 1) if started else 0.0
        return LayerSignal(
            layer=self.layer,
            score=50.0,
            confidence=0.0,
            weight=self.weight,
            status=SignalStatus.UNAVAILABLE,
            summary=f"{self.layer.value} unavailable: {reason}"[:500],
            error=reason[:500],
            latency_ms=latency,
        )

    def skipped(self, token: TokenSnapshot, reason: str) -> LayerSignal:
        """Not applicable / not configured: excluded from scoring by design."""
        return LayerSignal(
            layer=self.layer,
            score=50.0,
            confidence=0.0,
            weight=0.0,
            status=SignalStatus.SKIPPED,
            summary=f"{self.layer.value} skipped: {reason}"[:500],
        )


# ---------------------------------------------------------------------------
# Score-shaping helpers
# ---------------------------------------------------------------------------
def linear(value: float | None, low: float, high: float) -> float:
    """Map ``low..high`` onto ``0..100`` with clamping."""
    if value is None or high == low:
        return 50.0
    return clamp((value - low) / (high - low) * 100.0)


def log_scale(value: float | None, low: float, high: float) -> float:
    """Log-linear mapping - right for liquidity/volume, which span decades."""
    if value is None or value <= 0 or low <= 0 or high <= low:
        return 0.0 if value is not None and value <= 0 else 50.0
    return clamp(math.log10(value / low) / math.log10(high / low) * 100.0)


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def mean_of(values: list[float]) -> float:
    return sum(values) / len(values) if values else 50.0


class StubScanner(BaseScanner):
    """Placeholder for a class-specific layer that is routed but not implemented.

    A stub is *not* an error: it returns ``SKIPPED`` with zero weight, so routing
    for a market can be wired today and the module can grow into a real scanner
    without touching the registry, the pipeline or the scoring engine. Subclasses
    declare the ``asset_classes`` they will eventually describe.
    """

    layer = Layer.FUNDAMENTALS
    capability = "class-specific data"

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:
        return self.skipped(token, f"{self.capability} is not implemented for {token.asset_class} yet")


#: Historic name kept so older imports keep working.
ListedStubScanner = StubScanner


__all__ = [
    "BaseScanner",
    "ListedStubScanner",
    "ScanContext",
    "StubScanner",
    "linear",
    "log_scale",
    "mean_of",
    "safe_div",
]
