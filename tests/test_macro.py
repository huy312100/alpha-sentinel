"""Cross-asset macro: the same risk-on logic, but for listed instruments."""

from __future__ import annotations

import pytest

from scanners.macro import cross_asset_score, series_momentum, summarise_cross_asset
from tests.test_market_data import chart_payload


class TestSeriesMomentum:
    def test_changes_and_sma(self):
        closes = [100.0] * 45 + [100.0 + index for index in range(55)]  # 100 bars
        entry = series_momentum(closes)
        assert entry["bars"] == 100
        assert entry["level"] == closes[-1]
        assert entry["change_5d_pct"] == pytest.approx((closes[-1] / closes[-6] - 1) * 100)
        assert entry["change_20d_pct"] == pytest.approx((closes[-1] / closes[-21] - 1) * 100)
        assert entry["above_sma50"] is True

    def test_short_series_skips_sma(self):
        entry = series_momentum([10.0, 11.0])
        assert "above_sma50" not in entry
        assert entry["change_5d_pct"] is None

    def test_empty(self):
        assert series_momentum([]) == {}


class TestSummarise:
    def test_builds_a_tape_from_chart_payloads(self):
        payloads = {
            "vix": chart_payload([20.0, 18.0], symbol="^VIX", instrument_type="INDEX"),
            "spx": chart_payload([5000.0, 5100.0], symbol="^GSPC", instrument_type="INDEX"),
            "broken": "not-a-payload",
        }
        metrics = summarise_cross_asset(payloads)
        assert set(metrics) == {"vix", "spx"}
        assert metrics["vix"]["level"] == 18.0
        assert metrics["spx"]["symbol"] == "^GSPC"

    def test_malformed_payloads_are_skipped(self):
        assert summarise_cross_asset({"vix": {}}) == {}


class TestScore:
    def test_calm_tape_is_risk_on(self):
        metrics = {
            "vix": {"level": 13.0, "change_5d_pct": -5.0},
            "dollar": {"level": 100.0, "change_5d_pct": -1.0},
            "us10y": {"level": 4.0, "change_5d_pct": -2.0},
            "spx": {"level": 5000.0, "change_20d_pct": 4.0, "above_sma50": True},
        }
        score, flags, confidence = cross_asset_score(metrics)
        assert score >= 80.0
        assert flags == []
        assert confidence == 0.75

    def test_stressed_tape_is_risk_off_and_flagged(self):
        metrics = {
            "vix": {"level": 32.0, "change_5d_pct": 30.0},
            "dollar": {"level": 110.0, "change_5d_pct": 3.0},
            "us10y": {"level": 4.8, "change_5d_pct": 8.0},
            "spx": {"level": 4200.0, "change_20d_pct": -8.0, "above_sma50": False},
        }
        score, flags, confidence = cross_asset_score(metrics)
        assert score <= 10.0
        assert {
            "vix_elevated",
            "volatility_spiking",
            "dollar_squeeze",
            "yields_rising",
            "equity_downtrend",
        }.issubset(set(flags))
        assert confidence == 0.75

    def test_partial_tape_lowers_confidence(self):
        score, flags, confidence = cross_asset_score({"vix": {"level": 18.0}})
        assert 0.0 <= score <= 100.0
        assert confidence == 0.55

    def test_no_data_refuses_to_score(self):
        score, flags, confidence = cross_asset_score({})
        assert score == 50.0
        assert flags == ["no_cross_asset_data"]
        assert confidence == 0.0
