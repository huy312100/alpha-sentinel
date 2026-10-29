"""Shadow agreement: the number that decides whether the model may take over.

The policy is deliberately conservative: the model is only a candidate once it has
enough pairs and agrees at *every* decision boundary, because a disagreement there
is a changed verdict, not a rounding difference.
"""

from __future__ import annotations

import json

import pandas as pd

from ml.dataset import TRAINING_DATASET
from ml.shadow import (
    DEFAULT_BOUNDARIES,
    MIN_PAIRED_ROWS,
    _main,
    format_shadow,
    shadow_report,
)


def frame(rows: int, *, model, llm, source: str = "llm", reason: str = "shadow") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "observed_at": pd.date_range("2026-10-01", periods=rows, freq="h", tz="UTC"),
            "token_address": [f"0x{index:040x}" for index in range(rows)],
            "alpha_source": [source] * rows,
            "escalation_reason": [reason] * rows,
            "llm_alpha_score": llm,
            "model_alpha_score": model,
        }
    )


def test_an_empty_frame_says_so_and_asks_for_data():
    report = shadow_report(pd.DataFrame())

    assert report["rows"] == 0
    assert "ALPHA_SHADOW=true" in report["recommendation"]


def test_perfect_agreement_is_a_candidate_once_there_are_enough_pairs():
    scores = [80.0] * MIN_PAIRED_ROWS
    report = shadow_report(frame(MIN_PAIRED_ROWS, model=scores, llm=scores))

    assert report["paired_rows"] == MIN_PAIRED_ROWS
    assert report["agreement_65"] == 1.0
    assert report["flip_rate_65"] == 0.0
    assert report["model_mae"] == 0.0
    assert "ALPHA_PREDICTOR=model" in report["recommendation"]


def test_a_small_sample_is_refused_even_when_it_agrees():
    """Ten agreeing rows prove nothing, and the report has to say that."""
    scores = [80.0] * 10
    report = shadow_report(frame(10, model=scores, llm=scores))

    assert report["agreement_65"] == 1.0
    assert "not evidence yet" in report["recommendation"]
    assert "ALPHA_PREDICTOR=model" not in report["recommendation"]


def test_flips_at_the_approval_floor_block_the_handover():
    rows = MIN_PAIRED_ROWS
    model = [70.0] * rows  # approves
    llm = [60.0] * rows  # watches: this is a changed verdict, not a rounding error
    report = shadow_report(frame(rows, model=model, llm=llm))

    assert report["agreement_65"] == 0.0
    assert report["flip_rate_65"] == 1.0
    assert report["model_bias"] == 10.0
    assert "keep shadow on" in report["recommendation"]


def test_agreement_is_scored_per_boundary():
    rows = MIN_PAIRED_ROWS
    # Both sit below the approval floor (65) yet straddle the watch line (50): no
    # BUY is created or lost, but every row changes verdict at 50.
    report = shadow_report(frame(rows, model=[55.0] * rows, llm=[45.0] * rows))

    assert report["agreement_65"] == 1.0
    assert report["agreement_50"] == 0.0
    assert "at 50" in report["recommendation"]
    assert "ALPHA_PREDICTOR=model" not in report["recommendation"]


def test_rows_that_were_never_paired_are_excluded():
    mixed = frame(6, model=[80.0, None, float("nan"), 80.0, 80.0, 80.0], llm=[80.0] * 6)

    report = shadow_report(mixed)

    assert report["rows"] == 6
    assert report["paired_rows"] == 4
    assert report["model_scored"] == 4
    assert report["llm_scored"] == 6


def test_shadow_calls_are_not_counted_as_escalations():
    """In shadow mode the LLM answers everything by design; that is not a cost."""
    rows = MIN_PAIRED_ROWS
    report = shadow_report(frame(rows, model=[80.0] * rows, llm=[80.0] * rows, reason="shadow"))

    assert report["escalation"]["escalated"] == 0
    assert report["escalation"]["measured"] == rows
    assert report["escalation"]["rate"] == 0.0


def test_escalation_reasons_are_grouped_by_family():
    rows = 6
    reasons = [
        "near_boundary 65 (prediction 63.0)",
        "near_boundary 50 (prediction 48.0)",
        "out_of_range wash_trade_count",
        "low_confidence 0.12 < 0.60",
        "",
        "",
    ]
    report = shadow_report(frame(rows, model=[80.0] * rows, llm=[80.0] * rows, reason="shadow").assign(
        escalation_reason=reasons
    ))

    assert report["escalation"]["escalated"] == 4
    assert report["escalation"]["rate"] == round(4 / rows, 4)
    assert report["escalation"]["reasons"] == {"near_boundary": 2, "out_of_range": 1, "low_confidence": 1}


def test_the_source_mix_is_reported():
    rows = 4
    report = shadow_report(
        frame(rows, model=[80.0] * rows, llm=[80.0] * rows, source="model").assign(
            alpha_source=["model", "model", "llm", "prior"]
        )
    )

    assert report["sources"] == {"model": 2, "llm": 1, "prior": 1}


def test_a_frame_without_the_model_columns_does_not_crash():
    report = shadow_report(pd.DataFrame({"token_address": ["0x1"], "alpha_score": [70.0]}))

    assert report["paired_rows"] == 0
    assert report["recommendation"]


def test_the_text_report_mentions_the_boundaries():
    rows = MIN_PAIRED_ROWS
    text = format_shadow(shadow_report(frame(rows, model=[80.0] * rows, llm=[80.0] * rows)))

    for edge in DEFAULT_BOUNDARIES:
        assert str(int(edge)) in text
    assert "verdict" in text


def test_the_text_report_handles_no_data():
    assert format_shadow(shadow_report(pd.DataFrame())).startswith("shadow:")


def test_the_cli_reports_from_a_scratch_store(tmp_path, capsys):
    cold = store(tmp_path)

    assert _main(["--days", "30", "--json"], cold=cold) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == 0
    assert payload["reason"] == "no training rows yet"


def test_the_cli_prints_a_text_report(tmp_path, capsys):
    rows = 3
    cold = store(tmp_path, frame(rows, model=[80.0] * rows, llm=[80.0] * rows))

    assert _main([], cold=cold) == 0

    text = capsys.readouterr().out
    assert "shadow" in text
    assert str(rows) in text


def store(tmp_path, rows: pd.DataFrame | None = None):
    """A Parquet lake, optionally seeded with rows."""
    from storage.parquet_store import ParquetStore

    lake = ParquetStore(tmp_path / "lake", compression="snappy")
    if rows is not None:
        lake.write_frame(TRAINING_DATASET, rows, timestamp_column="observed_at")
    return lake
