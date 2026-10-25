"""Shared clients and libraries used by every scanner package.

Nothing in here knows about a specific market. A layer module imports from this
package instead of talking to ``aiohttp`` or re-implementing indicator maths, so
one retry policy, one rate limiter and one RSI implementation serve crypto,
equities, FX and everything added later.

Contents
--------
``http_client``
    Rate-limited, retrying JSON transport plus :class:`ProviderResult`, the
    fail-closed wrapper that turns a dead vendor into a skipped layer instead of
    an aborted scan.
``technicals``
    Pure indicator functions (SMA/EMA/RSI/MACD/ATR/OBV/Bollinger/z-score) and
    order-flow imbalance - auditable maths with no ``pandas``/``TA-Lib`` runtime.
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
