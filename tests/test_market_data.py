"""Market-data parsing: provider quirks must never leak into the scores."""

from __future__ import annotations

import pytest

from core.models import TokenSnapshot
from scanners import market_data
from scanners.market_data import (
    MarketQuote,
    fetch_yahoo_chart,
    load_market_series,
    parse_yahoo_chart,
)
from scanners.series import PriceSeries


def chart_payload(
    closes,
    *,
    symbol="AAPL",
    instrument_type="EQUITY",
    volume=1_000_000,
    previous=None,
    null_close_at=(),
):
    n = len(closes)
    quote = {
        "open": list(closes),
        "high": [value * 1.01 for value in closes],
        "low": [value * 0.99 for value in closes],
        "close": [None if index in null_close_at else value for index, value in enumerate(closes)],
        "volume": [None if index in null_close_at else volume for index in range(n)],
    }
    meta = {
        "symbol": symbol,
        "currency": "USD",
        "instrumentType": instrument_type,
        "regularMarketPrice": closes[-1],
        "chartPreviousClose": closes[0] if previous is None else previous,
        "fiftyTwoWeekHigh": max(closes),
        "fiftyTwoWeekLow": min(closes),
        "regularMarketVolume": volume,
        "fullExchangeName": "NasdaqGS",
        "longName": f"{symbol} Inc.",
    }
    return {
        "chart": {
            "result": [
                {
                    "meta": meta,
                    "timestamp": [1_700_000_000 + index * 86_400 for index in range(n)],
                    "indicators": {"quote": [quote]},
                }
            ],
            "error": None,
        }
    }


class TestParsing:
    def test_happy_path(self):
        quote, series = parse_yahoo_chart(chart_payload([100.0, 105.0, 110.0]))
        assert quote.price == 110.0
        # previous_close is yesterday's bar, not the close before the range start.
        assert quote.previous_close == 105.0
        assert quote.change_pct == pytest.approx((110.0 - 105.0) / 105.0 * 100.0)
        assert quote.bars == 3
        assert quote.instrument_type == "EQUITY"
        assert quote.venue == "NasdaqGS"
        assert series["close"] == [100.0, 105.0, 110.0]

    def test_explicit_day_change_from_provider_wins(self):
        payload = chart_payload([100.0, 105.0, 110.0])
        payload["chart"]["result"][0]["meta"]["regularMarketChangePercent"] = -0.42
        quote, _ = parse_yahoo_chart(payload)
        assert quote.change_pct == pytest.approx(-0.42)

    def test_null_close_bars_are_dropped_not_zero_filled(self):
        _, series = parse_yahoo_chart(chart_payload([100.0, 105.0, 110.0], null_close_at=(1,)))
        assert series["close"] == [100.0, 110.0]

    def test_missing_volume_becomes_zero(self):
        payload = chart_payload([100.0, 101.0])
        payload["chart"]["result"][0]["indicators"]["quote"][0]["volume"] = [None, None]
        _, series = parse_yahoo_chart(payload)
        assert series["volume"] == [0.0, 0.0]

    def test_missing_ohlc_fields_drop_the_bar_instead_of_zero_filling(self):
        payload = chart_payload([100.0, 101.0, 102.0])
        quote = payload["chart"]["result"][0]["indicators"]["quote"][0]
        quote["low"] = [99.0, None, 101.0]

        _, series = parse_yahoo_chart(payload)

        # A null low must not become 0.0: that would fabricate an ATR of ~101.
        assert series["close"] == [100.0, 102.0]
        assert series["low"] == [99.0, 101.0]
        assert len(series["open"]) == len(series["close"]) == len(series["high"]) == 2

    def test_provider_error_is_loud(self):
        with pytest.raises(ValueError):
            parse_yahoo_chart({"chart": {"result": None, "error": {"code": "Not Found"}}})

    def test_empty_payload_is_loud(self):
        with pytest.raises(ValueError):
            parse_yahoo_chart({})
        with pytest.raises(ValueError):
            parse_yahoo_chart({"chart": {"result": [], "error": None}})

    def test_asset_class_falls_back_to_declared(self):
        payload = chart_payload([1.0, 2.0], instrument_type="MUTUALFUND")
        quote, _ = parse_yahoo_chart(payload, asset_class="etf")
        assert quote.asset_class == "etf"


