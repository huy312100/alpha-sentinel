"""Score curves - the shared opinion about what an indicator reading is worth.

Weights belong to the layers (crypto and listed markets weight the same
components differently); the *shape* of each curve belongs here, so a 62 RSI is
worth the same in a DEX pool and in an ETF. Every function returns a 0-100 score
and treats missing data as neutral (50) rather than as bearish - silence must
never look like a sell signal.
"""

from __future__ import annotations

from scanners.base import clamp, linear


def trend_score(
    price: float | None,
    ema20: float | None,
    ema50: float | None,
    ema20_slope_pct: float | None = None,
    *,
    price_step: float = 20.0,
    cross_step: float = 20.0,
    slope_weight: float = 0.0,
    slope_cap: float = 16.0,
) -> float:
    """Price above rising EMAs is a trend; below them is distribution.

    ``price_step``/``cross_step`` are the per-leg nudges and ``slope_weight`` turns
    the EMA20 slope into an accelerator (listed markets use it; crypto's
    multi-timeframe read already has momentum components).
    """
    score = 50.0
    if price is not None and ema20 is not None:
        score += price_step if price > ema20 else -price_step
    if ema20 is not None and ema50 is not None:
        score += cross_step if ema20 > ema50 else -cross_step
    if ema20_slope_pct is not None and slope_weight:
        score += max(-slope_cap, min(slope_cap, ema20_slope_pct * slope_weight))
    return clamp(score)


def momentum_score(rsi14: float | None, change_day: float | None = None) -> float:
    """50-65 RSI is the sweet spot: trend with room left. Extremes are penalised."""
    if rsi14 is None:
        return 50.0
    if rsi14 > 70.0:
        return clamp(85.0 - (rsi14 - 70.0) * 4.0)
    if rsi14 < 30.0:
        return clamp(35.0 + (30.0 - rsi14) * 1.5)
    score = 55.0 + (rsi14 - 50.0) * 1.6
    if change_day is not None:
        score += max(-12.0, min(12.0, change_day)) * 0.8
    return clamp(score)


def rsi_score(value: float | None, *, overbought: float = 78.0, oversold: float = 25.0) -> float:
    """Reward constructive momentum; penalise exhaustion at both extremes."""
    if value is None:
        return 50.0
    if value >= overbought:
        return 25.0
    if value <= oversold:
        return 35.0
    return linear(value, 30.0, 70.0)


def macd_score(stats: dict[str, float] | None) -> float:
    """Positive histogram is constructive; its acceleration is the tie-break."""
    if not stats:
        return 50.0
    score = 70.0 if stats["histogram"] > 0 else 30.0
    delta = stats.get("histogram_delta", 0.0)
    if stats.get("macd"):
        # Scale the histogram's acceleration by the MACD line's own magnitude so
        # the nudge is price-scale invariant.
        score += max(-14.0, min(14.0, delta / abs(stats["macd"]) * 100.0))
    return clamp(score)


def volatility_score(
    atr_pct: float | None,
    bands: dict[str, float] | None = None,
    *,
    daily: bool = False,
) -> float:
    """Tradeable volatility band; dead or violent markets are not.

    Hourly crypto and daily exchange bars live on different scales, so the band
    shifts with the bar interval instead of penalising every low-vol market (FX,
    rates) as if it were a dead token.
    """
    low, high, extreme = (0.2, 8.0, 16.0) if daily else (0.5, 10.0, 20.0)
    score = 50.0
    if atr_pct is not None:
        if atr_pct < low:
            score = 30.0
        elif atr_pct <= high:
            score = 75.0
        elif atr_pct <= extreme:
            score = 45.0
        else:
            score = 20.0
    if bands and bands.get("bandwidth_pct") is not None:
        if bands["bandwidth_pct"] < 2.0:
            score -= 10.0
        elif bands["bandwidth_pct"] > 40.0:
            score -= 5.0
    return clamp(score)


def order_flow_score(imbalance: float | None) -> float:
    """Map signed taker flow ``-1..1`` onto the 0-100 score scale."""
    if imbalance is None:
        return 50.0
    return linear(imbalance, -0.5, 0.5)


__all__ = [
    "macd_score",
    "momentum_score",
    "order_flow_score",
    "rsi_score",
    "trend_score",
    "volatility_score",
]
