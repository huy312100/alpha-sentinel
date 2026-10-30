"""Shadow mode: did the learned scorer agree with the LLM, and can it take over?

In shadow mode both agents answer every token and the pair is stored in
``training_rows``. This module turns those rows into the one number that decides
whether ``ALPHA_PREDICTOR`` may move from ``llm`` to ``model``.

Agreement at the decision boundaries, not correlation on a scatter plot. A model
that tracks the LLM closely but disagrees on the handful of tokens sitting on the
65 line flips BUY into watch; one that disagrees on the 50 line flips watch into
reject. Both change what an operator sees, so both gate the handover.

One trap worth naming: in shadow mode the LLM answers *every* audit by design, so
the cost saving is zero while measuring. What the report gives you is the number
the saving would have been, in ``escalation.rate``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import pandas as pd

from core.models import utcnow
from ml.dataset import TRAINING_DATASET, load_training_rows, store_from_settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from storage.parquet_store import ParquetStore

#: Below this many paired rows an agreement figure is noise, not evidence.
MIN_PAIRED_ROWS = 200

#: Agree at least this often at *every* boundary before handing over. Below it,
#: too many calls become a different verdict for the report to be trusted.
MIN_AGREEMENT = 0.95

DEFAULT_BOUNDARIES: tuple[float, ...] = (65.0, 50.0)

#: ``escalation_reason`` carries the *shadow* marker on rows that were measured
#: rather than escalated. Those are not reasons the model needed help.
SHADOW_MARKER = "shadow"


def _validated_boundaries(boundaries: Sequence[float]) -> tuple[float, ...]:
    """Whole-number decision boundaries in ``0..100``.

    A bad boundary does not fail loudly on its own: ``predicted >= None`` is a
    comparison against NaN, so every row "agrees" and the report claims 100%
    agreement on a line that does not exist. A fractional edge would also leave
    the key ``agreement_64`` behind a boundary of 64.5.
    """
    checked: list[float] = []
    for edge in boundaries:
        if isinstance(edge, bool) or not isinstance(edge, (int, float)):
            raise ValueError(f"decision boundary must be a number, got {edge!r}")
        value = float(edge)
        if value != int(value) or not 0.0 <= value <= 100.0:
            raise ValueError(f"decision boundary must be a whole number in 0..100, got {value}")
        checked.append(value)
    if not checked:
        raise ValueError("at least one decision boundary is required")
    return tuple(checked)


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series([float("nan")] * len(frame), index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce")


def _share_agree(predicted: pd.Series, truth: pd.Series, edge: float) -> float | None:
    """Share of rows where both sides land on the same side of ``edge``."""
    mask = predicted.notna() & truth.notna()
    if not mask.any():
        return None
    return round(float(((predicted[mask] >= edge) == (truth[mask] >= edge)).mean()), 4)


def _corr(left: pd.Series, right: pd.Series) -> float | None:
    """Pearson on ranks is Spearman; no scipy, which the service does not install."""
    if len(left) < 3 or left.nunique() < 2 or right.nunique() < 2:
        return None
    return round(float(left.rank().corr(right.rank())), 3)


def _source_mix(frame: pd.DataFrame) -> dict[str, int]:
    if "alpha_source" not in frame.columns:
        return {}
    counts = frame["alpha_source"].fillna("unknown").astype(str).value_counts()
    return {str(key): int(value) for key, value in counts.items()}


def _escalation_mix(frame: pd.DataFrame) -> dict[str, Any]:
    """How often the LLM would still be needed, and why.

    ``escalation_reason`` looks like ``near_boundary 65 (prediction 63.0)``; the
    leading token is the family, which is what an operator acts on.
    """
    empty = {"escalated": 0, "rate": 0.0, "measured": 0, "reasons": {}}
    if "escalation_reason" not in frame.columns or frame.empty:
        return empty
    reasons = frame["escalation_reason"].fillna("").astype(str).str.strip()
    measured = int((reasons == SHADOW_MARKER).sum())
    real = reasons[(reasons != "") & (reasons != SHADOW_MARKER)]
    families = real.str.split().str[0].value_counts().head(8)
    return {
        "escalated": int(len(real)),
        "rate": round(len(real) / len(frame), 4),
        "measured": measured,
        "reasons": {str(key): int(value) for key, value in families.items()},
    }


def shadow_report(frame: pd.DataFrame, *, boundaries: Sequence[float] = DEFAULT_BOUNDARIES) -> dict[str, Any]:
    """Model-vs-LLM agreement over the paired rows in ``training_rows``."""
    boundaries = _validated_boundaries(boundaries)
    if frame is None or frame.empty:
        report = {"rows": 0, "reason": "no training rows yet"}
        report["recommendation"] = _recommend(report)
        return report

    model = _numeric(frame, "model_alpha_score")
    llm = _numeric(frame, "llm_alpha_score")
    paired = model.notna() & llm.notna()
    report: dict[str, Any] = {
        "rows": int(len(frame)),
        "tokens": int(frame["token_address"].nunique()) if "token_address" in frame.columns else 0,
        "paired_rows": int(paired.sum()),
        "model_scored": int(model.notna().sum()),
        "llm_scored": int(llm.notna().sum()),
        "sources": _source_mix(frame),
        "escalation": _escalation_mix(frame),
    }
    if "observed_at" in frame.columns:
        observed = pd.to_datetime(frame["observed_at"], errors="coerce", utc=True).dropna()
        if not observed.empty:
            report["first_observed"] = observed.min().isoformat()
            report["last_observed"] = observed.max().isoformat()

    if report["paired_rows"] >= 2:
        delta = model[paired] - llm[paired]
        report["model_bias"] = round(float(delta.mean()), 3)
        report["model_mae"] = round(float(delta.abs().mean()), 3)
        report["rank_correlation"] = _corr(model[paired], llm[paired])
        for edge in boundaries:
            agree = _share_agree(model[paired], llm[paired], edge)
            report[f"agreement_{int(edge)}"] = agree
            report[f"flip_rate_{int(edge)}"] = None if agree is None else round(1.0 - agree, 4)

    report["recommendation"] = _recommend(report)
    return report


def _recommend(report: Mapping[str, Any]) -> str:
    """A sentence an operator can act on, with the numbers that produced it."""
    if not report.get("rows"):
        return "No training rows yet: run a real (non dry-run) cycle with ALPHA_SHADOW=true."
    paired = report.get("paired_rows", 0)
    if paired < MIN_PAIRED_ROWS:
        return (
            f"Only {paired} paired rows (want {MIN_PAIRED_ROWS}): agreement is not evidence yet. "
            "Keep ALPHA_SHADOW=true and collect more."
        )
    # Every boundary, not just the approval floor: a flip at 50 turns a watch
    # call into a rejection, which is still a verdict an operator has to trust.
    scored = {
        int(key.rsplit("_", 1)[-1]): value
        for key, value in report.items()
        if key.startswith("agreement_") and value is not None
    }
    if not scored:
        return "Paired rows exist but no comparable scores: check llm_alpha_score and model_alpha_score."
    edge = min(scored, key=lambda bound: (scored[bound], bound))
    agree = scored[edge]
    rate = report.get("escalation", {}).get("rate", 0.0)
    if agree < MIN_AGREEMENT:
        return (
            f"The model flips {1.0 - agree:.1%} of calls at {edge} ({paired} pairs). "
            "Too often to hand over: keep shadow on."
        )
    return (
        f"Agreement {agree:.1%} or better at every boundary over {paired} paired rows, "
        f"{rate:.1%} would still escalate. Candidate for ALPHA_PREDICTOR=model."
    )


def _fmt_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _fmt_signed(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.3f}"


def format_shadow(report: Mapping[str, Any]) -> str:
    if not report.get("rows"):
        return f"shadow: {report.get('reason', 'no data')}"
    lines = [
        f"shadow    rows={report['rows']}  tokens={report['tokens']}  paired={report['paired_rows']}",
        f"  sources   {report['sources'] or 'none recorded'}",
    ]
    if report.get("first_observed"):
        lines.append(f"  window    {report['first_observed']} -> {report['last_observed']}")
    if "model_mae" in report:
        lines.append(
            f"  model     MAE {report['model_mae']:.3f}  bias {report['model_bias']:+.3f}  "
            f"rank r {_fmt_signed(report.get('rank_correlation'))}"
        )
    edges = sorted(
        int(key.rsplit("_", 1)[-1]) for key in report if key.startswith("agreement_") and report[key] is not None
    )
    if edges:
        agreed = "  ".join(f"{edge}: {_fmt_ratio(report[f'agreement_{edge}'])}" for edge in edges)
        lines.append(f"  agreement {agreed}")
    escalation = report.get("escalation") or {}
    if escalation:
        reasons = ", ".join(f"{key} {count}" for key, count in escalation.get("reasons", {}).items()) or "none"
        lines.append(f"  escalation {_fmt_ratio(escalation.get('rate'))} of rows ({reasons})")
        if escalation.get("measured"):
            lines.append(f"  measured  {escalation['measured']} rows were shadow calls, not escalations")
    lines.append(f"  verdict   {report['recommendation']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI: python -m ml.shadow
# ---------------------------------------------------------------------------
def _main(argv: list[str] | None = None, *, cold: ParquetStore | None = None) -> int:
    from config.settings import get_settings

    parser = argparse.ArgumentParser(description="Model-vs-LLM agreement from training_rows.")
    parser.add_argument("--days", type=int, default=90, help="lookback window in days (0 = everything)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    lake = cold or store_from_settings(get_settings())
    since = None if args.days <= 0 else (utcnow() - timedelta(days=args.days)).isoformat()
    frame = load_training_rows(lake, since=since)
    report = shadow_report(frame)

    if args.json:
        print(json.dumps(report, indent=2))
    elif frame.empty:
        print(f"no rows in {lake.dataset_path(TRAINING_DATASET)} yet - run the bot first")
    else:
        print(format_shadow(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(_main())


__all__ = [
    "DEFAULT_BOUNDARIES",
    "MIN_AGREEMENT",
    "MIN_PAIRED_ROWS",
    "format_shadow",
    "shadow_report",
]
