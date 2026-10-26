"""The technical stack has one implementation, and both layers read it.

These tests are about *identity*, not just behaviour: layer 7 (crypto M15/H1) and
the listed-market technicals layer must be wired to the same maths and the same
score curves, so an "overbought RSI" cannot mean one thing for a token and another
for an equity.
"""

from __future__ import annotations

from core.models import TokenSnapshot
from scanners.base import ScanContext
from scanners.crypto.layer4_dex import buy_sell_imbalance, parse_trades
from scanners.crypto.layer7_technicals import (
    apply_flag_caps,
    indicator_snapshot,
    order_flow_score,
    parse_trade_flow,
    rsi_score,
    trend_score,
)
from scanners.shared import technicals
from scanners.shared.listed.technicals import TechnicalsScanner
from scanners.shared.series import PriceSeries
from scanners.shared.technicals import MIN_CANDLES, compute_indicators
from tests.conftest import TOKEN


def make_candles(count: int = 80) -> dict[str, list[float]]:
    """Deterministic OHLCV with a gentle uptrend and rising volume."""
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    volumes: list[float] = []
    price = 100.0
    for index in range(count):
        price *= 1.004 if index % 3 else 0.997
        closes.append(round(price, 6))
        highs.append(round(price * 1.01, 6))
        lows.append(round(price * 0.99, 6))
        volumes.append(1_000.0 + index * 7.0)
    return {"open": closes, "high": highs, "low": lows, "close": closes, "volume": volumes}


def test_indicator_block_needs_the_documented_minimum():
    assert compute_indicators(make_candles(MIN_CANDLES - 1)) is None
    assert compute_indicators(make_candles(MIN_CANDLES)) is not None
    assert compute_indicators({}) is None


def test_indicator_metrics_are_prefixed_for_multi_timeframe_layers():
    candles = make_candles()
    indicators = compute_indicators(candles)
    assert indicators is not None
    prefixed = indicators.metrics("h1")
    assert prefixed["h1_candles"] == len(candles["close"])
    assert prefixed["h1_rsi14"] == round(indicators.rsi14, 2)
    assert prefixed["h1_ema50"] == indicators.ema50
    assert "price" in indicators.metrics()


async def test_both_technical_layers_read_the_same_indicator_block(settings, monkeypatch):
    candles = make_candles()

    async def fake_series(token, ctx):
        return PriceSeries(candles=candles, bar_interval="1d", change_bars=2, source="test")

    monkeypatch.setattr("scanners.shared.listed.technicals.load_price_series", fake_series)
    signal = await TechnicalsScanner(settings).collect(
        TokenSnapshot(token_address=TOKEN, chain="ethereum", symbol="TEST"),
        ScanContext(None, settings),
    )
    snapshot = indicator_snapshot(candles, label="h1")

    assert signal.metrics["rsi14"] == snapshot["h1_rsi14"]
    assert signal.metrics["ema20"] == snapshot["h1_ema20"]
    assert signal.metrics["ema50"] == snapshot["h1_ema50"]
    assert signal.metrics["atr_pct"] == snapshot["h1_atr_pct"]
    assert signal.metrics["obv_slope_pct"] == snapshot["h1_obv_slope_pct"]


def test_score_curves_have_exactly_one_home():
    assert trend_score is technicals.trend_score
    assert rsi_score is technicals.rsi_score
    assert order_flow_score is technicals.order_flow_score
    assert apply_flag_caps is technicals.apply_flag_caps
    for retired in ("_trend_score", "_momentum_score", "_macd_score", "_volatility_score"):
        assert not hasattr(TechnicalsScanner, retired)


def test_flag_caps_are_identical_for_every_market():
    assert apply_flag_caps(90.0, ["downtrend_structure"]) == 42.0
    assert apply_flag_caps(90.0, ["rsi_overbought"]) == 60.0
    assert apply_flag_caps(90.0, []) == 90.0


def test_trade_flow_reuses_the_dex_trade_parser():
    payload = {
        "data": [
            {"attributes": {"kind": "buy", "volume_in_usd": "120.0", "tx_from_address": "0xAA"}},
            {"attributes": {"kind": "sell", "volume_in_usd": "80.0", "tx_from_address": "0xBB"}},
            {"attributes": {"kind": "buy", "volume_in_usd": "not-a-number", "tx_from_address": "0xCC"}},
        ]
    }
    trades = parse_trades(payload)
    flow = parse_trade_flow(trades)

    assert flow["trades_sampled"] == 2
    assert (flow["buy_volume_usd"], flow["sell_volume_usd"]) == (120.0, 80.0)
    assert flow["order_flow_imbalance"] == 0.2
    # The wash filter and the OFI read must agree on the same tape.
    assert abs(flow["order_flow_imbalance"]) == buy_sell_imbalance(trades)


def test_trade_flow_is_empty_without_usable_prints():
    assert parse_trade_flow([]) == {}
    assert parse_trade_flow(parse_trades({"data": [{"attributes": {"kind": "buy"}}]})) == {}
