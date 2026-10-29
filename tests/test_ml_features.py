"""The feature contract: missing stays missing, and the layout never drifts."""

from __future__ import annotations

import math

from core.models import Layer, LayerSignal, SecurityReport, SignalStatus, TokenSnapshot, WashTradingReport
from ml import FEATURE_SCHEMA_VERSION
from ml.features import (
    CORE_FEATURES,
    LAYERS,
    MAX_METRIC_KEYS_PER_LAYER,
    STATUS_CODES,
    align,
    coverage,
    flatten,
)


def snapshot(**overrides) -> TokenSnapshot:
    defaults = {
        "token_address": "0x" + "ab" * 20,
        "chain": "ethereum",
        "asset_class": "crypto",
        "symbol": "TEST",
        "price_usd": 1.5,
        "market_cap_usd": 10_000_000.0,
        "fdv_usd": 12_000_000.0,
        "liquidity_usd": 500_000.0,
        "volume_24h_usd": 2_000_000.0,
        "volume_1h_usd": 100_000.0,
        "price_change_24h_pct": 4.5,
        "price_change_1h_pct": 0.5,
        "holder_count": 5_000,
        "age_hours": 720.0,
        "security": SecurityReport(available=True, top10_holder_pct=30.0, lp_locked_pct=90.0),
        "wash": WashTradingReport(evaluated=True, suspicion_score=12.0, trade_count=400, unique_wallets=200),
        "signals": [
            LayerSignal(layer=Layer.DEX, score=78.0, confidence=0.9, weight=1.0, status=SignalStatus.OK),
            LayerSignal(layer=Layer.SOCIAL, score=64.0, confidence=0.6, weight=1.0, status=SignalStatus.DEGRADED),
            LayerSignal(layer=Layer.DERIVATIVES, score=10.0, confidence=0.0, weight=1.0, status=SignalStatus.UNAVAILABLE),
            LayerSignal(layer=Layer.MACRO, score=50.0, confidence=0.0, weight=0.5, status=SignalStatus.SKIPPED),
        ],
    }
    defaults.update(overrides)
    return TokenSnapshot(**defaults)


def test_every_layer_is_present_even_when_the_provider_is_silent():
    row = flatten(snapshot())

    for layer in LAYERS:
        assert f"{layer}_score" in row, layer
        assert f"{layer}_status" in row, layer

    # A layer that never answered is a hole, not a neutral 50 - otherwise a
    # failing provider would look like a balanced reading.
    assert math.isnan(row["onchain_score"])
    assert row["onchain_contributes"] == 0.0
    assert row["dex_score"] == 78.0
    assert row["dex_contributes"] == 1.0


def test_status_codes_keep_skipped_apart_from_unavailable():
    row = flatten(snapshot())

    assert row["dex_status"] == STATUS_CODES["ok"] == 1.0
    assert row["social_status"] == STATUS_CODES["degraded"] == 0.5
    assert row["derivatives_status"] == STATUS_CODES["unavailable"] == 0.0
    assert row["macro_status"] == STATUS_CODES["skipped"] == -1.0


def test_missing_market_data_becomes_nan_not_zero():
    row = flatten(snapshot(volume_24h_usd=None, price_change_24h_pct=None))

    assert math.isnan(row["volume_24h_usd"])
    assert math.isnan(row["price_change_24h_pct"])
    # A ratio with a hole in it is a hole too.
    assert math.isnan(row["volume_1h_share"])


def test_ratios_never_produce_infinity():
    row = flatten(snapshot(market_cap_usd=0.0, holder_count=0, volume_24h_usd=0.0))

    assert math.isnan(row["turnover_24h_ratio"])
    assert math.isnan(row["liquidity_to_mcap"])
    assert math.isnan(row["mcap_per_holder"])
    assert math.isnan(row["volume_1h_share"])
    for value in row.values():
        assert not math.isinf(value)


def test_boolean_flags_are_numeric():
    security = SecurityReport(available=True, honeypot=True, mintable=True, flags=["honeypot"])
    row = flatten(snapshot(security=security))

    assert row["sec_available"] == 1.0
    assert row["sec_honeypot"] == 1.0
    assert row["sec_mintable"] == 1.0
    assert row["sec_is_proxy"] == 0.0
    assert row["sec_flag_count"] == 1.0
    assert row["sec_critical_flag_count"] == 1.0


def test_metrics_are_namespaced_and_capped():
    metrics = {f"m{index}": float(index) for index in range(MAX_METRIC_KEYS_PER_LAYER + 10)}
    metrics.update({"note": "not a number", "hole": None})
    signals = [
        LayerSignal(layer=Layer.DEX, score=70.0, confidence=0.8, weight=1.0, metrics=metrics),
    ]
    row = flatten(snapshot(signals=signals))

    namespaced = [key for key in row if key.startswith("dex_m")]
    assert 0 < len(namespaced) <= MAX_METRIC_KEYS_PER_LAYER
    assert "dex_note" not in row and "dex_hole" not in row
    assert row["dex_m0"] == 0.0


def test_structural_columns_win_over_colliding_metrics():
    signals = [
        LayerSignal(layer=Layer.DEX, score=70.0, confidence=0.8, weight=1.0, metrics={"score": 999.0}),
    ]
    row = flatten(snapshot(signals=signals))

    assert row["dex_score"] == 70.0


def test_the_layout_is_deterministic():
    first = flatten(snapshot())
    second = flatten(snapshot(symbol="OTHER"))

    assert list(first) == list(second)
    assert FEATURE_SCHEMA_VERSION >= 1


def test_align_projects_onto_the_trained_column_order():
    row = {"b": 2.0, "d": 4.0}

    values = align(row, ["a", "b", "c"])

    assert math.isnan(values[0])
    assert values[1] == 2.0
    assert math.isnan(values[2])


def test_coverage_counts_only_core_features():
    full = flatten(snapshot())
    full["prior_score"] = 60.0
    assert coverage(full) == 1.0

    # An empty snapshot carries nothing: no coverage, so any prediction built on
    # it must be escalated rather than trusted.
    assert coverage(flatten(TokenSnapshot(token_address="0x1", chain="ethereum"))) == 0.0

    partial = flatten(TokenSnapshot(token_address="0x1", chain="ethereum", price_usd=2.0))
    assert coverage(partial) == 1 / len(CORE_FEATURES)
    assert len(CORE_FEATURES) == 11
