"""Offline training: turn ``training_rows`` into a versioned artifact.

The target is the label the operator asks for (by default Agent 1's
``llm_alpha_score``). That makes this **distillation**: the model learns to
reproduce the teacher faster and cheaper, not to be right more often than it.
Choosing a forward-return column instead is supported by ``--label`` and is the
only honest way to claim predictive edge.

Three decisions worth knowing about:

1. **Time-based split.** The last ``test_fraction`` of rows, ordered by
   ``observed_at``, is the validation set. A random split lets the same token
   appear on both sides and reports a score that no live traffic can reproduce.
2. **The prior is the baseline.** Metrics are always reported against
   ``weighted_prior`` on the *same* validation rows. A model that cannot beat a
   fixed-weight average of the same eight numbers is not worth deploying.
3. **Nothing here runs in the hot path.** scikit-learn is imported lazily, so the
   service image does not need the ML extras to keep scanning.
"""

from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from core.logging import get_logger
from core.models import utcnow
from ml import FEATURE_SCHEMA_VERSION
from ml.dataset import NON_FEATURE_COLUMNS, baseline_report, feature_matrix, load_training_rows, store_from_settings

log = get_logger(__name__)

#: Below this, a "model" is memorising noise. Training refuses rather than
#: shipping an artifact with a confident-looking manifest built on 12 rows.
MIN_TRAIN_ROWS = 80

DEFAULT_TEST_FRACTION = 0.25
DEFAULT_ARTIFACT = Path("models") / "alpha_model.pkl"

#: Kept small and regularised on purpose: the label is a noisy LLM judgement of
#: a handful of aggregated numbers, so deep trees would fit the noise.
GBM_PARAMS: Mapping[str, Any] = {
    "max_depth": 3,
    "max_iter": 250,
    "learning_rate": 0.06,
    "min_samples_leaf": 20,
    "l2_regularization": 1.0,
    "early_stopping": False,
    "random_state": 0,
}


class TrainingDataError(RuntimeError):
    """The dataset cannot support a trustworthy artefact yet."""


def _require_sklearn() -> Any:
    try:
        from sklearn.ensemble import HistGradientBoostingRegressor
    except ImportError as exc:  # pragma: no cover - depends on the local env
        raise TrainingDataError(
            "scikit-learn is not installed. Training needs the optional extras: "
            "pip install -r requirements-ml.txt"
        ) from exc
    return HistGradientBoostingRegressor


