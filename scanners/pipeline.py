"""Layer coordinator: fan out every enabled layer, merge, and attach hard evidence.

The pipeline is deliberately dumb about *decisions* - it only collects. Everything
that can veto or size a position happens later (deterministic risk floor, then the
dual-agent audit). What it does guarantee is:

* one HTTP session and one settings object for the whole cycle;
* every layer is isolated: a provider that hangs or explodes yields an
  ``UNAVAILABLE`` signal instead of taking the scan down;
* the security screen runs *before* the LLMs, so a honeypot never costs tokens;
* market data is fetched once and shared by the DEX and technicals layers.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import aiohttp

from config.settings import Settings
from core.http import build_session
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import LayerSignal, SignalStatus, TokenSnapshot, json_safe, utcnow
from core.scoring import weighted_prior
from scanners.base import BaseScanner, ScanContext
from scanners.crypto.layer4_dex import DexMarket, WashTradingFilter, load_market
from scanners.crypto.layer8_security import fetch_security_report
from scanners.registry import (
    ASSET_LAYER_MAP,
    SCANNER_REGISTRY,
    build_scanners,
    run_isolated,
    validate_enabled,
)
from scanners.shared.listed.macro import (
    CRYPTO_CACHE_KEY,
    CROSS_ASSET_CACHE_KEY,
    cross_asset_usable,
    crypto_backdrop_usable,
    fetch_cross_asset_backdrop,
    fetch_crypto_backdrop,
)
from scanners.shared.market_data import MarketQuote, load_quote


log = get_logger(__name__)


#: Market-wide reads are shared across a daemon's cycles, but not forever: a
#: stale macro backdrop silently biases every instrument in the next run.
MACRO_CACHE_TTL_SECONDS = 300.0


@dataclass(slots=True)
class CollectedSnapshot:
    """A token plus its layer signals and the deterministic prior they imply."""

    snapshot: TokenSnapshot
    prior_score: float = 50.0
    duration_ms: float = 0.0
    layer_status: dict[str, str] = field(default_factory=dict)

    @property
    def token_address(self) -> str:
        return self.snapshot.token_address

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.snapshot.token_address,
            "chain": self.snapshot.chain,
            "asset_class": self.snapshot.asset_class,
            "symbol": self.snapshot.symbol,
            "prior_score": round(self.prior_score, 2),
            "duration_ms": self.duration_ms,
            "layer_status": dict(self.layer_status),
            "snapshot": json_safe(self.snapshot.to_prompt_payload()),
        }


@dataclass(slots=True)
class ScannerHealth:
    """Rolling per-layer health, surfaced on the dashboard and in bot status."""

    layer: str
    runs: int = 0
    ok: int = 0
    degraded: int = 0
    unavailable: int = 0
    skipped: int = 0
    last_error: str | None = None
    last_duration_ms: float = 0.0

    def record(self, signal: LayerSignal) -> None:
        self.runs += 1
        self.last_duration_ms = signal.latency_ms
        if signal.status is SignalStatus.OK:
            self.ok += 1
        elif signal.status is SignalStatus.DEGRADED:
            self.degraded += 1
        elif signal.status is SignalStatus.UNAVAILABLE:
            self.unavailable += 1
            self.last_error = signal.error
        else:
            self.skipped += 1

    @property
    def success_rate(self) -> float:
        usable = self.ok + self.degraded
        return round(usable / self.runs, 3) if self.runs else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "runs": self.runs,
            "ok": self.ok,
            "degraded": self.degraded,
            "unavailable": self.unavailable,
            "skipped": self.skipped,
            "success_rate": self.success_rate,
            "last_error": self.last_error,
            "last_duration_ms": self.last_duration_ms,
        }


class ScanPipeline:
    """Collects every enabled layer for a token, in parallel, without ever raising."""

    def __init__(
        self,
        settings: Settings,
        *,
        session: aiohttp.ClientSession | None = None,
        wash_threshold: float | None = None,
    ) -> None:
        self.settings = settings
        self._session = session
        self._owns_session = session is None
        self._enabled_layers = settings.scanners.enabled_layers
        # Fail fast at construction: a typo in ENABLED_LAYERS must not silently
        # delete a layer from every future scan.
        validate_enabled(self._enabled_layers)
        self._scanner_cache: dict[AssetClass, list[BaseScanner]] = {}
        #: Backwards-compatible default view; execution is routed per asset class.
        self.scanners: list[BaseScanner] = self.scanners_for(AssetClass.CRYPTO)
        self.wash = WashTradingFilter(wash_threshold or 85.0)
        self.health: dict[str, ScannerHealth] = {
            layer: ScannerHealth(layer)
            for layer in sorted(
                {spec.layer.value for specs in ASSET_LAYER_MAP.values() for spec in specs}
            )
        }

        #: Market-wide reads (``macro:crypto`` / ``macro:cross_asset``) reused by
        #: every instrument in a cycle. Values are ``(monotonic_stamp, data)`` and
        #: only *usable* reads are stored.
        self._macro_cache: dict[str, tuple[float, Any]] = {}
        self._macro_lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------------
    async def __aenter__(self) -> "ScanPipeline":
        if self._session is None:
            self._session = build_session(timeout_seconds=self.settings.scanners.http_timeout_seconds)
            self._owns_session = True
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = build_session(timeout_seconds=self.settings.scanners.http_timeout_seconds)
            self._owns_session = True
        return self._session

    # -- construction --------------------------------------------------------
    def scanners_for(self, asset_class: AssetClass | str) -> list[BaseScanner]:
        """Build (once) the enabled layers for ``asset_class``."""
        kind = AssetClass.coerce(asset_class)
        if kind not in self._scanner_cache:
            self._scanner_cache[kind] = build_scanners(
                self.settings, kind, enabled=self._enabled_layers
            )
        return self._scanner_cache[kind]

    # -- collection ----------------------------------------------------------
    async def collect(self, token: TokenSnapshot) -> CollectedSnapshot:
        started = time.perf_counter()
        ctx = ScanContext(session=self.session, settings=self.settings)
        now = time.monotonic()
        for key, (stored_at, value) in list(self._macro_cache.items()):
            # Market-wide read: identical for every instrument in the cycle, but
            # only while it is still fresh.
            if now - stored_at <= MACRO_CACHE_TTL_SECONDS:
                ctx.set_shared(key, value)

        snapshot = token
        snapshot.collected_at = utcnow()
        asset_class = AssetClass.coerce(snapshot.asset_class)

        if asset_class.is_crypto:
            await self._collect_crypto_market(snapshot, ctx)
        else:
            await self._collect_listed_market(snapshot, ctx)

        # 4) Every layer routed for this asset class, concurrently and isolated.
        scanners = self.scanners_for(asset_class)
        signals = await asyncio.gather(
            *(run_isolated(scanner, snapshot, ctx) for scanner in scanners)
        )
        for signal in signals:
            self.health.setdefault(signal.layer.value, ScannerHealth(signal.layer.value)).record(signal)
        snapshot.signals = list(signals)

        for key, value in ctx.shared.items():
            if not key.startswith("macro"):
                continue
            usable = crypto_backdrop_usable(value) if key == CRYPTO_CACHE_KEY else cross_asset_usable(value)
            if usable:
                self._macro_cache[key] = (time.monotonic(), value)

        prior = weighted_prior(snapshot.signals)
        duration = round((time.perf_counter() - started) * 1000.0, 1)
        critical = snapshot.security.critical_flags()
        snapshot.meta.update(
            {
                "prior_score": round(prior, 2),
                "collection_ms": duration,
                "security_critical_flags": critical,
                # The hard veto is enforced by ``core.scoring.reconcile`` and the
                # risk agent's short-circuit; this flag makes it visible to the UI.
                "security_veto": bool(critical),
                "wash_suspicion": snapshot.wash.suspicion_score if snapshot.wash.evaluated else None,
            }
        )
        result = CollectedSnapshot(
            snapshot=snapshot,
            prior_score=round(prior, 2),
            duration_ms=duration,
            layer_status={signal.layer.value: signal.status.value for signal in snapshot.signals},
        )
        log.info(
            "collected_snapshot",
            extra={
                "token": snapshot.token_address,
                "chain": snapshot.chain,
                "asset_class": snapshot.asset_class,
                "prior": result.prior_score,
                "duration_ms": duration,
                "layers": result.layer_status,
            },
        )
        return result

    # -- helpers -------------------------------------------------------------
    async def _collect_crypto_market(self, snapshot: TokenSnapshot, ctx: ScanContext) -> None:
        """Contract security, DEX market and wash read - crypto-only stages.

        A honeypot must never reach the LLMs, so the security screen runs before
        anything else. Every stage degrades in isolation: an outage leaves the
        snapshot thinner, never broken.
        """
        try:
            snapshot.security = await fetch_security_report(snapshot, ctx)
        except Exception as exc:  # noqa: BLE001 - scan() already degrades, belt and braces
            log.debug("security_stage_failed", extra={"token": snapshot.token_address, "error": str(exc)[:200]})

        market: DexMarket | None = None
        try:
            market = await load_market(snapshot, ctx)
        except Exception as exc:  # noqa: BLE001
            log.debug("market_stage_failed", extra={"token": snapshot.token_address, "error": str(exc)[:200]})
        self._apply_market(snapshot, market)

        try:
            pool = (market.pair_address if market else None) or snapshot.pair_address
            snapshot.wash = await self.wash.evaluate(snapshot, ctx, pool)
        except Exception as exc:  # noqa: BLE001
            log.debug("wash_stage_failed", extra={"token": snapshot.token_address, "error": str(exc)[:200]})

    async def _collect_listed_market(self, snapshot: TokenSnapshot, ctx: ScanContext) -> None:
        """One venue quote for a listed instrument, shared with technicals."""
        try:
            quote = await load_quote(snapshot, ctx)
        except Exception as exc:  # noqa: BLE001 - provider outage degrades, never aborts
            log.debug(
                "quote_stage_failed",
                extra={"instrument": snapshot.token_address, "error": f"{type(exc).__name__}: {str(exc)[:200]}"},
            )
            return
        self._apply_quote(snapshot, quote)

    @staticmethod
    def _apply_market(snapshot: TokenSnapshot, market: DexMarket | None) -> None:
        """Copy the merged DEX read onto the snapshot the LLMs and UI consume."""
        if market is None:
            return
        snapshot.pair_address = market.pair_address or snapshot.pair_address
        snapshot.price_usd = market.price_usd
        snapshot.liquidity_usd = market.liquidity_usd
        snapshot.volume_24h_usd = market.volume_24h_usd
        snapshot.volume_1h_usd = market.volume_1h_usd
        snapshot.price_change_24h_pct = market.price_change_24h_pct
        snapshot.price_change_1h_pct = market.price_change_1h_pct
        snapshot.fdv_usd = market.fdv_usd
        snapshot.market_cap_usd = market.market_cap_usd or snapshot.market_cap_usd
        snapshot.age_hours = market.age_hours or snapshot.age_hours
        snapshot.symbol = snapshot.symbol or market.base_symbol
        snapshot.name = snapshot.name or market.base_symbol

    @staticmethod
    def _apply_quote(snapshot: TokenSnapshot, quote: MarketQuote) -> None:
        """Copy a venue quote onto the snapshot (the non-crypto equivalent)."""
        if quote.price is not None:
            snapshot.price_usd = quote.price
        if quote.dollar_volume is not None:
            snapshot.volume_24h_usd = quote.dollar_volume
        if quote.change_pct is not None:
            snapshot.price_change_24h_pct = quote.change_pct
        snapshot.symbol = snapshot.symbol or quote.symbol
        snapshot.name = snapshot.name or quote.name
        snapshot.meta.update(
            {
                "venue_name": quote.venue,
                "currency": quote.currency,
                "provider_symbol": quote.provider_symbol,
                "instrument_type": quote.instrument_type,
                "fifty_two_week_high": quote.fifty_two_week_high,
                "fifty_two_week_low": quote.fifty_two_week_low,
            }
        )

    async def _prefetch_macro(self, tokens: Iterable[TokenSnapshot]) -> None:
        """Fetch each market-wide read once per cycle, before the parallel fan-out.

        ``collect_many`` starts several collections concurrently; without this,
        every cache miss copies the same empty cache and the whole batch fires
        duplicate CoinGecko/Yahoo requests.
        """
        needs: set[str] = set()
        for token in tokens:
            try:
                asset_class = AssetClass.coerce(token.asset_class)
            except Exception:  # noqa: BLE001 - unknown class -> let the scanner skip it
                continue
            needs.add("crypto" if asset_class.is_crypto else "cross_asset")
        if not needs:
            return
        async with self._macro_lock:
            for kind in sorted(needs):
                key = CRYPTO_CACHE_KEY if kind == "crypto" else CROSS_ASSET_CACHE_KEY
                cached = self._macro_cache.get(key)
                if cached is not None and (time.monotonic() - cached[0]) <= MACRO_CACHE_TTL_SECONDS:
                    continue
                try:
                    if kind == "crypto":
                        value = await fetch_crypto_backdrop(
                            self.session, endpoints=self.settings.endpoints
                        )
                        usable = crypto_backdrop_usable(value)
                    else:
                        value = await fetch_cross_asset_backdrop(
                            self.session, endpoints=self.settings.endpoints
                        )
                        usable = cross_asset_usable(value)
                except Exception as exc:  # noqa: BLE001 - the layer degrades to UNAVAILABLE
                    log.warning(
                        "macro_prefetch_failed",
                        extra={"kind": kind, "error": f"{type(exc).__name__}: {str(exc)[:200]}"},
                    )
                    continue
                if usable:
                    self._macro_cache[key] = (time.monotonic(), value)
                else:
                    log.warning("macro_prefetch_unusable", extra={"kind": kind})

    async def collect_many(
        self,
        tokens: Iterable[TokenSnapshot],
        *,
        concurrency: int | None = None,
    ) -> list[CollectedSnapshot]:
        """Collect a watchlist batch, bounded by ``TOKEN_CONCURRENCY``."""
        items = list(tokens)
        if not items:
            return []
        await self._prefetch_macro(items)
        limit = max(1, concurrency or self.settings.scanners.token_concurrency)
        semaphore = asyncio.Semaphore(limit)

        async def one(token: TokenSnapshot) -> CollectedSnapshot | None:
            async with semaphore:
                try:
                    return await self.collect(token)
                except Exception as exc:  # noqa: BLE001 - one bad token must not stop the cycle
                    log.error(
                        "token_collection_failed",
                        extra={"token": token.token_address, "error": f"{type(exc).__name__}: {str(exc)[:200]}"},
                    )
                    return None

        results = await asyncio.gather(*(one(token) for token in items))
        collected = [result for result in results if result is not None]
        log.info(
            "batch_collected",
            extra={"requested": len(items), "collected": len(collected), "concurrency": limit},
        )
        return collected

    def health_report(self) -> dict[str, Any]:
        """Per-layer success rates - the first thing to check when scores look odd."""
        return {
            "layers": {name: health.to_dict() for name, health in sorted(self.health.items())},
            "macro_cached": bool(self._macro_cache),
            "macro_keys": sorted(self._macro_cache),
        }


__all__ = [
    "CollectedSnapshot",
    "MACRO_CACHE_TTL_SECONDS",
    "SCANNER_REGISTRY",
    "ScanPipeline",
    "ScannerHealth",
]