class TestQuoteDerivedFields:
    def test_dollar_volume_and_range_position(self):
        quote = MarketQuote(
            symbol="GC",
            provider_symbol="GC=F",
            asset_class="commodity",
            price=4000.0,
            volume=150_000.0,
            fifty_two_week_high=5000.0,
            fifty_two_week_low=3000.0,
        )
        assert quote.dollar_volume == 600_000_000.0
        assert quote.week_range_position == pytest.approx(50.0)
        metrics = quote.to_metrics()
        assert metrics["dollar_volume"] == 600_000_000.0
        assert metrics["week_range_position"] == 50.0

    def test_no_volume_means_no_liquidity_estimate(self):
        quote = MarketQuote(symbol="EURUSD", provider_symbol="EURUSD=X", asset_class="fx", price=1.1, volume=0.0)
        assert quote.dollar_volume is None
        assert quote.week_range_position is None


class FakeContext:
    def __init__(self):
        self.shared = {}
        self.session = None

    def get_shared(self, key):
        return self.shared.get(key)

    def set_shared(self, key, value):
        self.shared[key] = value


class TestFetching:
    async def test_url_encodes_the_provider_symbol(self, monkeypatch):
        captured = {}

        async def fake_request(session, url, **kwargs):
            captured["url"] = url
            captured["params"] = kwargs.get("params")
            return chart_payload([100.0, 101.0], symbol="^GSPC", instrument_type="INDEX")

        monkeypatch.setattr(market_data, "request_json", fake_request)
        quote, _ = await fetch_yahoo_chart(None, "^GSPC")
        assert "%5EGSPC" in captured["url"]
        assert captured["params"]["interval"] == "1d"
        assert quote.asset_class == "index"

    async def test_empty_symbol_is_rejected(self):
        with pytest.raises(ValueError):
            await fetch_yahoo_chart(None, "")

    async def test_load_market_series_caches_within_one_collection(self, monkeypatch):
        calls = []

        async def fake_fetch(session, provider_symbol, **kwargs):
            calls.append(provider_symbol)
            return (MarketQuote(symbol="AAPL", provider_symbol=provider_symbol, asset_class="equity"), {})

        monkeypatch.setattr(market_data, "fetch_yahoo_chart", fake_fetch)
        ctx = FakeContext()
        token = TokenSnapshot("AAPL", chain="us_equity", asset_class="equity", symbol="AAPL")
        first = await load_market_series(token, ctx)
        second = await load_market_series(token, ctx)
        assert first is second
        assert calls == ["AAPL"]

    async def test_crypto_has_no_provider_symbol(self):
        token = TokenSnapshot("0xabc", asset_class="crypto")
        with pytest.raises(ValueError):
            await load_market_series(token, FakeContext())


class TestPriceSeries:
    def test_daily_change_uses_one_bar(self):
        series = PriceSeries(candles={"close": [100.0, 110.0]}, bar_interval="1d", change_bars=2)
        assert series.change_pct() == pytest.approx(10.0)
        assert series.bar_hours == 24.0

    def test_crypto_change_uses_a_full_day_of_hourly_bars(self):
        closes = [100.0] * 24 + [120.0]
        series = PriceSeries(candles={"close": closes}, bar_interval="1h", change_bars=25)
        assert series.change_pct() == pytest.approx(20.0)
        assert series.bar_hours == 1.0

    def test_not_enough_bars_is_none(self):
        series = PriceSeries(candles={"close": [100.0]}, bar_interval="1d", change_bars=2)
        assert series.change_pct() is None
