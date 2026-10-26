"""Raw indicator arithmetic - the smallest auditable unit of the technical stack.

Every function is pure, takes a plain ``Sequence[float]`` and returns ``None``
rather than raising when history is short. Nothing here knows about scoring,
vendors or asset classes, so the numbers can be pinned in isolation by
``tests/test_technicals.py``. No ``pandas-ta``/``TA-Lib``: the handful of
indicators this system trades on stay readable and the container stays small.
"""

from __future__ import annotations

import math
from typing import Sequence


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------
def sma(values: Sequence[float], period: int) -> float | None:
    """Simple moving average of the last ``period`` values."""
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


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------
def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> float | None:
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
    for true_range in true_ranges[period:]:
        value = (value * (period - 1) + true_range) / period
    return value


def bollinger(values: Sequence[float], period: int = 20, mult: float = 2.0) -> dict[str, float] | None:
    """Bollinger bands plus bandwidth, the cheap 'is it coiled?' read."""
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


def zscore(values: Sequence[float], period: int = 20) -> float | None:
    """How many standard deviations the latest value sits from its window mean.

    Scale free by construction, so it compares a 0.0001-priced memecoin with a
    $60k bar: ``|z| > 2`` is statistically rare for the window, whatever the
    price happens to be.
    """
    if period <= 1 or len(values) < period:
        return None
    window = list(values[-period:])
    mean = sum(window) / period
    variance = sum((value - mean) ** 2 for value in window) / period
    if variance <= 0:
        return 0.0
    deviation = math.sqrt(variance)
    if deviation == 0:
        return 0.0
    return (window[-1] - mean) / deviation


# ---------------------------------------------------------------------------
# Volume / flow
# ---------------------------------------------------------------------------
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
    """Percentage move from ``previous`` to ``current``; ``None`` without a base.

    The base is ``|previous|`` because the same helper measures OBV, which is
    signed: rising from ``-100`` to ``-50`` is accumulation, not a 50% collapse.
    Divides by zero only when the base is exactly zero.
    """
    if current is None or previous in (None, 0):
        return None
    return (current - previous) / abs(previous) * 100.0


def order_flow_imbalance(buy_volume_usd: float | None, sell_volume_usd: float | None) -> float | None:
    """Signed taker flow normalised to ``-1..1``.

    Positive means aggressive buyers are lifting offers; negative means sellers are
    hitting bids. It is the microstructure proxy that matters for a token with no
    central limit order book, and it is scale free by construction.
    """
    if buy_volume_usd is None or sell_volume_usd is None:
        return None
    total = buy_volume_usd + sell_volume_usd
    if total <= 0:
        return None
    return max(-1.0, min(1.0, (buy_volume_usd - sell_volume_usd) / total))


__all__ = [
    "atr",
    "bollinger",
    "ema",
    "ema_series",
    "macd",
    "obv",
    "order_flow_imbalance",
    "percent_change",
    "rsi",
    "sma",
    "zscore",
]
