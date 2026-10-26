"""Technicals layer for listed instruments: indicators from daily exchange bars.

All arithmetic, the indicator block, the score curves and the flag caps come from
:mod:`scanners.shared.technicals` - the same package
``scanners/crypto/layer7_technicals.py`` uses. This module owns only the *layer*:
sourcing candles through :mod:`scanners.shared.series`, choosing the listed-market
weights (the EMA20 slope accelerator, the volume component) and turning the result
into a :class:`LayerSignal`.

It is a separate file from the crypto layer on purpose: same maths, different
inputs (daily bars vs M15/H1 pool candles) and different weights. Re-exporting the
indicator functions from here - as an earlier version did - is what made the two
files look like duplicates; they are not, and the maths has one home.
"""

from __future__ import annotations

from core.models import Layer, LayerSignal, SignalStatus, TokenSnapshot
from scanners.base import BaseScanner, ScanContext, linear, log_scale
from scanners.shared.series import SeriesUnavailable, load_price_series
from scanners.shared.technicals import (
    MIN_CANDLES,
    apply_flag_caps,
    compute_indicators,
    macd_score,
    momentum_score,
    rsi_flags,
    structure_flags,
    trend_score,
    volatility_score,
)


class TechnicalsScanner(BaseScanner):
    layer = Layer.TECHNICALS
    weight = 1.0

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        try:
            series = await load_price_series(token, ctx)
        except SeriesUnavailable as exc:
            return self.skipped(token, str(exc))
        indicators = compute_indicators(series.candles, minimum=MIN_CANDLES)
        if indicators is None:
            return self.unavailable(
                token, f"only {len(series.closes)} candles available; need {MIN_CANDLES}+"
            )

        daily = series.bar_interval.endswith("d")
        change_day = series.change_pct()

        flags = rsi_flags(indicators.rsi14) + structure_flags(
            indicators.price, indicators.ema20, indicators.ema50
        )
        if indicators.atr_pct is not None and indicators.atr_pct >= 20.0:
            flags.append("volatility_extreme")
        if indicators.volume_ratio is not None and indicators.volume_ratio < 0.4:
            flags.append("volume_drying_up")

        score = (
            0.28
            * trend_score(
                indicators.price,
                indicators.ema20,
                indicators.ema50,
                indicators.ema20_slope_pct,
                price_step=18.0,
                cross_step=16.0,
                slope_weight=3.0,
            )
            + 0.20 * momentum_score(indicators.rsi14, change_day)
            + 0.18 * macd_score(indicators.macd)
            + 0.14
            * (
                log_scale(indicators.volume_ratio, 0.4, 2.5)
                if indicators.volume_ratio is not None
                else 50.0
            )
            + 0.10 * linear(indicators.obv_slope_pct, -30.0, 30.0)
            + 0.10 * volatility_score(indicators.atr_pct, indicators.bands, daily=daily)
        )
        score = apply_flag_caps(score, flags)

        confidence = 0.85 if indicators.candles >= 100 else 0.65
        if indicators.macd is None:
            confidence *= 0.8
        status = SignalStatus.OK if indicators.candles >= 100 else SignalStatus.DEGRADED

        parts = [f"{indicators.candles}x{series.bar_interval} candles"]
        if indicators.rsi14 is not None:
            parts.append(f"RSI {indicators.rsi14:.1f}")
        if indicators.ema20 is not None:
            parts.append(f"price {'above' if indicators.price > indicators.ema20 else 'below'} EMA20")
        if indicators.ema50 is not None and indicators.ema20 is not None:
            parts.append(f"EMA20 {'above' if indicators.ema20 > indicators.ema50 else 'below'} EMA50")
        if indicators.macd:
            parts.append(f"MACD hist {indicators.macd['histogram']:.6g}")
        if indicators.atr_pct is not None:
            parts.append(f"ATR {indicators.atr_pct:.1f}%")
        if indicators.volume_ratio is not None:
            parts.append(f"vol {indicators.volume_ratio:.2f}x prior")

        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=", ".join(parts),
            status=status,
            metrics={
                **indicators.metrics(),
                "atr14": indicators.atr14,
                "macd": (
                    {key: round(value, 8) for key, value in indicators.macd.items()}
                    if indicators.macd
                    else None
                ),
                "bollinger": (
                    {key: round(value, 6) for key, value in indicators.bands.items()}
                    if indicators.bands
                    else None
                ),
                "volume_ratio": (
                    round(indicators.volume_ratio, 3) if indicators.volume_ratio is not None else None
                ),
                "change_pct": round(change_day, 2) if change_day is not None else None,
                "change_bars": series.change_bars,
                "bar_interval": series.bar_interval,
                "series_source": series.source,
            },
            flags=flags,
        )


__all__ = [
    "MIN_CANDLES",
    "TechnicalsScanner",
]
