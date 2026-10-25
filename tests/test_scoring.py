"""Deterministic arithmetic: silence is not bearish, vetoes are absolute."""

from __future__ import annotations

import pytest

from config.settings import ThresholdSettings
from core.models import (
    Layer,
    LayerSignal,
    RiskAudit,
    RiskFinding,
    SecurityReport,
    Severity,
    SignalStatus,
    VerdictStatus,
    WashTradingReport,
)
from core.scoring import (
    NEUTRAL_SCORE,
    WASH_VETO_THRESHOLD,
    blend_alpha,
    deterministic_risk_score,
    position_multiplier,
    reconcile,
    risk_adjusted_score,
    severity_for_score,
    weighted_prior,
)

THRESHOLDS = ThresholdSettings()


def signal(layer: Layer, score: float, confidence: float = 1.0, weight: float = 1.0, status=SignalStatus.OK):
    return LayerSignal(layer=layer, score=score, confidence=confidence, weight=weight, status=status)


def risk_audit(score: float = 10.0, *, veto: bool = False, severity=Severity.LOW, findings=()):
    return RiskAudit(
        risk_score=score,
        veto=veto,
        severity=severity,
        findings=list(findings),
        reasoning_summary="scripted",
        model="test-risk",
    )


class TestWeightedPrior:
    def test_unavailable_layers_are_ignored_not_averaged(self):
        prior = weighted_prior(
            [
                signal(Layer.DEX, 80.0),
                signal(Layer.DERIVATIVES, 5.0, confidence=0.0, status=SignalStatus.UNAVAILABLE),
                signal(Layer.SOCIAL, 0.0, status=SignalStatus.SKIPPED),
            ]
        )
        assert prior == pytest.approx(80.0)

    def test_confidence_is_the_weight(self):
        prior = weighted_prior(
            [signal(Layer.DEX, 90.0, confidence=0.8), signal(Layer.SOCIAL, 40.0, confidence=0.2)]
        )
        assert prior == pytest.approx(80.0)

    def test_layer_weight_multiplies_confidence(self):
        prior = weighted_prior(
            [
                signal(Layer.DEX, 100.0, weight=3.0, confidence=1.0),
                signal(Layer.SOCIAL, 0.0, weight=1.0, confidence=1.0),
            ]
        )
        assert prior == pytest.approx(75.0)

    def test_no_usable_signal_is_neutral(self):
        assert weighted_prior([]) == NEUTRAL_SCORE
        assert weighted_prior([signal(Layer.DEX, 0.0, status=SignalStatus.UNAVAILABLE)]) == NEUTRAL_SCORE

    def test_out_of_range_scores_are_clamped(self):
        assert weighted_prior([signal(Layer.DEX, 400.0)]) == 100.0


class TestBlendAndRisk:
    def test_blend_respects_alpha_weight(self):
        assert blend_alpha(60.0, 100.0, 0.6) == pytest.approx(84.0)

    def test_blend_zero_weight_is_prior(self):
        assert blend_alpha(60.0, 100.0, 0.0) == pytest.approx(60.0)

    def test_risk_drag_shrinks_score(self):
        assert risk_adjusted_score(80.0, 40.0, 0.5) == pytest.approx(64.0)

    def test_zero_drag_is_identity(self):
        assert risk_adjusted_score(80.0, 90.0, 0.0) == pytest.approx(80.0)

    def test_deterministic_risk_floor_from_critical_flags(self):
        security = SecurityReport(available=True, flags=["honeypot", "mintable"])
        assert deterministic_risk_score(security, WashTradingReport()) == pytest.approx(85.0)

    def test_deterministic_risk_floor_from_wash(self):
        wash = WashTradingReport(evaluated=True, suspicion_score=WASH_VETO_THRESHOLD)
        assert deterministic_risk_score(SecurityReport(available=True), wash) == pytest.approx(85.0)

    def test_deterministic_risk_ignores_unavailable_sources(self):
        assert deterministic_risk_score(SecurityReport(available=False), WashTradingReport()) == 0.0


class TestPositionSizing:
    def test_tiers_are_ordered(self):
        assert position_multiplier(90.0, THRESHOLDS.position_tiers) == 1.0
        assert position_multiplier(80.0, THRESHOLDS.position_tiers) == 0.75
        assert position_multiplier(66.0, THRESHOLDS.position_tiers) == 0.5
        assert position_multiplier(10.0, THRESHOLDS.position_tiers) == 0.0


