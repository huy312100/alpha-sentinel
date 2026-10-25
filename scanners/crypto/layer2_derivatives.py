"""Layer 2 - derivatives and market depth.

Primary reads are keyless: **Hyperliquid** publishes per-asset funding, open
interest and mark price, and Binance/Bybit remain the fallback for funding and
taker flow. **Coinglass** adds aggregated open interest and the liquidation map
when a key is configured.

The layer is deliberately contrarian at extremes: crowded longs with rising open
interest are how good spot setups turn into liquidation cascades.
"""

from __future__ import annotations

import asyncio
from typing import Any

from config.settings import Settings
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import Layer, LayerSignal, SignalStatus, TokenSnapshot, clamp
from scanners.base import BaseScanner, ScanContext, linear, log_scale, safe_div
from scanners.derivatives import DerivativesScanner, funding_score, perp_symbol_candidates

from scanners.shared.http_client import ProviderResult, fetch_provider, first_float, first_row, request_json, to_float

log = get_logger(__name__)


HYPERLIQUID_INFO_PATH = "/info"
COINGLASS_BASE = "https://open-api-v4.coinglass.com/api"
COINGLASS_FUNDING_PATH = "/futures/funding-rate/history"
COINGLASS_OI_PATH = "/futures/open-interest/aggregated-history"
COINGLASS_LIQUIDATION_MAP_PATH = "/futures/liquidation/coin-map"


def hl_coin_candidates(symbol: str) -> list[str]:
    """Hyperliquid lists some meme perps with ``k``/``1000`` prefixes."""
    base = symbol.strip().upper()
    if not base:
        return []
    seen: list[str] = []
    for candidate in (base, f"k{base}", f"1000{base}"):
        if candidate not in seen:
            seen.append(candidate)
    return seen


