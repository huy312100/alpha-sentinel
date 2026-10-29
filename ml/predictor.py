"""The model as an alpha source, plus the policy that decides when to ask the LLM.

Shape of the thing
------------------
The LLM is not replaced, it is **downgraded to an adjudicator**. For every token
the model answers first; the LLM is called only when the prediction is not safe
to act on:

* the input is thin (few core features present),
* a core market input left the range the model was trained on, or enough
  features did that it is a regime shift rather than one counter wandering,
* the score sits within ``margin`` of a decision boundary, so the call could flip.

That last rule is the important one. "Confidence" near a threshold is not about
how sure the model is, it is about how little it takes to change the verdict - so
the boundary, not the score, drives escalation.

Honesty about ``confidence``
----------------------------
It is an *input trust* score: coverage of core features × (1 - drift ratio). It is
**not** predictive uncertainty; a gradient-boosted tree has no cheap posterior.
Treat it as "can I believe the inputs", never as "how likely is this to be right".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Sequence

import pandas as pd

from config.settings import ALPHA_PREDICTOR_MODES
from core.logging import get_logger
from core.models import TokenSnapshot, clamp
from core.scoring import weighted_prior
from ml.dataset import LABEL_COLUMNS, META_COLUMNS, PROVENANCE_COLUMNS
from ml.features import CORE_FEATURES, align, coverage, flatten
from ml.train import load_artifact

if TYPE_CHECKING:  # pragma: no cover - typing only
    from config.settings import Settings, ThresholdSettings

log = get_logger(__name__)

MODE_LLM = "llm"
MODE_MODEL = "model"
MODE_PRIOR = "prior"
#: Single source of truth for the allowed values (validated in config.settings).
VALID_MODES = ALPHA_PREDICTOR_MODES

#: Features must never be read from outcome columns; a predictor that saw
#: ``llm_alpha_score`` would be predicting its own label.
FORBIDDEN_FEATURES = frozenset(LABEL_COLUMNS + META_COLUMNS + PROVENANCE_COLUMNS)

#: Share of features that must leave their training range before drift counts.
#: A gradient-boosted model sees ~100 features, many of them integer counters
#: (flag counts, trade counts) that wander by one between any two tokens. If a
#: single counter could escalate, most audits would fall back to the LLM and the
#: cost saving would evaporate. Regime shift moves core market inputs together.
DRIFT_SHARE = 0.05
DRIFT_MIN_FEATURES = 3


@dataclass(slots=True)
class Prediction:
    """One model answer for one token."""

    alpha_score: float
    confidence: float
    model_id: str
    coverage: float = 1.0
    #: Every feature outside its training range, for observability.
    out_of_range: list[str] = field(default_factory=list)
    #: Whether that drift is broad enough to distrust the prediction.
    drift_material: bool = False


class AlphaPredictor(Protocol):
    """Anything that can propose an alpha score without an API call."""

    model_id: str

    def predict(self, snapshot: TokenSnapshot) -> Prediction | None: ...


class PriorPredictor:
    """The deterministic baseline, exposed as a predictor.

    Useful two ways: ``ALPHA_PREDICTOR=prior`` runs the whole pipeline with zero
    LLM calls for the alpha side, and it is the reference the model must beat.
    """

    model_id = "prior"

    def predict(self, snapshot: TokenSnapshot) -> Prediction:
        score = weighted_prior(snapshot.signals)
        return Prediction(alpha_score=round(score, 2), confidence=1.0, model_id=self.model_id, coverage=1.0)


class ModelPredictor:
    """Wraps a trained artefact; ``from_path`` returns ``None`` instead of raising."""

    def __init__(self, bundle: dict[str, Any]) -> None:
        self._model = bundle["model"]
        self.feature_names: list[str] = list(bundle["feature_names"])
        self.manifest: dict[str, Any] = dict(bundle.get("manifest") or {})
        # A label column in the feature list means the artefact was trained on
        # its own answer: perfect offline metrics, worthless in production.
        leaked = FORBIDDEN_FEATURES.intersection(self.feature_names)
        if leaked:
            raise ValueError(f"artefact uses outcome columns as features: {sorted(leaked)}")
        self._range: dict[str, tuple[float, float]] = {
            name: (float(low), float(high))
            for name, (low, high) in (self.manifest.get("feature_range") or {}).items()
        }
        trained_at = str(self.manifest.get("trained_at", "unknown"))
        self.model_id = f"gbm:{trained_at}"

    @classmethod
    def from_path(cls, path: str | Path) -> "ModelPredictor | None":
        bundle = load_artifact(path)
        if bundle is None:
            return None
        try:
            return cls(bundle)
        except (KeyError, TypeError, ValueError) as exc:
            log.error("alpha_model_incompatible", extra={"path": str(path), "error": str(exc)[:200]})
            return None

    def prediction_row(self, snapshot: TokenSnapshot) -> dict[str, float]:
        row = flatten(snapshot)
        row["prior_score"] = weighted_prior(snapshot.signals)
        return row

    def _out_of_range(self, row: dict[str, float]) -> list[str]:
        drifted: list[str] = []
        for name, (low, high) in self._range.items():
            value = row.get(name)
            if value is None or not math.isfinite(value):
                continue
            if value < low or value > high:
                drifted.append(name)
        return drifted

    def _drift_is_material(self, drifted: Sequence[str]) -> bool:
        """One counter moving is noise; a shifted market input or a broad move is not."""
        if not drifted:
            return False
        if any(name in CORE_FEATURES for name in drifted):
            return True
        floor = max(DRIFT_MIN_FEATURES, math.ceil(len(self.feature_names) * DRIFT_SHARE))
        return len(drifted) >= floor

    def predict(self, snapshot: TokenSnapshot) -> Prediction | None:
        row = self.prediction_row(snapshot)
        vector = align(row, self.feature_names)
        try:
            # A named frame keeps the estimator's own feature check working and
            # stops scikit-learn warning about unnamed input on every token.
            inputs = pd.DataFrame([vector], columns=self.feature_names)
            raw = float(self._model.predict(inputs)[0])
        except Exception as exc:  # noqa: BLE001 - a model failure must not fail the audit
            log.error("alpha_model_predict_failed", extra={"error": str(exc)[:200]})
            return None
        if not math.isfinite(raw):
            log.error("alpha_model_predict_nonfinite", extra={"value": raw})
            return None

        drifted = self._out_of_range(row)
        input_coverage = coverage(row)
        drift_ratio = len(drifted) / len(self.feature_names) if self.feature_names else 0.0
        confidence = max(0.0, min(1.0, input_coverage * (1.0 - min(drift_ratio, 1.0))))
        return Prediction(
            alpha_score=round(clamp(raw), 2),
            confidence=round(confidence, 3),
            model_id=self.model_id,
            coverage=round(input_coverage, 3),
            out_of_range=drifted,
            drift_material=self._drift_is_material(drifted),
        )


def escalation_reason(
    prediction: Prediction | None,
    *,
    thresholds: "ThresholdSettings",
    margin: float,
    min_confidence: float,
) -> str | None:
    """``None`` means "the model's answer stands"; a string is why the LLM is needed."""
    if prediction is None:
        return "no_model"
    if prediction.confidence < min_confidence:
        return f"low_confidence {prediction.confidence:.2f} < {min_confidence:.2f}"
    if prediction.drift_material:
        return "out_of_range " + ",".join(prediction.out_of_range[:3])
    edges: Sequence[float] = (thresholds.min_final_score, thresholds.watch_score)
    for edge in edges:
        if abs(prediction.alpha_score - edge) <= margin:
            return f"near_boundary {edge:.0f} (prediction {prediction.alpha_score:.1f})"
    return None


def build_predictor(settings: "Settings") -> tuple[AlphaPredictor | None, str]:
    """Resolve ``settings.ml.alpha_predictor`` into a predictor (or ``None`` for LLM-only).

    A missing or corrupt artefact degrades to the LLM path with a loud log line
    instead of failing boot: an unavailable model is a cost regression, not an
    outage, and the scanner must keep scanning.

    The returned mode is the *authoritative* one. With ``alpha_predictor=llm`` and
    ``shadow=true`` a model is still loaded for measurement, but the mode stays
    ``llm`` so :class:`ai_engine.dual_audit_flow.DualAuditFlow` keeps the LLM in charge.
    """
    mode = (settings.ml.alpha_predictor or MODE_LLM).strip().lower()
    if mode not in VALID_MODES:
        log.error("alpha_predictor_unknown", extra={"mode": mode, "fallback": MODE_LLM})
        return None, MODE_LLM
    if mode == MODE_PRIOR:
        return PriorPredictor(), MODE_PRIOR

    if mode == MODE_LLM and not settings.ml.shadow:
        # The default: no model is loaded, no pandas work per token.
        return None, MODE_LLM

    predictor = ModelPredictor.from_path(settings.ml.model_path)
    if predictor is None:
        log.error(
            "alpha_predictor_unavailable",
            extra={"mode": mode, "path": str(settings.ml.model_path), "fallback": MODE_LLM},
        )
        return None, MODE_LLM
    log.info(
        "alpha_predictor_loaded",
        extra={
            "model": predictor.model_id,
            "features": len(predictor.feature_names),
            "authoritative": mode == MODE_MODEL,
            "shadow": settings.ml.shadow,
        },
    )
    return predictor, mode


__all__ = [
    "AlphaPredictor",
    "FORBIDDEN_FEATURES",
    "MODE_LLM",
    "MODE_MODEL",
    "MODE_PRIOR",
    "ModelPredictor",
    "Prediction",
    "PriorPredictor",
    "VALID_MODES",
    "build_predictor",
    "escalation_reason",
]