class TestReconcile:
    def test_approval_path(self):
        decision = reconcile(
            prior_score=60.0,
            alpha_score=95.0,
            risk=risk_audit(5.0),
            security=SecurityReport(available=True),
            wash=WashTradingReport(evaluated=True, suspicion_score=10.0),
            thresholds=THRESHOLDS,
        )
        assert decision.status is VerdictStatus.APPROVED
        assert decision.final_score > THRESHOLDS.min_final_score
        assert decision.position_multiplier > 0
        assert decision.veto_reasons == []
        assert "Approved" in decision.rationale

    def test_watch_band(self):
        decision = reconcile(
            prior_score=55.0,
            alpha_score=55.0,
            risk=risk_audit(0.0),
            security=SecurityReport(available=True),
            wash=WashTradingReport(),
            thresholds=THRESHOLDS,
        )
        assert decision.status is VerdictStatus.WATCH
        assert decision.position_multiplier == 0.0
        assert "Watchlist" in decision.rationale

    def test_rejection_band(self):
        decision = reconcile(
            prior_score=10.0,
            alpha_score=10.0,
            risk=risk_audit(0.0),
            security=SecurityReport(available=True),
            wash=WashTradingReport(),
            thresholds=THRESHOLDS,
        )
        assert decision.status is VerdictStatus.REJECTED
        assert "Rejected" in decision.rationale

    def test_hard_security_flag_vetoes(self):
        decision = reconcile(
            prior_score=99.0,
            alpha_score=99.0,
            risk=risk_audit(0.0),
            security=SecurityReport(available=True, flags=["honeypot"]),
            wash=WashTradingReport(),
            thresholds=THRESHOLDS,
        )
        assert decision.status is VerdictStatus.VETOED
        assert decision.vetoed
        assert decision.final_score <= THRESHOLDS.veto_score_ceiling
        assert decision.position_multiplier == 0.0
        assert any("hard security flags" in reason for reason in decision.veto_reasons)

    def test_agent_veto_wins_even_with_false_boolean(self):
        audit = RiskAudit(
            risk_score=10.0,
            veto=False,
            severity=Severity.CRITICAL,
            reasoning_summary="rug imminent",
            model="test-risk",
        )
        decision = reconcile(
            prior_score=99.0,
            alpha_score=99.0,
            risk=audit,
            security=SecurityReport(available=True),
            wash=WashTradingReport(),
            thresholds=THRESHOLDS,
        )
        assert decision.status is VerdictStatus.VETOED
        assert any("risk auditor veto" in reason for reason in decision.veto_reasons)

    def test_critical_finding_escalates_to_veto(self):
        audit = risk_audit(
            20.0, findings=[RiskFinding(category="ownership", severity=Severity.CRITICAL, description="mint")]
        )
        decision = reconcile(
            prior_score=90.0,
            alpha_score=90.0,
            risk=audit,
            security=SecurityReport(available=True),
            wash=WashTradingReport(),
            thresholds=THRESHOLDS,
        )
        assert decision.status is VerdictStatus.VETOED

    def test_max_risk_score_veto(self):
        decision = reconcile(
            prior_score=99.0,
            alpha_score=99.0,
            risk=risk_audit(THRESHOLDS.max_risk_score),
            security=SecurityReport(available=True),
            wash=WashTradingReport(),
            thresholds=THRESHOLDS,
        )
        assert decision.status is VerdictStatus.VETOED
        assert any("MAX_RISK_SCORE" in reason for reason in decision.veto_reasons)

    def test_wash_trading_veto(self):
        decision = reconcile(
            prior_score=99.0,
            alpha_score=99.0,
            risk=risk_audit(0.0),
            security=SecurityReport(available=True),
            wash=WashTradingReport(evaluated=True, suspicion_score=WASH_VETO_THRESHOLD),
            thresholds=THRESHOLDS,
        )
        assert decision.status is VerdictStatus.VETOED
        assert any("wash-trading" in reason for reason in decision.veto_reasons)

    def test_unevaluated_wash_never_vetoes(self):
        decision = reconcile(
            prior_score=90.0,
            alpha_score=90.0,
            risk=risk_audit(0.0),
            security=SecurityReport(available=True),
            wash=WashTradingReport(evaluated=False, suspicion_score=100.0),
            thresholds=THRESHOLDS,
        )
        assert decision.status is VerdictStatus.APPROVED

    def test_all_reasons_are_reported_in_precedence_order(self):
        decision = reconcile(
            prior_score=99.0,
            alpha_score=99.0,
            risk=risk_audit(95.0, veto=True, severity=Severity.CRITICAL),
            security=SecurityReport(available=True, flags=["honeypot", "blacklist_enabled"]),
            wash=WashTradingReport(evaluated=True, suspicion_score=90.0),
            thresholds=THRESHOLDS,
        )
        assert len(decision.veto_reasons) == 4
        assert decision.veto_reasons[0].startswith("hard security flags")
        assert decision.veto_reasons[1].startswith("risk auditor veto")
        assert "MAX_RISK_SCORE" in decision.veto_reasons[2]
        assert decision.veto_reasons[3].startswith("wash-trading")
        assert decision.rationale.startswith("VETO - ")

    def test_hard_flags_veto_even_when_the_screen_is_unavailable(self):
        """A honeypot flag is a veto first and an availability detail second."""
        decision = reconcile(
            prior_score=90.0,
            alpha_score=90.0,
            risk=risk_audit(0.0),
            security=SecurityReport(available=False, flags=["honeypot"]),
            wash=WashTradingReport(),
            thresholds=THRESHOLDS,
        )
        assert decision.status is VerdictStatus.VETOED
        assert decision.veto_reasons[0].startswith("hard security flags")


class TestSeverityMapping:
    @pytest.mark.parametrize(
        "score,expected",
        [(0, Severity.LOW), (34.9, Severity.LOW), (35, Severity.MEDIUM), (60, Severity.HIGH), (80, Severity.CRITICAL)],
    )
    def test_buckets(self, score, expected):
        assert severity_for_score(score) is expected
