"""Keyless market data for non-crypto instruments (Yahoo Finance chart API).

Crypto has excellent free microstructure providers (DexScreener, GeckoTerminal).
Everything else - equities, ETFs, indices, FX, futures, rates - has one reliable
keyless source with a stable contract: the Yahoo chart endpoint. It returns the
same OHLCV shape our indicator code already consumes, plus a metadata block with
price, previous close, 52-week range, currency and exchange, which is everything
the market layer needs to describe liquidity and position.

Design rules
------------
* **One fetch, many readers.** The quote and the candles come from the same
  response, so the market and technicals layers share it through ``ScanContext``.
* **No fabrication.** A missing bar is dropped, not zero-filled; a halt or an
  FX holiday must not look like a 0% return.
* **Provider syntax stays here.** Operators write ``EURUSD``; this module knows
  the request needs ``EURUSD=X`` (see :mod:`core.instruments`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote as url_quote

import aiohttp

from config.settings import EndpointSettings
from core.http import request_json, to_float
from core.instruments import (
    AssetClass,
    asset_class_for_yahoo_type,
    provider_symbol_for_snapshot,
)
from core.models import TokenSnapshot

DEFAULT_RANGE = "1y"
DEFAULT_INTERVAL = "1d"
#: Trading days per year used to annualise volatility from daily bars.
ANNUALISATION_DAYS = 252

_SERIES_KEYS = ("open", "high", "low", "close", "volume")


@dataclass(slots=True)
class MarketQuote:
    """Best-known read for one non-crypto instrument."""

    symbol: str
    provider_symbol: str
    asset_class: str
    price: float | None = None
    previous_close: float | None = None
    change_pct: float | None = None
    volume: float | None = None
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None
    currency: str = ""
    venue: str = ""
    instrument_type: str = ""
    name: str = ""
    bars: int = 0
    source: str = "yahoo"

    @property
    def dollar_volume(self) -> float | None:
        """Traded value, not share count - the only liquidity proxy we get free."""
        if self.price is None or self.volume is None or self.volume <= 0:
            return None
        return self.price * self.volume

    @property
    def week_range_position(self) -> float | None:
        """0 = at the 52-week low, 100 = at the high."""
        low, high, price = self.fifty_two_week_low, self.fifty_two_week_high, self.price
        if low is None or high is None or price is None or high <= low:
            return None
        return max(0.0, min(100.0, (price - low) / (high - low) * 100.0))

    def to_metrics(self) -> dict[str, Any]:
        return {
            "provider_symbol": self.provider_symbol,
            "price": self.price,
            "previous_close": self.previous_close,
            "change_pct": round(self.change_pct, 3) if self.change_pct is not None else None,
            "volume": self.volume,
            "dollar_volume": round(self.dollar_volume, 2) if self.dollar_volume is not None else None,
            "fifty_two_week_high": self.fifty_two_week_high,
            "fifty_two_week_low": self.fifty_two_week_low,
            "week_range_position": (
                round(self.week_range_position, 1) if self.week_range_position is not None else None
            ),
            "currency": self.currency,
            "venue": self.venue,
            "instrument_type": self.instrument_type,
            "bars": self.bars,
            "source": self.source,
        }


def _chart_result(payload: Any) -> dict[str, Any]:
    chart = (payload or {}).get("chart") if isinstance(payload, Mapping) else None
    if not isinstance(chart, Mapping):
        raise ValueError(f"unexpected market-data response: {str(payload)[:200]}")
    error = chart.get("error")
    if error:
        raise ValueError(f"market-data provider error: {str(error)[:200]}")
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], Mapping):
        raise ValueError("market-data response contained no result")
    return dict(results[0])


def _series_from_result(result: Mapping[str, Any]) -> dict[str, list[float]]:
    """Zipped OHLCV arrays, oldest first, with incomplete bars dropped."""
    timestamps = result.get("timestamp") or []
    quote_block = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    if not isinstance(quote_block, Mapping):
        return {}
    arrays = {key: quote_block.get(key) or [] for key in _SERIES_KEYS}
    series: dict[str, list[float]] = {"timestamps": []}
    for key in _SERIES_KEYS:
        series[key] = []
    for index, stamp in enumerate(timestamps):
        row: dict[str, float] = {}
        for key in _SERIES_KEYS:
            column = arrays[key]
            value = to_float(column[index]) if index < len(column) else None
            if value is None:
                if key == "volume":
                    # Volume is optional: OBV still needs a defined sign, and a
                    # missing volume is not a missing price.
                    row[key] = 0.0
                    continue
                # Yahoo pads halted sessions with nulls. A null open/high/low/close
                # means the bar is incomplete - drop it rather than fabricate 0.0,
                # which would corrupt ATR and every range read.
                row = {}
                break
            row[key] = value
        if not row:
            continue
        series["timestamps"].append(float(stamp))
        for key in _SERIES_KEYS:
            series[key].append(row[key])
    return series if series["close"] else {}


def parse_yahoo_chart(
    payload: Any,
    *,
    fallback_symbol: str = "",
    asset_class: str | AssetClass | None = None,
) -> tuple[MarketQuote, dict[str, list[float]]]:
    """Normalise a chart response into ``(quote, series)``."""
    result = _chart_result(payload)
    meta = result.get("meta") or {}
    series = _series_from_result(result)

    price = to_float(meta.get("regularMarketPrice"))
    closes = series.get("close") or []
    if price is None and closes:
        price = closes[-1]
    # ``chartPreviousClose`` is the close *before the requested range* (a year for
    # range=1y), which is not yesterday. Prefer the explicit day-over-day fields
    # and fall back to the second-to-last bar, so "change" always means 1 session.
    previous = to_float(meta.get("previousClose"))
    if previous is None and len(closes) >= 2:
        previous = closes[-2]
    if previous is None:
        previous = to_float(meta.get("chartPreviousClose"))
    change = to_float(meta.get("regularMarketChangePercent"))
    if change is None and price is not None and previous not in (None, 0):
        change = (price - previous) / previous * 100.0
    volume = to_float(meta.get("regularMarketVolume"))
    if volume is None and series.get("volume"):
        volume = series["volume"][-1]

    provider_symbol = str(meta.get("symbol") or fallback_symbol or "").strip()
    declared = AssetClass.coerce(asset_class) if asset_class is not None else None
    kind = asset_class_for_yahoo_type(meta.get("instrumentType")) or declared or AssetClass.EQUITY

    if price is None and not series:
        raise ValueError(f"market-data response for {provider_symbol or fallback_symbol!r} had no prices")

    quote = MarketQuote(
        symbol=fallback_symbol or provider_symbol,
        provider_symbol=provider_symbol or fallback_symbol,
        asset_class=kind.value,
        price=price,
        previous_close=previous,
        change_pct=change,
        volume=volume,
        fifty_two_week_high=to_float(meta.get("fiftyTwoWeekHigh")),
        fifty_two_week_low=to_float(meta.get("fiftyTwoWeekLow")),
        currency=str(meta.get("currency") or ""),
        venue=str(meta.get("fullExchangeName") or meta.get("exchangeName") or ""),
        instrument_type=str(meta.get("instrumentType") or ""),
        name=str(meta.get("longName") or meta.get("shortName") or ""),
        bars=len(series.get("close", [])),
    )
    return quote, series


def endpoints_for(ctx: Any) -> EndpointSettings:
    """Configured vendor hosts for this scan.

    ``ScanContext`` always carries them; a test that fakes the context with only a
    ``session`` gets the canonical defaults instead of an ``AttributeError``.
    """
    settings = getattr(ctx, "settings", None)
    return getattr(settings, "endpoints", None) or EndpointSettings()


async def fetch_yahoo_chart(
    session: aiohttp.ClientSession,
    provider_symbol: str,
    *,
    endpoints: EndpointSettings | None = None,
    range_: str = DEFAULT_RANGE,
    interval: str = DEFAULT_INTERVAL,
) -> tuple[MarketQuote, dict[str, list[float]]]:
    """Fetch one instrument's chart and normalise it."""
    if not provider_symbol:
        raise ValueError("provider symbol is empty")
    url = (endpoints or EndpointSettings()).url(
        "yahoo.chart", symbol=url_quote(provider_symbol, safe="")
    )
    payload = await request_json(
        session,
        url,
        params={"range": range_, "interval": interval, "includePrePost": "false"},
    )
    return parse_yahoo_chart(payload, fallback_symbol=provider_symbol)


