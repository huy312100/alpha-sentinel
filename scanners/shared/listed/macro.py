"""Macro layer: the backdrop that decides whether a setup is worth taking.

Two reads, one layer, chosen by asset class:

* **Crypto** - CoinGecko global (total mcap, BTC dominance) plus Fear & Greed.
  Alt-season phases decide whether a good micro setup is worth taking at all.
* **Everything else** - a cross-asset tape: VIX for the price of protection, the
  dollar for global liquidity, US 10y for the discount rate and the S&P for the
  risk appetite every other market prices against.

Both are market-wide, so the result is cached in the scan context and shared by
every instrument in the batch; a cycle pays for one read, not one per ticker.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping
from urllib.parse import quote as url_quote

from config.settings import EndpointSettings
from core.http import request_json, to_float
from core.instruments import AssetClass
from core.models import Layer, SignalStatus, LayerSignal, TokenSnapshot
from scanners.base import BaseScanner, ScanContext, clamp, linear, safe_div
from scanners.shared.market_data import parse_yahoo_chart

#: Cross-asset dashboard used for non-crypto instruments. All keyless on Yahoo.
CROSS_ASSET_SYMBOLS: dict[str, str] = {
    "vix": "^VIX",
    "dollar": "DX-Y.NYB",
    "us10y": "^TNX",
    "spx": "^GSPC",
    "gold": "GC=F",
}
CROSS_ASSET_CACHE_KEY = "macro:cross_asset"
CRYPTO_CACHE_KEY = "macro:crypto"


def fear_greed_score(value: float | None) -> float:
    """Contrarian: extreme greed is late, extreme fear is where asymmetric entries live."""
    if value is None:
        return 50.0
    if value >= 75.0:
        return max(20.0, 100.0 - (value - 75.0) * 3.0)
    if value <= 25.0:
        return 45.0 + (25.0 - value) * 1.2  # capitulation is opportunity, not comfort
    return 60.0 + (50.0 - abs(value - 50.0)) * 0.4


def parse_fear_greed(payload: Any) -> tuple[float | None, str]:
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return None, ""
    row = data[0] if isinstance(data[0], dict) else {}
    return to_float(row.get("value")), str(row.get("value_classification") or "")


def parse_global(payload: Any) -> dict[str, float | None]:
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}
    mcap = data.get("total_market_cap") or {}
    volume = data.get("total_volume") or {}
    change = data.get("market_cap_change_percentage_24h_usd")
    dominance = data.get("market_cap_percentage") or {}
    return {
        "total_market_cap_usd": to_float(mcap.get("usd")),
        "total_volume_usd": to_float(volume.get("usd")),
        "market_cap_change_24h_pct": to_float(change),
        "btc_dominance": to_float(dominance.get("btc")),
        "eth_dominance": to_float(dominance.get("eth")),
    }


# ---------------------------------------------------------------------------
# Cross-asset read (everything that is not crypto)
# ---------------------------------------------------------------------------
def series_momentum(closes: list[float]) -> dict[str, Any]:
    """Level, 5d/20d change and 50d mean for one daily series."""
    if not closes:
        return {}
    level = closes[-1]
    result: dict[str, Any] = {"level": level, "bars": len(closes)}

    def _change(lookback: int) -> float | None:
        if len(closes) <= lookback or closes[-lookback - 1] == 0:
            return None
        return (level - closes[-lookback - 1]) / closes[-lookback - 1] * 100.0

    result["change_5d_pct"] = _change(5)
    result["change_20d_pct"] = _change(20)
    if len(closes) >= 50:
        sma50 = sum(closes[-50:]) / 50.0
        result["sma50"] = sma50
        result["above_sma50"] = level >= sma50
    return result


def summarise_cross_asset(payloads: Mapping[str, Any]) -> dict[str, Any]:
    """Turn raw chart payloads into a compact cross-asset tape."""
    metrics: dict[str, Any] = {}
    for name, payload in payloads.items():
        if not isinstance(payload, Mapping):
            continue
        try:
            quote, candles = parse_yahoo_chart(
                payload, fallback_symbol=CROSS_ASSET_SYMBOLS.get(name, "")
            )
        except ValueError:
            continue
        entry = series_momentum(candles.get("close", []))
        if not entry:
            continue
        entry["symbol"] = quote.provider_symbol or CROSS_ASSET_SYMBOLS.get(name, "")
        if entry.get("level") is None:
            entry["level"] = quote.price
        metrics[name] = entry
    return metrics


# ---------------------------------------------------------------------------
# Fetchers (shared by the scanner and the pipeline's once-per-cycle prefetch)
# ---------------------------------------------------------------------------
def crypto_backdrop_usable(metrics: Mapping[str, Any]) -> bool:
    """At least one crypto backdrop metric must be present to be worth caching."""
    return any(
        to_float(metrics.get(key)) is not None
        for key in ("market_cap_change_24h_pct", "btc_dominance", "fear_greed")
    )


def cross_asset_usable(metrics: Mapping[str, Any]) -> bool:
    """A tape with no usable component must not be cached as if it were data."""
    return cross_asset_score(metrics)[2] > 0


async def fetch_crypto_backdrop(
    session: Any,
    *,
    endpoints: EndpointSettings | None = None,
) -> dict[str, Any]:
    """Fetch the crypto market-wide read once (CoinGecko global + Fear & Greed)."""
    endpoints = endpoints or EndpointSettings()
    global_payload, fng_payload = await asyncio.gather(
        request_json(session, endpoints.url("coingecko.global")),
        request_json(
            session,
            endpoints.url("fear_greed.index"),
            params={"limit": 2},
        ),
        return_exceptions=True,
    )
    metrics = parse_global(None if isinstance(global_payload, Exception) else global_payload)
    fng_value, fng_label = parse_fear_greed(None if isinstance(fng_payload, Exception) else fng_payload)
    metrics.update({"fear_greed": fng_value, "fear_greed_label": fng_label})
    return metrics


async def fetch_cross_asset_backdrop(
    session: Any,
    *,
    endpoints: EndpointSettings | None = None,
) -> dict[str, Any]:
    """Fetch the VIX/dollar/yields/S&P tape once for every listed instrument."""
    endpoints = endpoints or EndpointSettings()
    payloads = await asyncio.gather(
        *(
            request_json(
                session,
                endpoints.url("yahoo.chart", symbol=url_quote(symbol, safe="")),
                params={"range": "3mo", "interval": "1d", "includePrePost": "false"},
            )
            for symbol in CROSS_ASSET_SYMBOLS.values()
        ),
        return_exceptions=True,
    )
    raw = {
        name: payload
        for name, payload in zip(CROSS_ASSET_SYMBOLS, payloads)
        if not isinstance(payload, Exception)
    }
    return summarise_cross_asset(raw)


def cross_asset_score(metrics: Mapping[str, Any]) -> tuple[float, list[str], float]:  # noqa: C901
    """Risk-on score (0-100) from VIX, the dollar, US 10y and the S&P."""
    flags: list[str] = []
    components: list[tuple[float, float]] = []

    vix = metrics.get("vix") or {}
    vix_level = to_float(vix.get("level"))
    vix_change = to_float(vix.get("change_5d_pct"))
    if vix_level is not None:
        score = 100.0 - linear(vix_level, 12.0, 35.0)
        if vix_change is not None:
            score -= clamp(vix_change * 1.5, -15.0, 15.0)
        components.append((0.35, clamp(score)))
        if vix_level >= 25.0:
            flags.append("vix_elevated")
        if vix_change is not None and vix_change >= 25.0:
            flags.append("volatility_spiking")

    dollar = metrics.get("dollar") or {}
    dollar_change = to_float(dollar.get("change_5d_pct"))
    if dollar_change is not None:
        components.append((0.20, 100.0 - linear(dollar_change, -2.0, 2.0)))
        if dollar_change >= 2.0:
            flags.append("dollar_squeeze")

    yields = metrics.get("us10y") or {}
    yield_change = to_float(yields.get("change_5d_pct"))
    if yield_change is not None:
        components.append((0.20, 100.0 - linear(yield_change, -5.0, 5.0)))
        if yield_change >= 5.0:
            flags.append("yields_rising")

    spx = metrics.get("spx") or {}
    spx_change = to_float(spx.get("change_20d_pct"))
    if spx_change is not None:
        score = linear(spx_change, -5.0, 5.0)
        if spx.get("above_sma50") is True:
            score = clamp(score + 10.0)
        elif spx.get("above_sma50") is False:
            score = clamp(score - 10.0)
            flags.append("equity_downtrend")
        components.append((0.25, score))

    if not components:
        return 50.0, ["no_cross_asset_data"], 0.0
    total = sum(weight for weight, _ in components)
    score = sum(weight * value for weight, value in components) / total
    confidence = 0.75 if len(components) >= 3 else 0.55
    return clamp(score), flags, confidence


class MacroScanner(BaseScanner):
    layer = Layer.MACRO
    weight = 0.5

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:
        if AssetClass.coerce(token.asset_class).is_crypto:
            return await self._crypto_backdrop(token, ctx)
        return await self._cross_asset_backdrop(token, ctx)

    async def _crypto_backdrop(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        cached = ctx.get_shared(CRYPTO_CACHE_KEY)
        if not isinstance(cached, dict):
            try:
                cached = await fetch_crypto_backdrop(ctx.session, endpoints=ctx.settings.endpoints)
            except Exception:  # noqa: BLE001 - an unreachable backdrop is not a token failure
                cached = {}
            # Never cache a failed read: a later instrument would inherit the gap.
            if crypto_backdrop_usable(cached):
                ctx.set_shared(CRYPTO_CACHE_KEY, cached)

        change = to_float(cached.get("market_cap_change_24h_pct"))
        dominance = to_float(cached.get("btc_dominance"))
        fng = to_float(cached.get("fear_greed"))
        volume = to_float(cached.get("total_volume_usd"))
        mcap = to_float(cached.get("total_market_cap_usd"))

        if change is None and fng is None and dominance is None:
            return self.unavailable(token, "macro providers returned no usable data")

        flags: list[str] = []
        if change is not None and change < -5.0:
            flags.append("macro_risk_off_day")
        if dominance is not None and dominance > 60.0:
            flags.append("btc_dominance_above_60")
        if fng is not None and fng >= 80.0:
            flags.append("extreme_greed")

        trend_score = linear(change, -6.0, 6.0)
        fng_score = fear_greed_score(fng)
        # Rising BTC dominance drains alt liquidity; 40-50% is the friendliest band.
        alt_rotation_score = linear(52.0 - (dominance if dominance is not None else 52.0), -12.0, 12.0)
        turnover_score = linear(safe_div(volume, mcap), 0.02, 0.12)

        score = 0.35 * trend_score + 0.30 * fng_score + 0.25 * alt_rotation_score + 0.10 * turnover_score
        if "macro_risk_off_day" in flags:
            score = min(score, 40.0)

        confidence = 0.85 if change is not None and fng is not None else 0.6
        status = SignalStatus.OK if change is not None and fng is not None else SignalStatus.DEGRADED
        change_text = f"{change:.2f}%" if change is not None else "n/a"
        dominance_text = f"{dominance:.1f}%" if dominance is not None else "n/a"
        fng_text = f"{fng:.0f}" if fng is not None else "n/a"
        summary = (
            f"Total mcap 24h {change_text}, BTC dominance {dominance_text}, "
            f"Fear & Greed {fng_text} {cached.get('fear_greed_label') or ''}"
        ).strip()
        metrics = dict(cached)
        metrics["flags"] = flags
        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=summary,
            status=status,
            metrics=metrics,
            flags=flags,
        )

    async def _cross_asset_backdrop(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:
        cached = ctx.get_shared(CROSS_ASSET_CACHE_KEY)
        if not isinstance(cached, dict):
            try:
                cached = await fetch_cross_asset_backdrop(ctx.session, endpoints=ctx.settings.endpoints)
            except Exception:  # noqa: BLE001 - degrade to UNAVAILABLE, never crash the layer
                cached = {}
            if cross_asset_usable(cached):
                ctx.set_shared(CROSS_ASSET_CACHE_KEY, cached)

        score, flags, confidence = cross_asset_score(cached)
        if confidence <= 0:
            return self.unavailable(token, "cross-asset providers returned no usable data")

        def _fmt(name: str) -> str:
            entry = cached.get(name) or {}
            level = to_float(entry.get("level"))
            change = to_float(entry.get("change_5d_pct"))
            if level is None:
                return f"{name} n/a"
            if change is None:
                return f"{name} {level:.2f}"
            return f"{name} {level:.2f} ({change:+.1f}% 5d)"

        summary = f"Cross-asset: {_fmt('vix')}, {_fmt('dollar')}, {_fmt('us10y')}, {_fmt('spx')}"
        metrics = dict(cached)
        metrics["flags"] = flags
        status = SignalStatus.OK if len(cached) >= 3 else SignalStatus.DEGRADED
        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=summary,
            status=status,
            metrics=metrics,
            flags=flags,
        )


__all__ = [
    "CROSS_ASSET_CACHE_KEY",
    "CROSS_ASSET_SYMBOLS",
    "CRYPTO_CACHE_KEY",
    "MacroScanner",
    "cross_asset_score",
    "cross_asset_usable",
    "crypto_backdrop_usable",
    "fear_greed_score",
    "fetch_cross_asset_backdrop",
    "fetch_crypto_backdrop",
    "parse_fear_greed",
    "parse_global",
    "series_momentum",
    "summarise_cross_asset",
]
