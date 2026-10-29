"""``training_rows``: the dataset that makes a model possible.

Every audit that completes writes one row here: the flattened feature vector, the
labels the two agents produced, and enough provenance to reproduce the run months
later. Without this the lake only holds 8 compressed layer scores, which caps any
model at what ``weighted_prior`` already computes.

Design notes
------------
* **One row per (token, cycle).** Cycles are minutes apart, so a token appears
  many times; that is intended. For time-series validation, split on ``observed_at``.
* **``llm_alpha_score`` is only filled when an LLM actually scored the token.**
  In shadow mode both ``llm_alpha_score`` and ``model_alpha_score`` are present,
  which is exactly the pair you need to measure disagreement before trusting the
  model in production.
* Writing here is best-effort, like every other cold-path write: a lake problem
  must never abort a cycle.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

import pandas as pd

from core.logging import get_logger
from ml import FEATURE_SCHEMA_VERSION
from ml.features import flatten
from storage.parquet_store import ParquetStore, StorageError

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from ai_engine.dual_audit_flow import AuditOutcome
    from config.settings import Settings
    from scanners.pipeline import CollectedSnapshot

log = get_logger(__name__)

#: Parquet dataset name (lowercase, underscore - the store rejects anything else).
TRAINING_DATASET = "training_rows"

#: Columns that identify a row rather than describe the market.
META_COLUMNS: tuple[str, ...] = (
    "token_address",
    "chain",
    "asset_class",
    "symbol",
    "observed_at",
    "feature_schema_version",
)

#: Outcomes of the audit. Never feed these to a model as inputs: they are what
#: the model is supposed to predict, and including them is leakage that produces
#: a beautiful offline score and a useless bot.
LABEL_COLUMNS: tuple[str, ...] = (
    "alpha_score",
    "alpha_confidence",
    "alpha_source",
    "llm_alpha_score",
    "llm_alpha_confidence",
    "model_alpha_score",
    "escalation_reason",
    "risk_score",
    "risk_veto",
    "final_score",
    "final_status",
    "degraded",
)

#: Model/prompt provenance, also excluded from the feature matrix.
PROVENANCE_COLUMNS: tuple[str, ...] = (
    "alpha_model",
    "risk_model",
    "prompt_version",
    "scan_duration_ms",
)

NON_FEATURE_COLUMNS: frozenset[str] = frozenset(META_COLUMNS + LABEL_COLUMNS + PROVENANCE_COLUMNS)


def store_from_settings(settings: "Settings") -> ParquetStore:
    """Build the lake handle the same way :class:`bot.engine.ScanEngine` does."""
    storage = settings.storage
    return ParquetStore(
        storage.parquet_root,
        r2=storage.r2,
        compression=storage.compression,
        row_group_size=storage.row_group_size,
        local_retention_days=storage.local_retention_days,
        remote_retention_days=storage.remote_retention_days,
    )


def training_row(collected: "CollectedSnapshot", outcome: "AuditOutcome") -> dict[str, Any]:
    """Features + labels + provenance for one completed audit."""
    snapshot = collected.snapshot
    prior = float(outcome.prior_score)

    row: dict[str, Any] = {
        "token_address": snapshot.token_address,
        "chain": snapshot.chain,
        "asset_class": snapshot.asset_class,
        "symbol": snapshot.symbol or "UNKNOWN",
        "observed_at": snapshot.collected_at,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
    }
    row.update(flatten(snapshot))
    # The deterministic prior is both a feature and the baseline a model has to
    # beat; storing it means every notebook starts from a real comparison.
    row["prior_score"] = prior

    alpha_source = getattr(outcome, "alpha_source", "llm")
    model_alpha = getattr(outcome, "model_alpha_score", None)
    row["alpha_score"] = float(outcome.alpha.alpha_score)
    row["alpha_confidence"] = float(outcome.alpha.confidence)
    row["alpha_source"] = alpha_source
    row["llm_alpha_score"] = float(outcome.alpha.alpha_score) if alpha_source == "llm" else float("nan")
    row["llm_alpha_confidence"] = float(outcome.alpha.confidence) if alpha_source == "llm" else float("nan")
    row["model_alpha_score"] = float(model_alpha) if model_alpha is not None else float("nan")
    row["escalation_reason"] = getattr(outcome, "escalation_reason", None) or ""

    row["risk_score"] = float(outcome.risk.risk_score)
    row["risk_veto"] = 1.0 if outcome.risk.effective_veto() else 0.0
    row["final_score"] = float(outcome.verdict.final_score)
    row["final_status"] = outcome.verdict.status.value
    row["degraded"] = 1.0 if outcome.degraded else 0.0

    row["alpha_model"] = outcome.alpha.model
    row["risk_model"] = outcome.risk.model
    row["prompt_version"] = outcome.prompt_version
    row["scan_duration_ms"] = round(float(collected.duration_ms), 1)
    return row


def write_training_rows(cold: ParquetStore, rows: Iterable[Mapping[str, Any]]) -> int:
    """Append one Parquet file per cycle. Returns the row count written."""
    materialised = list(rows)
    if not materialised:
        return 0
    frame = pd.DataFrame(materialised)
    try:
        cold.write_frame(TRAINING_DATASET, frame, timestamp_column="observed_at")
    except (StorageError, OSError, ValueError) as exc:
        log.error("training_rows_write_failed", extra={"rows": len(frame), "error": str(exc)[:300]})
        return 0
    return len(frame)


def load_training_rows(cold: ParquetStore, *, since: str | None = None) -> pd.DataFrame:
    """Read the whole dataset (or from ``since``) into one frame."""
    frame = cold.read_frame(TRAINING_DATASET, start=since)
    if frame.empty:
        return frame
    return frame.sort_values("observed_at").reset_index(drop=True)


def feature_matrix(frame: pd.DataFrame, feature_names: Sequence[str] | None = None) -> tuple[pd.DataFrame, list[str]]:
    """Numeric feature columns only, labels and metadata dropped."""
    if frame.empty:
        return pd.DataFrame(), []
    if feature_names is not None:
        columns = [name for name in feature_names if name in frame.columns]
        return frame[columns].astype("float64"), columns
    columns = [
        name
        for name in frame.columns
        if name not in NON_FEATURE_COLUMNS and pd.api.types.is_numeric_dtype(frame[name])
    ]
    return frame[columns].astype("float64"), columns


def _correlations(prior: pd.Series, truth: pd.Series) -> dict[str, float | None]:
    """Correlation, or ``None`` when the sample cannot support one.

    Two rows always correlate perfectly and a constant column divides by zero;
    reporting ``None`` keeps a three-row dataset from printing a confident 1.0.
    """
    if len(prior) < 3 or prior.nunique() < 2 or truth.nunique() < 2:
        return {"pearson_r": None, "spearman_r": None}
    return {
        "pearson_r": round(float(prior.corr(truth, method="pearson")), 3),
        "spearman_r": round(float(prior.corr(truth, method="spearman")), 3),
    }


def baseline_report(
    frame: pd.DataFrame,
    *,
    label: str = "llm_alpha_score",
    boundaries: Sequence[float] = (65.0, 50.0),
) -> dict[str, Any]:
    """How well the deterministic prior predicts the label it is blended with.

    This is the number a model must beat. If the prior already agrees closely,
    a gradient-boosted imitation of the LLM adds maintenance, not edge.
    """
    if frame.empty:
        return {"rows": 0, "reason": "no rows"}
    column = label if label in frame.columns else "alpha_score"
    if column not in frame.columns or "prior_score" not in frame.columns:
        return {"rows": 0, "reason": f"missing columns for {label!r}"}

    pairs = frame[[column, "prior_score"]].apply(pd.to_numeric, errors="coerce").dropna()
    if pairs.empty:
        return {"rows": 0, "reason": f"no rows carry a usable {column!r}"}

    truth = pairs[column]
    prior = pairs["prior_score"]
    error = (prior - truth).abs()
    report: dict[str, Any] = {
        "rows": int(len(pairs)),
        "label": column,
        "tokens": int(frame["token_address"].nunique()) if "token_address" in frame else 0,
        "prior_mae": round(float(error.mean()), 3),
        "prior_rmse": round(float(((prior - truth) ** 2).mean() ** 0.5), 3),
        "mean_bias": round(float((prior - truth).mean()), 3),
    }
    report.update(_correlations(prior, truth))
    for edge in boundaries:
        flips = ((prior >= edge) != (truth >= edge)).mean()
        report[f"band_flips_{int(edge)}"] = round(float(flips), 4)
    if "observed_at" in frame:
        observed = pd.to_datetime(frame["observed_at"], errors="coerce", utc=True).dropna()
        if not observed.empty:
            report["first_observed"] = observed.min().isoformat()
            report["last_observed"] = observed.max().isoformat()
    return report


def _fmt_r(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.3f}"


def format_baseline(report: Mapping[str, Any]) -> str:
    if not report.get("rows"):
        return f"baseline: {report.get('reason', 'not enough data')}"
    lines = [
        f"baseline  label={report['label']}  rows={report['rows']}  tokens={report['tokens']}",
        f"  prior MAE   {report['prior_mae']:.3f}   RMSE {report['prior_rmse']:.3f}",
        f"  correlation pearson {_fmt_r(report.get('pearson_r'))}   spearman {_fmt_r(report.get('spearman_r'))}",
        f"  bias        {report['mean_bias']:+.3f} (prior - label)",
    ]
    flips = [f"{key.rsplit('_', 1)[-1]}: {value:.1%}" for key, value in report.items() if key.startswith("band_flips_")]
    if flips:
        lines.append("  decision flips at " + ", ".join(flips))
    if report.get("first_observed"):
        lines.append(f"  window      {report['first_observed']} -> {report['last_observed']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI: python -m ml.dataset export|stats
# ---------------------------------------------------------------------------
def _main(argv: list[str] | None = None, *, cold: ParquetStore | None = None) -> int:
    from config.settings import get_settings

    parser = argparse.ArgumentParser(description="Export the training_rows dataset.")
    parser.add_argument("command", choices=("export", "stats"), nargs="?", default="export")
    parser.add_argument("--out", help="destination file (.parquet or .csv)")
    parser.add_argument("--since", help="ISO date/datetime lower bound")
    parser.add_argument("--label", default="llm_alpha_score", help="label column for --baseline")
    parser.add_argument("--baseline", action="store_true", help="print prior-vs-label metrics")
    args = parser.parse_args(argv)

    lake = cold or store_from_settings(get_settings())
    frame = load_training_rows(lake, since=args.since)
    if frame.empty:
        print(f"no rows in {lake.dataset_path(TRAINING_DATASET)} yet - run the bot first")
        return 1

    print(f"rows {len(frame)}  columns {len(frame.columns)}  dataset {TRAINING_DATASET}")
    if args.baseline:
        print(format_baseline(baseline_report(frame, label=args.label)))
    if args.command == "stats":
        print(json.dumps({"columns": list(frame.columns)}, indent=2))
        return 0

    if not args.out:
        parser.error("--out is required for export")
    destination = Path(args.out).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() == ".csv":
        frame.to_csv(destination, index=False)
    else:
        frame.to_parquet(destination, index=False)
    print(f"wrote {destination} ({destination.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(_main())


__all__ = [
    "LABEL_COLUMNS",
    "META_COLUMNS",
    "NON_FEATURE_COLUMNS",
    "PROVENANCE_COLUMNS",
    "TRAINING_DATASET",
    "baseline_report",
    "feature_matrix",
    "format_baseline",
    "load_training_rows",
    "store_from_settings",
    "training_row",
    "write_training_rows",
]
