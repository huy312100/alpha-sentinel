"""training_rows: one row per audit, labels never leaked into the feature matrix."""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd

from core.models import AlphaAssessment, Layer, LayerSignal, SecurityReport, TokenSnapshot, WashTradingReport
from ml import dataset
from ml.dataset import (
    LABEL_COLUMNS,
    TRAINING_DATASET,
    baseline_report,
    feature_matrix,
    load_training_rows,
    training_row,
    write_training_rows,
)
from scanners.pipeline import CollectedSnapshot
from storage.parquet_store import ParquetStore

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def snapshot(**overrides) -> TokenSnapshot:
    defaults = {
        "token_address": "0x" + "cd" * 20,
        "chain": "ethereum",
        "asset_class": "crypto",
        "symbol": "TEST",
        "price_usd": 2.5,
        "market_cap_usd": 20_000_000.0,
        "liquidity_usd": 900_000.0,
        "volume_24h_usd": 3_000_000.0,
        "holder_count": 9_000,
        "security": SecurityReport(available=True),
        "wash": WashTradingReport(evaluated=True, suspicion_score=8.0),
        "signals": [
            LayerSignal(layer=Layer.DEX, score=77.0, confidence=0.9, weight=1.0),
            LayerSignal(layer=Layer.SOCIAL, score=61.0, confidence=0.5, weight=1.0),
        ],
        "collected_at": NOW,
    }
    defaults.update(overrides)
    return TokenSnapshot(**defaults)


def collected(**overrides) -> CollectedSnapshot:
    return CollectedSnapshot(
        snapshot=overrides.pop("snapshot", snapshot()),
        prior_score=overrides.pop("prior_score", 68.0),
        duration_ms=overrides.pop("duration_ms", 12.5),
        layer_status={},
    )


