"""Market layer: venue microstructure for everything that is not a DEX pair.

A token's ``dex`` layer answers "how deep is the pool and who is trading it".
A listed instrument has no pool, but it does have the same three questions:

* **Can I get out?** Dollar volume, not share count.
* **Where is price in its own range?** 52-week position beats a raw return.
* **Is participation expanding or dying?** Today's volume against its own month.

Scores are composed exactly like every other layer (0-100 bullishness, explicit
confidence, flags for the risk agent), so an equity read slots into the same
prior/blend/veto machinery as a token read.
"""

from __future__ import annotations

from core.instruments import AssetClass
from core.models import Layer, SignalStatus, LayerSignal, TokenSnapshot, clamp
from scanners.base import BaseScanner, ScanContext, linear, log_scale
from scanners.market_data import load_market_series

#: Venues where less than a million dollars a day cannot absorb a real position.
MIN_LIQUID_DOLLAR_VOLUME = 1_000_000.0
MAX_LIQUID_DOLLAR_VOLUME = 500_000_000.0
#: Bars needed before the trend/position reads are trustworthy.
MIN_BARS = 35
FULL_CONFIDENCE_BARS = 120


def trend_return_pct(closes: list[float], lookback: int = 63) -> float | None:
    """Three-month total return (63 trading sessions)."""
    if len(closes) <= lookback:
        return None
    start = closes[-lookback - 1]
    if start == 0:
        return None
    return (closes[-1] - start) / start * 100.0


def relative_volume(volumes: list[float], *, window: int = 20) -> float | None:
    """Last session's volume against the mean of the prior ``window`` sessions."""
    if len(volumes) < window + 1:
        return None
    baseline = sum(volumes[-window - 1 : -1]) / window
    if baseline <= 0:
        return None
    return volumes[-1] / baseline


class MarketScanner(BaseScanner):
    """Liquidity, range position and participation for listed instruments."""

    layer = Layer.MARKET
    weight = 1.1
    asset_classes = frozenset(
        {
            AssetClass.EQUITY,
            AssetClass.ETF,
            AssetClass.INDEX,
            AssetClass.FX,
            AssetClass.COMMODITY,
            AssetClass.RATE,
        }
    )

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        quote, candles = await load_market_series(token, ctx)
        closes = candles.get("close", [])
        if quote.price is None or len(closes) < MIN_BARS:
            return self.unavailable(
                token, f"only {len(closes)} bars for {quote.provider_symbol or token.token_address}"
            )

        flags: list[str] = []
        dollar_volume = quote.dollar_volume
        if dollar_volume is not None and dollar_volume < MIN_LIQUID_DOLLAR_VOLUME:
            flags.append("illiquid_venue")
        position = quote.week_range_position
        if position is not None and position < 5.0:
            flags.append("near_52w_low")

        rel_volume = relative_volume(candles.get("volume", []))
        if rel_volume is not None and rel_volume < 0.4:
            flags.append("volume_drying_up")
        trend = trend_return_pct(closes)
        if trend is not None and trend < -20.0:
            flags.append("trend_broken")

        liquidity_score = (
            log_scale(dollar_volume, MIN_LIQUID_DOLLAR_VOLUME, MAX_LIQUID_DOLLAR_VOLUME)
            if dollar_volume is not None
            else 50.0
        )
        trend_score = linear(trend, -15.0, 25.0)
        position_score = clamp(35.0 + 0.45 * position) if position is not None else 50.0
        participation_score = log_scale(rel_volume, 0.5, 2.0) if rel_volume is not None else 50.0

        score = (
            0.35 * liquidity_score
            + 0.30 * trend_score
            + 0.20 * position_score
            + 0.15 * participation_score
        )
        if "illiquid_venue" in flags:
            score = min(score, 45.0)

        confidence = 0.8 if len(closes) >= FULL_CONFIDENCE_BARS else 0.55
        status = SignalStatus.OK if len(closes) >= FULL_CONFIDENCE_BARS else SignalStatus.DEGRADED

        summary_parts = [f"{quote.venue or token.chain}"]
        if dollar_volume is not None:
            summary_parts.append(f"${dollar_volume / 1e6:.1f}M/day")
        if trend is not None:
            summary_parts.append(f"3m {trend:+.1f}%")
        if position is not None:
            summary_parts.append(f"{position:.0f}% of 52w range")
        if rel_volume is not None:
            summary_parts.append(f"vol {rel_volume:.2f}x")
        summary = f"{quote.provider_symbol or token.symbol}: " + ", ".join(summary_parts)

        metrics = quote.to_metrics()
        metrics.update(
            {
                "trend_3m_pct": round(trend, 2) if trend is not None else None,
                "relative_volume": round(rel_volume, 3) if rel_volume is not None else None,
                "turnover_usd": round(dollar_volume, 2) if dollar_volume is not None else None,
                "bars": len(closes),
            }
        )
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
    "FULL_CONFIDENCE_BARS",
    "MIN_BARS",
    "MIN_LIQUID_DOLLAR_VOLUME",
    "MarketScanner",
    "relative_volume",
    "trend_return_pct",
]
