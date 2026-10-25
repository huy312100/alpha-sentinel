"""Technicals layer: indicators computed from a provider-neutral price series.

No ``pandas-ta``/``TA-Lib`` dependency: the handful of indicators we actually trade
on are implemented here in pure Python and unit-tested. That keeps the container
small and the maths auditable - an indicator we cannot reason about is a liability
in a system that can veto on its output.

The layer is asset-class agnostic: candles come from :mod:`scanners.series`, which
uses GeckoTerminal for crypto and daily exchange bars for everything else. The
indicator maths and the score composition are identical across markets, so a
crypto momentum read and an equity momentum read mean the same thing.
"""

from __future__ import annotations

import math
from typing import Sequence

from core.models import Layer, SignalStatus, LayerSignal, TokenSnapshot
from scanners.base import BaseScanner, ScanContext, clamp, linear, log_scale, safe_div
from scanners.series import GECKO_OHLCV_URL, SeriesUnavailable, load_price_series, parse_ohlcv


# ---------------------------------------------------------------------------
# Indicators (pure functions, no external deps)
# ---------------------------------------------------------------------------
def sma(values: Sequence[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def ema_series(values: Sequence[float], period: int) -> list[float]:
    """Classic EMA seeded with an SMA; returns one value per input after the seed."""
    if period <= 0 or len(values) < period:
        return []
    multiplier = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    series = [seed]
    for value in values[period:]:
        series.append(value * multiplier + series[-1] * (1.0 - multiplier))
    return series


def ema(values: Sequence[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """Wilder's RSI. Returns ``None`` when there is not enough history."""
    if len(values) <= period:
        return None
    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        delta = values[index] - values[index - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    for index in range(period + 1, len(values)):
        delta = values[index] - values[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> dict[str, float] | None:
    """Return ``{'macd', 'signal', 'histogram'}`` or ``None`` without enough data."""
    fast_series = ema_series(values, fast)
    slow_series = ema_series(values, slow)
    if not fast_series or not slow_series:
        return None
    offset = len(fast_series) - len(slow_series)
    if offset < 0:
        return None
    macd_line = [fast_series[index + offset] - slow_series[index] for index in range(len(slow_series))]
    signal_series = ema_series(macd_line, signal_period)
    if not signal_series:
        return None
    signal_value = signal_series[-1]
    macd_value = macd_line[-1]
    previous_hist = (macd_line[-2] - signal_series[-2]) if len(signal_series) >= 2 else 0.0
    return {
        "macd": macd_value,
        "signal": signal_value,
        "histogram": macd_value - signal_value,
        "histogram_delta": (macd_value - signal_value) - previous_hist,
    }


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder's Average True Range."""
    count = min(len(highs), len(lows), len(closes))
    if count <= period:
        return None
    true_ranges: list[float] = []
    for index in range(1, count):
        high, low, previous_close = highs[index], lows[index], closes[index - 1]
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    if len(true_ranges) < period:
        return None
    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def bollinger(values: Sequence[float], period: int = 20, mult: float = 2.0) -> dict[str, float] | None:
    if len(values) < period:
        return None
    window = list(values[-period:])
    middle = sum(window) / period
    variance = sum((value - middle) ** 2 for value in window) / period
    deviation = math.sqrt(variance)
    return {
        "middle": middle,
        "upper": middle + mult * deviation,
        "lower": middle - mult * deviation,
        "bandwidth_pct": (2.0 * mult * deviation / middle * 100.0) if middle else 0.0,
    }


def obv(closes: Sequence[float], volumes: Sequence[float]) -> list[float]:
    """On-balance volume - sign of the move times traded volume."""
    count = min(len(closes), len(volumes))
    series = [0.0]
    for index in range(1, count):
        if closes[index] > closes[index - 1]:
            series.append(series[-1] + volumes[index])
        elif closes[index] < closes[index - 1]:
            series.append(series[-1] - volumes[index])
        else:
            series.append(series[-1])
    return series


def percent_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / previous * 100.0


class TechnicalsScanner(BaseScanner):
    layer = Layer.TECHNICALS
    weight = 1.0

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        try:
            series = await load_price_series(token, ctx)
        except SeriesUnavailable as exc:
            return self.skipped(token, str(exc))
        candles = series.candles
        closes = candles.get("close", [])
        if len(closes) < 35:
            return self.unavailable(token, f"only {len(closes)} candles available; need 35+")

        price = closes[-1]
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50) if len(closes) >= 50 else None
        rsi14 = rsi(closes, 14)
        macd_stats = macd(closes)
        atr14 = atr(candles["high"], candles["low"], closes, 14)
        bands = bollinger(closes, 20)
        volume_series = candles["volume"]
        obv_series = obv(closes, volume_series)
        recent_volume = sum(volume_series[-12:])
        prior_volume = sum(volume_series[-36:-12]) / 2.0 if len(volume_series) >= 36 else None
        volume_ratio = safe_div(recent_volume, prior_volume)
        atr_pct = safe_div(atr14, price)
        atr_pct = atr_pct * 100.0 if atr_pct is not None else None
        obv_slope = percent_change(obv_series[-1], obv_series[-13]) if len(obv_series) >= 13 else None
        change_day = series.change_pct()
        ema20_slope = percent_change(ema20, ema(closes[:-6], 20)) if len(closes) > 26 else None

        flags: list[str] = []
        if rsi14 is not None and rsi14 >= 78:
            flags.append("rsi_overbought")
        if rsi14 is not None and rsi14 <= 25:
            flags.append("rsi_oversold")
        if atr_pct is not None and atr_pct >= 20.0:
            flags.append("volatility_extreme")
        if volume_ratio is not None and volume_ratio < 0.4:
            flags.append("volume_drying_up")
        if ema20 is not None and ema50 is not None and price < ema20 < ema50:
            flags.append("downtrend_structure")

        score = (
            0.28 * self._trend_score(price, ema20, ema50, ema20_slope)
            + 0.20 * self._momentum_score(rsi14, change_day)
            + 0.18 * self._macd_score(macd_stats)
            + 0.14 * (log_scale(volume_ratio, 0.4, 2.5) if volume_ratio is not None else 50.0)
            + 0.10 * linear(obv_slope, -30.0, 30.0)
            + 0.10 * self._volatility_score(atr_pct, bands, daily=series.bar_interval.endswith("d"))
        )
        if "downtrend_structure" in flags:
            score = min(score, 42.0)
        if "rsi_overbought" in flags:
            score = min(score, 60.0)

        confidence = 0.85 if len(closes) >= 100 else 0.65
        if macd_stats is None:
            confidence *= 0.8
        status = SignalStatus.OK if len(closes) >= 100 else SignalStatus.DEGRADED

        parts = [f"{len(closes)}x{series.bar_interval} candles"]
        if rsi14 is not None:
            parts.append(f"RSI {rsi14:.1f}")
        if ema20 is not None:
            parts.append(f"price {'above' if price > ema20 else 'below'} EMA20")
        if ema50 is not None and ema20 is not None:
            parts.append(f"EMA20 {'above' if ema20 > ema50 else 'below'} EMA50")
        if macd_stats:
            parts.append(f"MACD hist {macd_stats['histogram']:.6g}")
        if atr_pct is not None:
            parts.append(f"ATR {atr_pct:.1f}%")
        if volume_ratio is not None:
            parts.append(f"vol {volume_ratio:.2f}x prior")

        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=", ".join(parts),
            status=status,
            metrics={
                "price": price,
                "ema20": ema20,
                "ema50": ema50,
                "rsi14": round(rsi14, 2) if rsi14 is not None else None,
                "macd": {key: round(value, 8) for key, value in macd_stats.items()} if macd_stats else None,
                "atr14": atr14,
                "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
                "bollinger": {key: round(value, 6) for key, value in bands.items()} if bands else None,
                "volume_ratio": round(volume_ratio, 3) if volume_ratio is not None else None,
                "obv_slope_pct": round(obv_slope, 2) if obv_slope is not None else None,
                "change_pct": round(change_day, 2) if change_day is not None else None,
                "change_bars": series.change_bars,
                "bar_interval": series.bar_interval,
                "series_source": series.source,
                "candles": len(closes),
            },
            flags=flags,
        )

    # -- component scoring ---------------------------------------------------
    @staticmethod
    def _trend_score(price: float, ema20: float | None, ema50: float | None, ema20_slope: float | None) -> float:
        score = 50.0
        if ema20 is not None:
            score += 18.0 if price > ema20 else -18.0
        if ema50 is not None and ema20 is not None:
            score += 16.0 if ema20 > ema50 else -16.0
        if ema20_slope is not None:
            score += clamp(ema20_slope * 3.0, -16.0, 16.0)
        return clamp(score)

    @staticmethod
    def _momentum_score(rsi14: float | None, change_day: float | None) -> float:
        """50-65 RSI is the sweet spot: trend with room left. Extremes are penalised."""
        if rsi14 is None:
            return 50.0
        if rsi14 > 70.0:
            return clamp(85.0 - (rsi14 - 70.0) * 4.0)
        if rsi14 < 30.0:
            return clamp(35.0 + (30.0 - rsi14) * 1.5)
        score = 55.0 + (rsi14 - 50.0) * 1.6
        if change_day is not None:
            score += clamp(change_day, -12.0, 12.0) * 0.8
        return clamp(score)

    @staticmethod
    def _macd_score(stats: dict[str, float] | None) -> float:
        if not stats:
            return 50.0
        score = 70.0 if stats["histogram"] > 0 else 30.0
        delta = stats.get("histogram_delta", 0.0)
        if stats.get("macd"):
            # Scale the histogram's acceleration by the MACD line's own magnitude so
            # the nudge is price-scale invariant.
            score += clamp(delta / abs(stats["macd"]) * 100.0, -14.0, 14.0)
        return clamp(score)

    @staticmethod
    def _volatility_score(atr_pct: float | None, bands: dict[str, float] | None, *, daily: bool = False) -> float:
        """Tradeable volatility band; dead or violent markets are not.

        Hourly crypto and daily exchange bars live on different scales, so the
        band shifts with the bar interval instead of penalising every low-vol
        market (FX, rates) as if it were a dead token.
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


__all__ = [
    "GECKO_OHLCV_URL",
    "TechnicalsScanner",
    "atr",
    "bollinger",
    "ema",
    "ema_series",
    "macd",
    "obv",
    "parse_ohlcv",
    "percent_change",
    "rsi",
    "sma",
]