def outcome(**overrides) -> SimpleNamespace:
    defaults = {
        "alpha": AlphaAssessment(alpha_score=72.5, confidence=0.7, thesis="scripted", model="scripted-alpha"),
        "risk": SimpleNamespace(risk_score=18.0, effective_veto=lambda: False, model="scripted-risk"),
        "verdict": SimpleNamespace(final_score=68.4, status=SimpleNamespace(value="approved")),
        "prior_score": 68.0,
        "degraded": False,
        "prompt_version": "v3-test",
        "alpha_source": "llm",
        "escalation_reason": None,
        "model_alpha_score": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_a_row_carries_features_labels_and_provenance():
    row = training_row(collected(), outcome())

    # identity
    assert row["token_address"] == "0x" + "cd" * 20
    assert row["chain"] == "ethereum"
    assert row["observed_at"] == NOW
    assert row["feature_schema_version"] >= 1
    # features
    assert row["dex_score"] == 77.0
    assert row["prior_score"] == 68.0
    # labels
    assert row["alpha_score"] == 72.5
    assert row["llm_alpha_score"] == 72.5
    assert row["risk_score"] == 18.0
    assert row["risk_veto"] == 0.0
    assert row["final_score"] == 68.4
    assert row["final_status"] == "approved"
    # provenance
    assert row["alpha_model"] == "scripted-alpha"
    assert row["prompt_version"] == "v3-test"
    assert row["scan_duration_ms"] == 12.5


def test_a_model_decision_leaves_the_llm_label_empty():
    row = training_row(
        collected(),
        outcome(alpha_source="model", model_alpha_score=71.0, escalation_reason=None),
    )

    assert row["alpha_source"] == "model"
    assert row["alpha_score"] == 72.5
    assert row["model_alpha_score"] == 71.0
    # No LLM scored this token, so there is no teacher label to learn from.
    assert math.isnan(row["llm_alpha_score"])


def test_write_and_read_roundtrip(tmp_path):
    cold = ParquetStore(tmp_path / "lake")
    rows = [training_row(collected(), outcome())]

    assert write_training_rows(cold, rows) == 1

    frame = load_training_rows(cold)
    assert len(frame) == 1
    assert frame.iloc[0]["token_address"] == "0x" + "cd" * 20
    assert cold.dataset_path(TRAINING_DATASET).exists()


def test_loading_sorts_by_observation_time(tmp_path):
    cold = ParquetStore(tmp_path / "lake")
    later = training_row(collected(snapshot=snapshot(collected_at=NOW + timedelta(hours=2))), outcome())
    earlier = training_row(collected(snapshot=snapshot(collected_at=NOW)), outcome())
    write_training_rows(cold, [later, earlier])

    frame = load_training_rows(cold)

    assert list(pd.to_datetime(frame["observed_at"], utc=True)) == sorted(
        pd.to_datetime(frame["observed_at"], utc=True)
    )


def test_no_rows_means_no_dataset(tmp_path):
    cold = ParquetStore(tmp_path / "lake")

    assert write_training_rows(cold, []) == 0
    assert not cold.dataset_path(TRAINING_DATASET).exists()


def test_the_feature_matrix_excludes_labels_and_metadata():
    frame = pd.DataFrame(
        [
            training_row(collected(), outcome()),
            training_row(collected(), outcome()),
        ]
    )

    matrix, columns = feature_matrix(frame)

    assert "dex_score" in columns
    assert "prior_score" in columns
    for excluded in ("llm_alpha_score", "final_status", "alpha_source", "token_address", "observed_at"):
        assert excluded not in columns
    assert set(columns).isdisjoint(set(LABEL_COLUMNS))
    assert len(matrix) == 2


def test_the_baseline_reports_perfect_agreement_as_zero_error():
    frame = pd.DataFrame({"prior_score": [60.0, 70.0, 40.0], "llm_alpha_score": [60.0, 70.0, 40.0]})

    report = baseline_report(frame)

    assert report["rows"] == 3
    assert report["prior_mae"] == 0.0
    assert report["pearson_r"] == 1.0
    assert report["band_flips_65"] == 0.0


def test_the_baseline_counts_decision_flips():
    frame = pd.DataFrame(
        {
            "prior_score": [60.0, 70.0],
            "llm_alpha_score": [70.0, 60.0],  # both cross the 65 floor
        }
    )

    report = baseline_report(frame)

    assert report["band_flips_65"] == 1.0
    assert report["prior_mae"] == 10.0


def test_the_baseline_tolerates_a_frame_without_labels():
    report = baseline_report(pd.DataFrame({"prior_score": [60.0], "alpha_score": [float("nan")]}))

    assert report["rows"] == 0
    assert "reason" in report


def test_the_baseline_tolerates_an_empty_frame():
    assert baseline_report(pd.DataFrame())["rows"] == 0


def test_the_baseline_never_needs_scipy(monkeypatch):
    """The export runs on requirements.txt alone; scipy is a training-only extra.

    pandas' ``method="spearman"`` imports scipy under the hood, which broke CI
    while passing locally in a venv where scikit-learn had pulled scipy in.
    """
    monkeypatch.setitem(sys.modules, "scipy", None)
    monkeypatch.setitem(sys.modules, "scipy.stats", None)
    frame = pd.DataFrame(
        {"prior_score": [60.0, 70.0, 40.0, 30.0], "llm_alpha_score": [62.0, 68.0, 41.0, 33.0]}
    )

    report = baseline_report(frame)

    # Same order, different values: rank correlation is exactly 1, Pearson is not.
    assert report["spearman_r"] == 1.0
    assert report["pearson_r"] < 1.0


def test_export_writes_a_parquet_file(tmp_path):
    cold = ParquetStore(tmp_path / "lake")
    write_training_rows(cold, [training_row(collected(), outcome())])

    destination = tmp_path / "exports" / "train.parquet"
    code = dataset._main(["export", "--out", str(destination), "--baseline"], cold=cold)

    assert code == 0
    assert destination.exists()
    assert len(pd.read_parquet(destination)) == 1


def test_export_without_rows_exits_non_zero(tmp_path):
    cold = ParquetStore(tmp_path / "empty")

    assert dataset._main(["export", "--out", str(tmp_path / "x.parquet")], cold=cold) == 1


def test_an_unwritable_lake_is_reported_not_raised(tmp_path):
    class Broken(ParquetStore):
        def write_frame(self, *args, **kwargs):
            raise OSError("disk full")

    assert write_training_rows(Broken(tmp_path / "lake"), [training_row(collected(), outcome())]) == 0


def test_rows_are_partitioned_by_the_observation_date(tmp_path):
    cold = ParquetStore(tmp_path / "lake")
    write_training_rows(cold, [training_row(collected(), outcome())])

    assert cold.partitions(TRAINING_DATASET) == ["2026-10-29"]
