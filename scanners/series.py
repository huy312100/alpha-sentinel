"""Price-series abstraction: one shape for crypto and every other market.

The indicator code should not care whether candles came from a DEX pool or from
an exchange feed. This module resolves the right provider for an instrument's
asset class and returns a :class:`PriceSeries` with consistent keys, oldest-first
ordering and an explicit bar interval.

* **Crypto** - GeckoTerminal hourly OHLCV for the deepest pool, exactly as before.
* **Everything else** - Yahoo daily bars (one year), shared with the market layer
  so a cycle fetches each instrument once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.http import request_json
from core.instruments import AssetClass
from core.models import TokenSnapshot, get_chain_profile
from scanners.market_data import load_market_series

GECKO_OHLCV_URL = "https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/ohlcv/hour"

#: Hourly crypto bars: 24 bars = one day, so momentum needs 25 closes.
CRYPTO_CHANGE_BARS = 25
#: Daily bars: one session = one day.
DAILY_CHANGE_BARS = 2


class SeriesUnavailable(ValueError):
    """Raised when a price series legitimately does not exist for this instrument."""


@dataclass(slots=True)
class PriceSeries:
    """Normalised OHLCV plus the context needed to interpret it."""

    candles: dict[str, list[float]] = field(default_factory=dict)
    bar_interval: str = "1h"
    change_bars: int = CRYPTO_CHANGE_BARS
    source: str = ""

    @property
    def closes(self) -> list[float]:
        return self.candles.get("close", [])

    @property
    def highs(self) -> list[float]:
        return self.candles.get("high", [])

    @property
    def lows(self) -> list[float]:
        return self.candles.get("low", [])

    @property
    def volumes(self) -> list[float]:
        return self.candles.get("volume", [])

    @property
    def bar_hours(self) -> float:
        return 24.0 if self.bar_interval.endswith("d") else 1.0

    def change_pct(self) -> float | None:
        """Percent change over one day of bars (24 hourly or 1 daily session)."""
        closes = self.closes
        if len(closes) < self.change_bars:
            return None
        previous = closes[-self.change_bars]
        if previous == 0:
            return None
        return (closes[-1] - previous) / previous * 100.0


def parse_ohlcv(payload: Any) -> dict[str, list[float]]:
    """Normalise GeckoTerminal's newest-first candle list into oldest-first series."""
    from core.http import to_float

    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    rows = ((data or {}).get("attributes") or {}).get("ohlcv_list") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        return {}
    ordered = sorted(rows, key=lambda row: to_float((row or [0])[0]) or 0.0)
    series: dict[str, list[float]] = {"timestamps": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    for row in ordered:
        if not isinstance(row, list) or len(row) < 6:
            continue
        values = [to_float(cell) for cell in row[:6]]
        if any(value is None for value in values):
            continue
        series["timestamps"].append(values[0])
        series["open"].append(values[1])
        series["high"].append(values[2])
        series["low"].append(values[3])
        series["close"].append(values[4])
        series["volume"].append(values[5])
    return series if series["close"] else {}


async def load_price_series(token: TokenSnapshot, ctx: Any) -> PriceSeries:
    """Resolve the provider for ``token``'s asset class and return its candles."""
    kind = AssetClass.coerce(token.asset_class)
    if kind is AssetClass.CRYPTO:
        return await _crypto_series(token, ctx)
    quote, candles = await load_market_series(token, ctx)
    return PriceSeries(
        candles=candles,
        bar_interval="1d",
        change_bars=DAILY_CHANGE_BARS,
        source=f"yahoo:{quote.provider_symbol}",
    )


async def _crypto_series(token: TokenSnapshot, ctx: Any) -> PriceSeries:
    from scanners.dex import load_market

    market = await load_market(token, ctx)
    pool = (market.pair_address if market else None) or token.pair_address or ""
    if not pool:
        raise SeriesUnavailable("no pool address available for OHLCV")
    network = get_chain_profile(token.chain).geckoterminal
    payload = await request_json(
        ctx.session,
        GECKO_OHLCV_URL.format(network=network, pool=pool),
        params={"aggregate": 1, "limit": 200},
    )
    return PriceSeries(
        candles=parse_ohlcv(payload),
        bar_interval="1h",
        change_bars=CRYPTO_CHANGE_BARS,
        source=f"geckoterminal:{pool}",
    )


__all__ = [
    "CRYPTO_CHANGE_BARS",
    "DAILY_CHANGE_BARS",
    "GECKO_OHLCV_URL",
    "PriceSeries",
    "SeriesUnavailable",
    "load_price_series",
    "parse_ohlcv",
]
