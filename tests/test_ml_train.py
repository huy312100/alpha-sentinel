"""Training: refuse toy data, split by time, and beat the prior or say so.

These tests need the optional extras (``requirements-ml.txt``) and are skipped
without scikit-learn, exactly like the service does not need it to keep scanning.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from ml.train import (  # noqa: E402 - after the importorskip guard
    MIN_TRAIN_ROWS,
    TrainingDataError,
    load_artifact,
    save_artifact,
    train_alpha_model,
)


def synthetic_frame(rows: int = 400, seed: int = 7, noise: float = 4.0) -> pd.DataFrame:
    """A label the prior cannot explain on its own: a nonlinear mix of features."""
    rng = np.random.default_rng(seed)
    prior = rng.uniform(20.0, 90.0, rows)
    dex = rng.uniform(0.0, 100.0, rows)
    liquidity = rng.uniform(1e5, 5e6, rows)
    label = 35.0 + 0.45 * prior + 0.25 * dex - 4.0 * (np.log10(liquidity) - 6.0) + rng.normal(0.0, noise, rows)
    return pd.DataFrame(
        {
            "observed_at": pd.date_range("2026-01-01", periods=rows, freq="h", tz="UTC"),
            "token_address": [f"0x{index:040x}" for index in range(rows)],
            "prior_score": prior,
            "dex_score": dex,
            "liquidity_usd": liquidity,
            "llm_alpha_score": label,
        }
    )


def test_refuses_a_dataset_below_the_floor():
    with pytest.raises(TrainingDataError, match=str(MIN_TRAIN_ROWS)):
        train_alpha_model(synthetic_frame(rows=20))


def test_refuses_an_empty_frame():
    with pytest.raises(TrainingDataError, match="empty"):
        train_alpha_model(pd.DataFrame())


def test_refuses_a_frame_without_a_label():
    frame = synthetic_frame().drop(columns=["llm_alpha_score", "alpha_score"], errors="ignore")

    with pytest.raises(TrainingDataError, match="no 'llm_alpha_score' column"):
        train_alpha_model(frame)


def test_refuses_an_impossible_test_fraction():
    with pytest.raises(TrainingDataError, match="test_fraction"):
        train_alpha_model(synthetic_frame(), test_fraction=1.5)


def test_trains_and_reports_a_comparison_against_the_prior():
    bundle = train_alpha_model(synthetic_frame())
    metrics = bundle["manifest"]["metrics"]

    assert metrics["beats_prior"] is True
    assert metrics["model_mae"] < metrics["prior_mae"]
    assert metrics["improvement_pct"] > 0
    assert metrics["rows_train"] + metrics["rows_valid"] == 400
    assert metrics["features"] >= 3


def test_the_split_is_chronological():
    bundle = train_alpha_model(synthetic_frame())
    metrics = bundle["manifest"]["metrics"]
    window = bundle["manifest"]["window"]

    assert metrics["rows_train"] == 300  # 400 rows, last 25% held out
    assert metrics["rows_valid"] == 100
    assert window["first_observed"] < window["last_observed"]


def test_the_manifest_describes_the_artefact():
    bundle = train_alpha_model(synthetic_frame())
    manifest = bundle["manifest"]

    assert manifest["feature_schema_version"] >= 1
    assert manifest["label"] == "llm_alpha_score"
    assert "llm_alpha_score" not in manifest["feature_names"]
    assert "token_address" not in manifest["feature_names"]
    assert "observed_at" not in manifest["feature_names"]
    assert "dex_score" in manifest["feature_names"]
    assert set(manifest["feature_range"]) <= set(manifest["feature_names"])
    assert manifest["prior_baseline"]["rows"] == 400


def test_an_artifact_survives_a_roundtrip(tmp_path):
    bundle = train_alpha_model(synthetic_frame())
    path = save_artifact(bundle, tmp_path / "nest" / "alpha.pkl")

    loaded = load_artifact(path)

    assert loaded is not None
    assert loaded["feature_names"] == bundle["feature_names"]
    inputs = pd.DataFrame([[60.0] + [0.0] * (len(loaded["feature_names"]) - 1)], columns=loaded["feature_names"])
    assert math.isfinite(float(loaded["model"].predict(inputs)[0]))


def test_a_missing_artifact_is_not_an_error(tmp_path):
    assert load_artifact(tmp_path / "absent.pkl") is None


def test_a_corrupt_artifact_is_not_fatal(tmp_path):
    path = tmp_path / "broken.pkl"
    path.write_bytes(b"not a pickle at all")

    assert load_artifact(path) is None


def test_an_unexpected_payload_is_rejected(tmp_path):
    path = tmp_path / "wrong.pkl"
    import pickle

    path.write_bytes(pickle.dumps({"something": "else"}))

    assert load_artifact(path) is None