async def load_market_series(
    token: TokenSnapshot,
    ctx: Any,
    *,
    range_: str = DEFAULT_RANGE,
    interval: str = DEFAULT_INTERVAL,
) -> tuple[MarketQuote, dict[str, list[float]]]:
    """Quote + candles for a non-crypto instrument, cached for the collection."""
    provider_symbol = provider_symbol_for_snapshot(token)
    if not provider_symbol:
        raise ValueError(f"no provider symbol for {token.token_address!r} ({token.asset_class})")
    cache_key = f"market:{provider_symbol}"
    cached = ctx.get_shared(cache_key)
    if cached is not None:
        return cached
    result = await fetch_yahoo_chart(
        ctx.session,
        provider_symbol,
        endpoints=endpoints_for(ctx),
        range_=range_,
        interval=interval,
    )
    ctx.set_shared(cache_key, result)
    return result


async def load_quote(token: TokenSnapshot, ctx: Any) -> MarketQuote:
    """Quote only - used by the pipeline to populate the snapshot."""
    quote, _ = await load_market_series(token, ctx)
    return quote


__all__ = [
    "ANNUALISATION_DAYS",
    "DEFAULT_INTERVAL",
    "DEFAULT_RANGE",
    "MarketQuote",
    "endpoints_for",
    "fetch_yahoo_chart",
    "load_market_series",
    "load_quote",
    "parse_yahoo_chart",
]
