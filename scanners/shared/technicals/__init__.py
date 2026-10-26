"""The technical-analysis library: arithmetic, snapshot, score curves, flags.

This package is *not* a layer. It is the one implementation of the maths both
technical layers use, split by analysis family so each file can be read and
tested on its own:

``primitives``
    SMA/EMA/RSI/MACD/ATR/Bollinger/z-score/OBV/OFI. Pure functions over
    ``Sequence[float]``, no scoring, no vendors.
``snapshot``
    :class:`Indicators` and :func:`compute_indicators` - one block of indicator
    values, computed once per timeframe and read by every layer.
``scoring``
    Trend/momentum/RSI/MACD/volatility/order-flow curves mapping those readings
    onto 0-100. Weights stay in the layers; the curve shape stays here.
``flags``
    ``rsi_overbought`` / ``downtrend_structure`` and the score ceilings they imply.

The two layers that consume this package are deliberately separate files, because
they differ in inputs and weighting, not in maths:

* ``scanners/crypto/layer7_technicals.py`` - M15/H1 GeckoTerminal candles plus the
  DEX trade tape (order-flow imbalance).
* ``scanners/shared/listed/technicals.py`` - daily Yahoo bars for equities, ETFs,
  indices, FX, commodities and rates, with the EMA20 slope as an accelerator.

Keeping them apart is what lets the crypto layer own a multi-timeframe read and an
order-flow component that a rate chart has no data for, while both still answer
"what does RSI 62 mean?" with the same code.
"""

from __future__ import annotations

from scanners.shared.technicals.flags import apply_flag_caps, rsi_flags, structure_flags
from scanners.shared.technicals.primitives import (
    atr,
    bollinger,
    ema,
    ema_series,
    macd,
    obv,
    order_flow_imbalance,
    percent_change,
    rsi,
    sma,
    zscore,
)
from scanners.shared.technicals.scoring import (
    macd_score,
    momentum_score,
    order_flow_score,
    rsi_score,
    trend_score,
    volatility_score,
)
from scanners.shared.technicals.snapshot import MIN_CANDLES, Indicators, compute_indicators

__all__ = [
    "Indicators",
    "MIN_CANDLES",
    "apply_flag_caps",
    "atr",
    "bollinger",
    "compute_indicators",
    "ema",
    "ema_series",
    "macd",
    "macd_score",
    "momentum_score",
    "obv",
    "order_flow_imbalance",
    "order_flow_score",
    "percent_change",
    "rsi",
    "rsi_flags",
    "rsi_score",
    "sma",
    "structure_flags",
    "trend_score",
    "volatility_score",
    "zscore",
]