def _change_pct(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return round((current - previous) / previous * 100.0, 3)


def parse_hyperliquid_context(payload: Any, candidates: list[str]) -> dict[str, Any]:
    """Read one asset's context from ``metaAndAssetCtxs``.

    The endpoint returns ``[meta, contexts]`` where the two lists share an index
    order. A symbol that is not listed returns ``{}`` (no perp market), which the
    caller reports as SKIPPED rather than as a provider outage.
    """
    if not isinstance(payload, list) or len(payload) != 2:
        return {}
    meta, contexts = payload
    universe = meta.get("universe") if isinstance(meta, dict) else None
    if not isinstance(universe, list) or not isinstance(contexts, list):
        return {}
    by_name = {
        str(entry.get("name") or "").upper(): contexts[index]
        for index, entry in enumerate(universe)
        if isinstance(entry, dict) and index < len(contexts)
    }
    for candidate in candidates:
        context = by_name.get(candidate.upper())
        if not isinstance(context, dict):
            continue
        funding = first_float(context, "funding")
        mark = first_float(context, "markPx", "oraclePx")
        open_interest = first_float(context, "openInterest")
        open_interest_usd = open_interest * mark if open_interest is not None and mark else None
        return {
            "source": "hyperliquid",
            "symbol": candidate.upper(),
            "funding_rate": funding,
            "mark_price": mark,
            "open_interest": open_interest,
            "open_interest_usd": open_interest_usd,
            "quote_volume_24h": first_float(context, "dayNtlVlm"),
            "price_change_24h_pct": _change_pct(mark, first_float(context, "prevDayPx")),
        }
    return {}


def parse_coinglass_series(payload: Any, *, field: str = "close") -> dict[str, Any]:
    """Parse Coinglass OHLC rows into ``latest`` plus a change percentage."""
    rows = first_row(payload)
    values: list[float] = []
    for row in rows:
        if isinstance(row, dict):
            value = first_float(row, field, "close", "value")
            if value is not None:
                values.append(value)
        elif isinstance(row, (int, float)):
            values.append(float(row))
    if not values:
        return {}
    first, last = values[0], values[-1]
    change = safe_div(last - first, abs(first)) if first else None
    return {
        "latest": round(last, 6),
        "change_pct": round(change * 100.0, 3) if change is not None else None,
        "samples": len(values),
    }


def parse_coinglass_liquidation_map(payload: Any) -> dict[str, Any]:
    """Sum long/short liquidation notional from Coinglass' liquidation map."""
    rows = first_row(payload)
    long_usd = short_usd = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        long_value = first_float(
            row, "longLiquidationUsd", "long_liquidation_usd", "longLiquidation", "long_liq_usd"
        )
        short_value = first_float(
            row, "shortLiquidationUsd", "short_liquidation_usd", "shortLiquidation", "short_liq_usd"
        )
        long_usd += long_value or 0.0
        short_usd += short_value or 0.0
    total = long_usd + short_usd
    if total <= 0:
        return {}
    return {
        "long_liquidation_usd": round(long_usd, 2),
        "short_liquidation_usd": round(short_usd, 2),
        "liquidation_skew": round((long_usd - short_usd) / total, 4),
        "liquidation_samples": len(rows),
    }


def liquidation_score(skew: float | None) -> float:
    """Long-heavy liquidation maps are downside fuel; short-heavy ones squeeze."""
    if skew is None:
        return 50.0
    return clamp(50.0 - clamp(skew, -1.0, 1.0) * 45.0)


def depth_score(open_interest_usd: float | None, volume_24h_usd: float | None) -> float:
    """Deep, liquid perp markets are more trustworthy than thin ones."""
    if open_interest_usd is None and volume_24h_usd is None:
        return 50.0
    oi_score = log_scale(open_interest_usd, 250_000.0, 500_000_000.0)
    volume_score = log_scale(volume_24h_usd, 250_000.0, 2_000_000_000.0)
    return 0.5 * oi_score + 0.5 * volume_score


class CryptoDerivativesScanner(BaseScanner):
    """Layer 2: funding, open interest, depth and liquidation positioning."""

    layer = Layer.DERIVATIVES
    weight = 0.9
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        symbol = self._symbol_for(token, ctx)
        if not symbol:
            return self.skipped(token, "token symbol unknown; no perp lookup possible")

        candidates = hl_coin_candidates(symbol)
        hyperliquid = await fetch_provider("hyperliquid", self._hyperliquid(candidates, ctx))
        metrics: dict[str, Any] = dict(hyperliquid.metrics)
        sources: list[str] = ["hyperliquid"] if hyperliquid.ok else []

        if self.settings.providers.coinglass_api_key.is_configured():
            calls = self._coinglass(symbol, ctx)
            results: list[ProviderResult] = await asyncio.gather(
                *(fetch_provider(name, coro) for name, coro in calls.items())
            )
            for result in results:
                if result.ok:
                    metrics.update(result.metrics)
                    sources.append(result.source)

        if not metrics:
            fallback = await fetch_provider("binance_bybit", self._legacy(symbol, ctx))
            if fallback.ok:
                metrics = dict(fallback.metrics)
                sources.append("binance_bybit")

        if not metrics:
            return self.skipped(
                token,
                f"no perpetual market found for {symbol}"
                if not hyperliquid.error
                else f"derivatives providers unavailable: {hyperliquid.error}",
            )

        funding = to_float(metrics.get("funding_rate"))
        oi_change = to_float(metrics.get("oi_change_pct"))
        taker = to_float(metrics.get("taker_buy_sell_ratio"))
        ratio = to_float(metrics.get("long_short_ratio"))
        oi_usd = to_float(metrics.get("open_interest_usd"))
        volume = to_float(metrics.get("quote_volume_24h"))
        skew = to_float(metrics.get("liquidation_skew"))

        components: list[tuple[float, float]] = []
        if funding is not None:
            components.append((0.30, funding_score(funding)))
        if oi_change is not None:
            components.append((0.25, linear(oi_change, -15.0, 25.0)))
        components.append((0.15, depth_score(oi_usd, volume)))
        if taker is not None:
            components.append((0.15, linear(taker, 0.85, 1.15)))
        if skew is not None:
            components.append((0.15, liquidation_score(skew)))

        total_weight = sum(weight for weight, _ in components) or 1.0
        score = sum(weight * value for weight, value in components) / total_weight

        flags: list[str] = []
        if funding is not None and funding > 0.0008:
            flags.append("funding_overheated")
        if funding is not None and funding < -0.0005:
            flags.append("funding_deep_negative")
        if oi_change is not None and oi_change > 40.0:
            flags.append("oi_spike_above_40pct")
        if ratio is not None and ratio > 3.0:
            flags.append("crowded_long_accounts")
        if skew is not None and skew > 0.35:
            flags.append("liquidation_map_long_heavy")
        if "funding_overheated" in flags:
            score = min(score, 45.0)

        confidence = 0.85 if len(sources) >= 2 else 0.65
        if funding is None or oi_change is None:
            confidence *= 0.65
        status = SignalStatus.OK if len(components) >= 3 and sources else SignalStatus.DEGRADED

        summary = (
            f"{metrics.get('symbol') or symbol} perp: funding "
            f"{(funding * 100 if funding is not None else 0):.4f}%/8h, "
            f"OI {oi_change if oi_change is not None else 'n/a'}%, "
            f"liq skew {skew if skew is not None else 'n/a'} [{'+'.join(sources) or 'no source'}]"
        )
        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=summary,
            status=status,
            metrics={**metrics, "sources": sources, "flags": flags},
            flags=flags,
        )

    # -- providers -----------------------------------------------------------
    @staticmethod
    def _symbol_for(token: TokenSnapshot, ctx: ScanContext) -> str:
        """Prefer the DEX pair's base symbol (canonical), fall back to the hint."""
        market = ctx.get_shared("market")
        base = getattr(market, "base_symbol", None)
        return (base or token.symbol or "").strip()

    async def _hyperliquid(self, candidates: list[str], ctx: ScanContext) -> dict[str, Any]:
        payload = await request_json(
            ctx.session,
            f"{self.settings.providers.hyperliquid_base_url.rstrip('/')}{HYPERLIQUID_INFO_PATH}",
            method="POST",
            json_body={"type": "metaAndAssetCtxs"},
        )
        return parse_hyperliquid_context(payload, candidates)

    def _coinglass(self, symbol: str, ctx: ScanContext) -> dict[str, Any]:
        """Build the (independent) Coinglass reads for one contract."""
        key = self.settings.providers.coinglass_api_key.get_secret_value()
        headers = {"CG-API-KEY": key, "Accept": "application/json"}
        pair = f"{symbol.upper()}USDT"
        coin = symbol.upper()

        async def funding() -> dict[str, Any]:
            payload = await request_json(
                ctx.session,
                f"{COINGLASS_BASE}{COINGLASS_FUNDING_PATH}",
                params={"exchange": "Binance", "symbol": pair, "interval": "8h", "limit": 1},
                headers=headers,
            )
            series = parse_coinglass_series(payload)
            return {"funding_rate": series.get("latest"), "coinglass_samples": series.get("samples")}

        async def open_interest() -> dict[str, Any]:
            payload = await request_json(
                ctx.session,
                f"{COINGLASS_BASE}{COINGLASS_OI_PATH}",
                params={"symbol": coin, "interval": "1h", "limit": 25},
                headers=headers,
            )
            series = parse_coinglass_series(payload)
            return {
                "open_interest_change_pct": series.get("change_pct"),
                "oi_change_pct": series.get("change_pct"),
                "open_interest_usd": series.get("latest"),
            }

        async def liquidation_map() -> dict[str, Any]:
            payload = await request_json(
                ctx.session,
                f"{COINGLASS_BASE}{COINGLASS_LIQUIDATION_MAP_PATH}",
                params={"symbol": coin, "interval": "1h"},
                headers=headers,
            )
            return parse_coinglass_liquidation_map(payload)

        return {"coinglass_funding": funding(), "coinglass_oi": open_interest(), "coinglass_liquidation": liquidation_map()}

    async def _legacy(self, symbol: str, ctx: ScanContext) -> dict[str, Any]:
        """Keyless Binance/Bybit fallback, reusing the original derivatives client."""
        scanner = DerivativesScanner(self.settings)
        for contract in perp_symbol_candidates(symbol):
            metrics = await scanner._fetch(contract, ctx)  # noqa: SLF001 - shared in-tree client
            if metrics:
                return metrics
        return {}


def build_scanner(settings: Settings) -> CryptoDerivativesScanner:
    return CryptoDerivativesScanner(settings)


__all__ = [
    "COINGLASS_BASE",
    "CryptoDerivativesScanner",
    "HYPERLIQUID_INFO_PATH",
    "build_scanner",
    "depth_score",
    "hl_coin_candidates",
    "liquidation_score",
    "parse_coinglass_liquidation_map",
    "parse_coinglass_series",
    "parse_hyperliquid_context",
]
