"""Deterministic scoring & the veto hierarchy.

The LLM is a *judge of narrative*, never the sole source of truth. Everything
below is pure arithmetic so the final decision is reproducible, auditable and
unit-testable:

    prior   = confidence/weight-weighted mean of the contributing layer scores
    blended = (1 - w) * prior + w * alpha_score          (w = ALPHA_WEIGHT)
    final   = blended * (1 - risk_drag * risk_score / 100)

Veto precedence (first match wins, and it is absolute):
    1. hard security flag from GoPlus (honeypot, hidden owner, >10% sell tax...)
    2. Agent 2's veto (explicit flag, CRITICAL severity or CRITICAL finding)
    3. risk_score >= MAX_RISK_SCORE
    4. wash-trading suspicion >= 85 (fabricated volume)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from config.settings import ThresholdSettings
from core.models import (
    LayerSignal,
    RiskAudit,
    SecurityReport,
    Severity,
    VerdictStatus,
    WashTradingReport,
    clamp,
)

NEUTRAL_SCORE = 50.0
WASH_VETO_THRESHOLD = 85.0


@dataclass(slots=True)
class Decision:
    """Result of :func:`reconcile` - everything needed to build a FinalVerdict."""

    status: VerdictStatus
    final_score: float
    blended_score: float
    position_multiplier: float
    rationale: str
    veto_reasons: list[str] = field(default_factory=list)

    @property
    def vetoed(self) -> bool:
        return self.status is VerdictStatus.VETOED


# ---------------------------------------------------------------------------
# Layer aggregation
# ---------------------------------------------------------------------------
def weighted_prior(signals: Iterable[LayerSignal]) -> float:
    """Confidence-weighted mean of contributing layer scores.

    A failed provider (``UNAVAILABLE``) or a non-applicable layer (``SKIPPED``)
    contributes zero weight instead of dragging the score toward neutral -
    silence must not be mistaken for a bearish signal.
    """
    usable = [signal for signal in signals if signal.contributes and signal.confidence > 0]
    total_weight = sum(signal.effective_weight for signal in usable)
    if total_weight <= 0:
        return NEUTRAL_SCORE
    weighted_sum = sum(signal.score * signal.effective_weight for signal in usable)
    return clamp(weighted_sum / total_weight)


def blend_alpha(prior: float, alpha_score: float, alpha_weight: float = 0.6) -> float:
    weight = clamp(alpha_weight, 0.0, 1.0)
    return clamp((1.0 - weight) * prior + weight * clamp(alpha_score))


def risk_adjusted_score(blended: float, risk_score: float, risk_drag: float = 0.5) -> float:
    drag = clamp(risk_drag, 0.0, 1.0)
    return clamp(blended * (1.0 - drag * clamp(risk_score) / 100.0))


def deterministic_risk_score(security: SecurityReport, wash: WashTradingReport) -> float:
    """A quantitative risk floor handed to Agent 2 as context."""
    contract_risk = security.risk_points() if security.available else 0.0
    wash_risk = wash.suspicion_score if wash.evaluated else 0.0
    score = 0.7 * contract_risk + 0.3 * wash_risk
    if security.critical_flags():
        score = max(score, 85.0)
    if wash.evaluated and wash_risk >= WASH_VETO_THRESHOLD:
        score = max(score, 85.0)
    return clamp(score)


def position_multiplier(final_score: float, tiers: Sequence[tuple[float, float]]) -> float:
    for threshold, multiplier in sorted(tiers, reverse=True):
        if final_score >= threshold:
            return multiplier
    return 0.0


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------
def reconcile(
    *,
    prior_score: float,
    alpha_score: float,
    risk: RiskAudit,
    security: SecurityReport,
    wash: WashTradingReport,
    thresholds: ThresholdSettings,
) -> Decision:
    """Apply the veto hierarchy and produce the final status/score."""
    blended = blend_alpha(prior_score, alpha_score, thresholds.alpha_weight)
    final = risk_adjusted_score(blended, risk.risk_score, thresholds.risk_drag)

    hard_flags = security.critical_flags()
    veto_reasons: list[str] = []

    if hard_flags:
        veto_reasons.append(f"hard security flags: {', '.join(hard_flags)}")
    if risk.effective_veto():
        veto_reasons.append(f"risk auditor veto ({risk.severity.value}, risk={risk.risk_score:.1f})")
    if risk.risk_score >= thresholds.max_risk_score:
        veto_reasons.append(
            f"risk score {risk.risk_score:.1f} >= MAX_RISK_SCORE {thresholds.max_risk_score:.1f}"
        )
    if wash.evaluated and wash.suspicion_score >= WASH_VETO_THRESHOLD:
        veto_reasons.append(f"wash-trading suspicion {wash.suspicion_score:.1f} >= {WASH_VETO_THRESHOLD:.0f}")

    if veto_reasons:
        # A veto is a hard stop: the score is capped below any actionable band.
        capped = min(final * 0.5, thresholds.veto_score_ceiling)
        return Decision(
            status=VerdictStatus.VETOED,
            final_score=round(capped, 2),
            blended_score=round(blended, 2),
            position_multiplier=0.0,
            rationale="VETO - " + "; ".join(veto_reasons),
            veto_reasons=veto_reasons,
        )

    if final >= thresholds.min_final_score:
        status = VerdictStatus.APPROVED
        rationale = (
            f"Approved: blended {blended:.1f} adjusted to {final:.1f} "
            f"after risk {risk.risk_score:.1f}; both agents aligned."
        )
    elif final >= thresholds.watch_score:
        status = VerdictStatus.WATCH
        rationale = (
            f"Watchlist: adjusted score {final:.1f} below approval floor "
            f"{thresholds.min_final_score:.1f}."
        )
    else:
        status = VerdictStatus.REJECTED
        rationale = f"Rejected: adjusted score {final:.1f} below watch floor {thresholds.watch_score:.1f}."

    multiplier = position_multiplier(final, thresholds.position_tiers) if status is VerdictStatus.APPROVED else 0.0
    return Decision(
        status=status,
        final_score=round(final, 2),
        blended_score=round(blended, 2),
        position_multiplier=multiplier,
        rationale=rationale,
    )


def severity_for_score(score: float) -> Severity:
    """Map a 0-100 risk score to a severity bucket (used when a model omits it)."""
    if score >= 80:
        return Severity.CRITICAL
    if score >= 60:
        return Severity.HIGH
    if score >= 35:
        return Severity.MEDIUM
    return Severity.LOW


__all__ = [
    "Decision",
    "NEUTRAL_SCORE",
    "WASH_VETO_THRESHOLD",
    "blend_alpha",
    "deterministic_risk_score",
    "position_multiplier",
    "reconcile",
    "risk_adjusted_score",
    "severity_for_score",
    "weighted_prior",
]
