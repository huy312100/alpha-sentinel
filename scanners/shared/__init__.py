"""Shared clients and libraries used by every scanner package.

A layer module imports from this package instead of talking to ``aiohttp`` or
re-implementing indicator maths, so one retry policy, one rate limiter and one
RSI implementation serve crypto, equities, FX and everything added later.

Contents
--------
``http_client``
    Rate-limited, retrying JSON transport plus :class:`ProviderResult`, the
    fail-closed wrapper that turns a dead vendor into a skipped layer instead of
    an aborted scan.
``technicals``
    The technical-analysis library, one module per analysis family:
    ``primitives`` (SMA/EMA/RSI/MACD/ATR/OBV/Bollinger/z-score), ``snapshot``
    (:class:`Indicators` / :func:`compute_indicators`), ``scoring`` (0-100 curves)
    and ``flags`` (exhaustion/structure flags plus their score ceilings). Pure
    functions, no ``pandas``/``TA-Lib`` runtime; both technical layers read them.
``market_data``
    Keyless venue quotes and daily candles (Yahoo chart API) for every listed
    instrument, with the parsing quirks normalised at the edge.
``series``
    :class:`PriceSeries` and :func:`load_price_series`: one candle shape for
    crypto (GeckoTerminal) and listed venues (Yahoo), so indicators never branch
    on asset class. The crypto leg is imported lazily to keep the dependency
    arrow pointing ``crypto -> shared``.
``listed``
    The generic non-crypto layers (market, macro, fundamentals, technicals) that
    the placeholder market packages compose until each market ships its own.
"""

from __future__ import annotations

from scanners.shared.http_client import (
    DEFAULT_REQUESTS_PER_SECOND,
    ProviderResult,
    fetch_provider,
    first_float,
    first_int,
    first_row,
    get_json,
    post_json,
    request_json,
    to_float,
    to_int,
)
from scanners.shared.technicals import (
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

__all__ = [
    "DEFAULT_REQUESTS_PER_SECOND",
    "ProviderResult",
    "atr",
    "bollinger",
    "ema",
    "ema_series",
    "fetch_provider",
    "first_float",
    "first_int",
    "first_row",
    "get_json",
    "macd",
    "obv",
    "order_flow_imbalance",
    "percent_change",
    "post_json",
    "request_json",
    "rsi",
    "sma",
    "to_float",
    "to_int",
    "zscore",
]