def _split(frame: pd.DataFrame, test_fraction: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    if "observed_at" not in frame.columns:
        raise TrainingDataError("dataset has no observed_at column; cannot build a time-based split")
    ordered = frame.assign(observed_at=pd.to_datetime(frame["observed_at"], errors="coerce", utc=True))
    ordered = ordered.dropna(subset=["observed_at"]).sort_values("observed_at")
    cut = int(len(ordered) * (1.0 - test_fraction))
    cut = max(1, min(cut, len(ordered) - 1))
    return ordered.iloc[:cut], ordered.iloc[cut:]


def _metrics(
    model: Any,
    train: pd.DataFrame,
    valid: pd.DataFrame,
    features: Sequence[str],
    label: str,
) -> dict[str, Any]:
    predicted = pd.Series(model.predict(valid[list(features)].astype("float64")), index=valid.index)
    truth = pd.to_numeric(valid[label], errors="coerce")
    prior = pd.to_numeric(valid["prior_score"], errors="coerce")
    mask = truth.notna() & predicted.notna()
    if not mask.any():
        raise TrainingDataError(f"validation rows carry no usable {label!r}")

    model_error = (predicted[mask] - truth[mask]).abs()
    prior_error = (prior[mask] - truth[mask]).abs()
    model_mae = float(model_error.mean())
    prior_mae = float(prior_error.mean())
    report: dict[str, Any] = {
        "rows_train": int(len(train)),
        "rows_valid": int(mask.sum()),
        "model_mae": round(model_mae, 3),
        "prior_mae": round(prior_mae, 3),
        "model_rmse": round(float(((predicted[mask] - truth[mask]) ** 2).mean() ** 0.5), 3),
        "beats_prior": bool(model_mae < prior_mae),
        "features": len(features),
    }
    if prior_mae > 0:
        report["improvement_pct"] = round(100.0 * (prior_mae - model_mae) / prior_mae, 2)
    correlation = predicted[mask].corr(truth[mask])
    report["pearson_r"] = round(float(correlation), 3) if pd.notna(correlation) else None
    for edge in (65.0, 50.0):
        model_side = predicted[mask] >= edge
        truth_side = truth[mask] >= edge
        report[f"agreement_at_{int(edge)}"] = round(float((model_side == truth_side).mean()), 4)
    return report


def train_alpha_model(
    frame: pd.DataFrame,
    *,
    label: str = "llm_alpha_score",
    test_fraction: float = DEFAULT_TEST_FRACTION,
    min_rows: int = MIN_TRAIN_ROWS,
) -> dict[str, Any]:
    """Fit, evaluate against the prior, and return a self-describing bundle."""
    if frame.empty:
        raise TrainingDataError("dataset is empty - run the bot to collect training_rows first")
    column = label if label in frame.columns else "alpha_score"
    if column not in frame.columns:
        raise TrainingDataError(f"dataset has no {label!r} column")
    if not 0.0 < test_fraction < 1.0:
        raise TrainingDataError(f"test_fraction must be in (0, 1), got {test_fraction}")

    usable = frame[pd.to_numeric(frame[column], errors="coerce").notna()].copy()
    if len(usable) < min_rows:
        raise TrainingDataError(
            f"{len(usable)} labelled rows is below the {min_rows}-row floor; a model trained on this "
            "would report confident metrics that mean nothing"
        )

    train, valid = _split(usable, test_fraction)
    _, features = feature_matrix(train)
    features = [name for name in features if name not in NON_FEATURE_COLUMNS]
    if not features:
        raise TrainingDataError("no numeric feature columns found")

    estimator_class = _require_sklearn()
    model = estimator_class(**dict(GBM_PARAMS))
    model.fit(train[features].astype("float64"), pd.to_numeric(train[column], errors="coerce"))

    metrics = _metrics(model, train, valid, features, column)
    manifest = {
        "trained_at": utcnow().isoformat(timespec="seconds"),
        "label": column,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": features,
        "params": dict(GBM_PARAMS),
        "metrics": metrics,
        "window": {
            "first_observed": str(train["observed_at"].min()),
            "last_observed": str(valid["observed_at"].max()),
        },
        "prior_baseline": baseline_report(usable, label=column),
        "feature_range": {
            name: [float(train[name].min()), float(train[name].max())]
            for name in features
            if train[name].notna().any()
        },
    }
    log.info("alpha_model_trained", extra={"manifest": {k: manifest[k] for k in ("label", "metrics")}})
    return {"model": model, "feature_names": features, "manifest": manifest}


def save_artifact(bundle: Mapping[str, Any], path: str | Path) -> Path:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # pickle rather than joblib: stdlib, and a scikit-learn estimator pickles
    # cleanly. The artifact is a build output, never loaded from untrusted input.
    with destination.open("wb") as handle:
        pickle.dump(dict(bundle), handle, protocol=pickle.HIGHEST_PROTOCOL)
    log.info("alpha_model_saved", extra={"path": str(destination), "bytes": destination.stat().st_size})
    return destination


def load_artifact(path: str | Path) -> dict[str, Any] | None:
    """Load a bundle, or ``None`` if it is missing/unreadable/unexpected.

    ``None`` is a supported answer: the caller falls back to the LLM. A boot
    failure over a stale pickle would take the scanner offline for no reason.
    """
    source = Path(path).expanduser()
    if not source.is_file():
        log.warning("alpha_model_missing", extra={"path": str(source)})
        return None
    try:
        with source.open("rb") as handle:
            bundle = pickle.load(handle)  # noqa: S301 - build artefact, trusted path
    except Exception as exc:  # noqa: BLE001 - corrupt artifact is not fatal
        log.error("alpha_model_unreadable", extra={"path": str(source), "error": str(exc)[:200]})
        return None
    if not isinstance(bundle, dict) or not bundle.get("feature_names") or "model" not in bundle:
        log.error("alpha_model_invalid", extra={"path": str(source)})
        return None
    return bundle


def _print_metrics(manifest: Mapping[str, Any]) -> None:
    metrics = manifest["metrics"]
    print(f"label            {manifest['label']}")
    print(f"rows             train {metrics['rows_train']}  valid {metrics['rows_valid']}")
    print(f"MAE              model {metrics['model_mae']:.3f}  prior {metrics['prior_mae']:.3f}")
    if "improvement_pct" in metrics:
        verdict = "beats" if metrics["beats_prior"] else "does NOT beat"
        print(f"vs prior         {verdict} it by {abs(metrics['improvement_pct']):.2f}%")
    print(f"r (pearson)      {metrics['pearson_r']}")
    print(f"agreement        65: {metrics.get('agreement_at_65', float('nan')):.1%}  50: {metrics.get('agreement_at_50', float('nan')):.1%}")
    print(f"window           {manifest['window']['first_observed']} -> {manifest['window']['last_observed']}")


def _main(argv: list[str] | None = None) -> int:
    from config.settings import get_settings

    parser = argparse.ArgumentParser(description="Train the alpha-score model from training_rows.")
    parser.add_argument("--data", help="exported file (.parquet/.csv); default: read the lake")
    parser.add_argument("--out", default=str(DEFAULT_ARTIFACT), help=f"artifact path (default {DEFAULT_ARTIFACT})")
    parser.add_argument("--label", default="llm_alpha_score")
    parser.add_argument("--test-fraction", type=float, default=DEFAULT_TEST_FRACTION)
    parser.add_argument("--min-rows", type=int, default=MIN_TRAIN_ROWS)
    parser.add_argument("--dry-run", action="store_true", help="train and print metrics, write nothing")
    args = parser.parse_args(argv)

    if args.data:
        source = Path(args.data).expanduser()
        frame = pd.read_parquet(source) if source.suffix.lower() != ".csv" else pd.read_csv(source)
    else:
        frame = load_training_rows(store_from_settings(get_settings()))

    try:
        bundle = train_alpha_model(
            frame, label=args.label, test_fraction=args.test_fraction, min_rows=args.min_rows
        )
    except TrainingDataError as exc:
        print(f"cannot train: {exc}")
        return 1

    _print_metrics(bundle["manifest"])
    if args.dry_run:
        print("dry run: artifact not written")
        return 0
    destination = save_artifact(bundle, args.out)
    print(f"wrote {destination}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(_main())


__all__ = [
    "DEFAULT_ARTIFACT",
    "GBM_PARAMS",
    "MIN_TRAIN_ROWS",
    "TrainingDataError",
    "load_artifact",
    "save_artifact",
    "train_alpha_model",
]
