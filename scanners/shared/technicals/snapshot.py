"""The indicator block: one snapshot shape for every market and timeframe.

Both technical layers (``scanners/crypto/layer7_technicals.py`` for M15/H1 DEX
bars and ``scanners/shared/listed/technicals.py`` for daily exchange bars) call
:func:`compute_indicators` and read the same fields, so "RSI 62" or "price above
EMA20" means the same thing everywhere. The arithmetic lives in
:mod:`.primitives`; this module only decides what belongs in the snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from scanners.base import safe_div
from scanners.shared.technicals.primitives import (
    atr,
    bollinger,
    ema,
    macd,
    obv,
    percent_change,
    rsi,
)

#: Fewer than this many candles cannot support EMA50 + ATR14 + a 13-bar OBV slope.
MIN_CANDLES = 35


@dataclass(frozen=True, slots=True)
class Indicators:
    """One snapshot of the indicator block, computed once and read by both layers."""

    price: float
    ema20: float | None
    ema50: float | None
    rsi14: float | None
    atr14: float | None
    #: ATR as a percentage of price (``3.4`` means 3.4%), comparable across venues.
    atr_pct: float | None
    bands: dict[str, float] | None
    macd: dict[str, float] | None
    volume_ratio: float | None
    obv_slope_pct: float | None
    ema20_slope_pct: float | None
    change_12_pct: float | None
    candles: int

    def metrics(self, prefix: str = "") -> dict[str, Any]:
        """Flat ``{prefix}_indicator`` dict, the shape layer metrics are stored in."""
        label = f"{prefix}_" if prefix else ""
        return {
            f"{label}price": self.price,
            f"{label}ema20": self.ema20,
            f"{label}ema50": self.ema50,
            f"{label}rsi14": round(self.rsi14, 2) if self.rsi14 is not None else None,
            f"{label}atr_pct": round(self.atr_pct, 3) if self.atr_pct is not None else None,
            f"{label}obv_slope_pct": round(self.obv_slope_pct, 2) if self.obv_slope_pct is not None else None,
            f"{label}change_12_pct": self.change_12_pct,
            f"{label}candles": self.candles,
        }


def compute_indicators(
    candles: dict[str, Sequence[float]] | None,
    *,
    minimum: int = MIN_CANDLES,
) -> Indicators | None:
    """Indicator block for one timeframe; ``None`` when there is not enough history.

    ``candles`` is the ``{'open','high','low','close','volume'}`` shape produced by
    :func:`scanners.shared.series.parse_ohlcv` and the Yahoo chart parser, so a DEX
    pool and a listed instrument run through identical arithmetic.
    """
    data = candles or {}
    closes = list(data.get("close") or [])
    highs = list(data.get("high") or [])
    lows = list(data.get("low") or [])
    volumes = list(data.get("volume") or [])
    if len(closes) < minimum or len(highs) < minimum or len(lows) < minimum:
        return None

    price = closes[-1]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50) if len(closes) >= 50 else None
    atr14 = atr(highs, lows, closes, 14)
    atr_pct_ratio = safe_div(atr14, price)
    obv_series = obv(closes, volumes) if len(volumes) == len(closes) else []
    recent_volume = sum(volumes[-12:]) if len(volumes) >= 12 else None
    prior_volume = sum(volumes[-36:-12]) / 2.0 if len(volumes) >= 36 else None
    return Indicators(
        price=price,
        ema20=ema20,
        ema50=ema50,
        rsi14=rsi(closes, 14),
        atr14=atr14,
        atr_pct=atr_pct_ratio * 100.0 if atr_pct_ratio is not None else None,
        bands=bollinger(closes, 20),
        macd=macd(closes),
        volume_ratio=safe_div(recent_volume, prior_volume),
        obv_slope_pct=percent_change(obv_series[-1], obv_series[-13]) if len(obv_series) >= 13 else None,
        ema20_slope_pct=percent_change(ema20, ema(closes[:-6], 20)) if len(closes) > 26 else None,
        change_12_pct=percent_change(price, closes[-13]) if len(closes) >= 13 else None,
        candles=len(closes),
    )


__all__ = ["Indicators", "MIN_CANDLES", "compute_indicators"]
