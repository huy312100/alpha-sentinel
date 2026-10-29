"""The predictor and the escalation policy that keeps the LLM on the hard cases."""

from __future__ import annotations

import math
import pickle
from dataclasses import replace

from ai_engine.dual_audit_flow import DualAuditFlow
from ai_engine.llm_client import ChatResult
from config.settings import Settings
from core.models import Layer, LayerSignal, SecurityReport, TokenSnapshot, WashTradingReport
from ml.predictor import (
    MODE_LLM,
    MODE_MODEL,
    MODE_PRIOR,
    ModelPredictor,
    Prediction,
    PriorPredictor,
    build_predictor,
    escalation_reason,
)

ALPHA_JSON = '{"alpha_score": 80, "confidence": 0.8, "thesis": "scripted"}'
RISK_JSON = '{"risk_score": 18, "veto": false, "severity": "low"}'


class ConstantModel:
    """Stands in for a fitted estimator: answers the same number every time."""

    def __init__(self, value: float) -> None:
        self.value = value

    def predict(self, inputs):  # noqa: ANN001 - mirrors the sklearn signature
        return [self.value for _ in range(len(inputs))]


class FailingModel:
    def predict(self, inputs):  # noqa: ANN001
        raise RuntimeError("estimator exploded")


def bundle(model=None, *, feature_names=("dex_score", "prior_score"), feature_range=None) -> dict:
    return {
        "model": model if model is not None else ConstantModel(72.0),
        "feature_names": list(feature_names),
        "manifest": {
            "trained_at": "2026-10-29T00:00:00+00:00",
            "feature_range": feature_range if feature_range is not None else {"dex_score": [0.0, 100.0]},
        },
    }


def snapshot(**overrides) -> TokenSnapshot:
    defaults = {
        "token_address": "0x" + "ef" * 20,
        "chain": "ethereum",
        "asset_class": "crypto",
        "symbol": "TEST",
        "price_usd": 1.0,
        "market_cap_usd": 10_000_000.0,
        "fdv_usd": 10_000_000.0,
        "liquidity_usd": 500_000.0,
        "volume_24h_usd": 1_000_000.0,
        "volume_1h_usd": 50_000.0,
        "price_change_24h_pct": 1.0,
        "price_change_1h_pct": 0.2,
        "holder_count": 4_000,
        "age_hours": 500.0,
        "security": SecurityReport(available=True),
        "wash": WashTradingReport(evaluated=True, suspicion_score=5.0),
        "signals": [LayerSignal(layer=Layer.DEX, score=70.0, confidence=0.9, weight=1.0)],
    }
    defaults.update(overrides)
    return TokenSnapshot(**defaults)


class FakePredictor:
    model_id = "fake:test"

    def __init__(self, score: float, confidence: float = 0.95, out_of_range=None) -> None:
        self.prediction = Prediction(
            alpha_score=score,
            confidence=confidence,
            model_id=self.model_id,
            out_of_range=list(out_of_range or []),
        )

    def predict(self, snapshot: TokenSnapshot) -> Prediction:
        return self.prediction


class CountingClient:
    """Scripted two-agent stand-in that counts what it was actually asked."""

    def __init__(self, *, alpha: str = ALPHA_JSON, risk: str = RISK_JSON) -> None:
        self.alpha = alpha
        self.risk = risk
        self.alpha_calls = 0
        self.risk_calls = 0

    async def chat_json(self, *, model=None, system, user, temperature=0.2, max_tokens=None, json_mode=True):
        if "SENTINEL-RISK" in system:
            self.risk_calls += 1
            content = self.risk
        else:
            self.alpha_calls += 1
            content = self.alpha
        return ChatResult(content=content, model="scripted")


# -- the predictor itself ---------------------------------------------------
def test_a_clean_snapshot_predicts_with_full_confidence():
    predictor = ModelPredictor(bundle(ConstantModel(72.5)))

    prediction = predictor.predict(snapshot())

    assert prediction is not None
    assert prediction.alpha_score == 72.5
    assert prediction.confidence == 1.0
    assert prediction.out_of_range == []
    assert prediction.model_id.startswith("gbm:")


def test_predictions_are_clamped_into_range():
    prediction = ModelPredictor(bundle(ConstantModel(140.0))).predict(snapshot())

    assert prediction.alpha_score == 100.0


def test_a_feature_outside_the_training_range_is_reported():
    predictor = ModelPredictor(bundle(ConstantModel(70.0), feature_range={"dex_score": [0.0, 60.0]}))

    prediction = predictor.predict(snapshot())  # dex_score is 70

    assert prediction.out_of_range == ["dex_score"]
    assert prediction.confidence < 1.0


def test_a_thin_snapshot_loses_confidence():
    plain = ModelPredictor(bundle(ConstantModel(70.0))).predict(snapshot())
    thin = ModelPredictor(bundle(ConstantModel(70.0))).predict(TokenSnapshot(token_address="0x1", chain="ethereum"))

    assert thin.confidence < plain.confidence
    # The deterministic prior is always computable, so coverage never reaches
    # zero on this path - it bottoms out at 1/len(CORE_FEATURES).
    assert 0.0 < thin.coverage < 0.2


def test_a_failing_estimator_yields_no_prediction():
    assert ModelPredictor(bundle(FailingModel())).predict(snapshot()) is None


def test_a_non_finite_prediction_is_rejected():
    assert ModelPredictor(bundle(ConstantModel(float("nan")))).predict(snapshot()) is None


def test_a_missing_artifact_is_not_a_crash(tmp_path):
    assert ModelPredictor.from_path(tmp_path / "nope.pkl") is None


def test_an_artifact_that_learned_from_its_own_label_is_rejected(tmp_path):
    path = tmp_path / "leaky.pkl"
    path.write_bytes(pickle.dumps(bundle(feature_names=("dex_score", "llm_alpha_score"))))

    assert ModelPredictor.from_path(path) is None


def test_the_prior_predictor_is_the_deterministic_baseline():
    prediction = PriorPredictor().predict(snapshot())

    assert prediction.model_id == "prior"
    assert 0.0 <= prediction.alpha_score <= 100.0
    assert prediction.confidence == 1.0


# -- the escalation policy --------------------------------------------------
def test_a_confident_prediction_far_from_the_boundaries_is_not_escalated(settings: Settings):
    prediction = Prediction(alpha_score=82.0, confidence=0.95, model_id="gbm")
    thresholds = settings.thresholds

    assert escalation_reason(prediction, thresholds=thresholds, margin=5.0, min_confidence=0.6) is None


def test_a_prediction_next_to_the_approval_floor_is_escalated(settings: Settings):
    prediction = Prediction(alpha_score=63.0, confidence=0.95, model_id="gbm")

    reason = escalation_reason(prediction, thresholds=settings.thresholds, margin=5.0, min_confidence=0.6)

    assert reason.startswith("near_boundary 65")


def test_a_prediction_next_to_the_watch_floor_is_escalated(settings: Settings):
    prediction = Prediction(alpha_score=48.0, confidence=0.95, model_id="gbm")

    reason = escalation_reason(prediction, thresholds=settings.thresholds, margin=5.0, min_confidence=0.6)

    assert reason.startswith("near_boundary 50")


def test_low_input_trust_is_escalated(settings: Settings):
    prediction = Prediction(alpha_score=85.0, confidence=0.2, model_id="gbm")

    reason = escalation_reason(prediction, thresholds=settings.thresholds, margin=5.0, min_confidence=0.6)

    assert reason.startswith("low_confidence")


def test_drift_is_escalated(settings: Settings):
    prediction = Prediction(
        alpha_score=85.0, confidence=0.95, model_id="gbm", out_of_range=["dex_score"], drift_material=True
    )

    reason = escalation_reason(prediction, thresholds=settings.thresholds, margin=5.0, min_confidence=0.6)

    assert reason.startswith("out_of_range")


def test_a_single_counter_out_of_range_is_not_drift(settings: Settings):
    """One integer counter moving must not spend an API call."""
    predictor = ModelPredictor(
        bundle(ConstantModel(80.0), feature_names=("dex_score", "prior_score", "wash_trade_count"))
    )
    predictor._range["wash_trade_count"] = (0.0, 5.0)  # trained on 0-5, this token has 9

    prediction = predictor.predict(snapshot(wash=WashTradingReport(evaluated=True, suspicion_score=5.0, trade_count=9)))

    assert prediction.out_of_range == ["wash_trade_count"]
    assert prediction.drift_material is False
    assert escalation_reason(prediction, thresholds=settings.thresholds, margin=5.0, min_confidence=0.6) is None


def test_a_core_market_input_out_of_range_is_drift(settings: Settings):
    predictor = ModelPredictor(bundle(ConstantModel(80.0), feature_names=("liquidity_usd", "prior_score")))
    predictor._range["liquidity_usd"] = (1e5, 2e5)  # the token has 500k

    prediction = predictor.predict(snapshot())

    assert prediction.out_of_range == ["liquidity_usd"]
    assert prediction.drift_material is True
    # One of two features drifting halves the trust score, so the low-confidence
    # rule would fire first; hold it out to prove drift is what escalates here.
    reason = escalation_reason(prediction, thresholds=settings.thresholds, margin=5.0, min_confidence=0.4)

    assert reason.startswith("out_of_range")


def test_no_prediction_means_no_model(settings: Settings):
    assert escalation_reason(None, thresholds=settings.thresholds, margin=5.0, min_confidence=0.6) == "no_model"


# -- wiring -----------------------------------------------------------------
def test_the_default_configuration_loads_no_model(settings: Settings):
    predictor, mode = build_predictor(settings)

    assert predictor is None
    assert mode == MODE_LLM


def test_a_missing_artifact_falls_back_instead_of_failing_boot(settings: Settings):
    broken = replace(settings, ml=replace(settings.ml, alpha_predictor=MODE_MODEL))

    predictor, mode = build_predictor(broken)

    assert predictor is None
    assert mode == MODE_LLM


def test_shadow_mode_loads_a_model_but_keeps_the_llm_authoritative(settings: Settings, tmp_path):
    path = tmp_path / "alpha.pkl"
    path.write_bytes(pickle.dumps(bundle()))
    shadow = replace(settings, ml=replace(settings.ml, shadow=True, model_path=path))

    predictor, mode = build_predictor(shadow)

    assert isinstance(predictor, ModelPredictor)
    assert mode == MODE_LLM


def test_prior_mode_needs_no_artifact(settings: Settings):
    predictor, mode = build_predictor(replace(settings, ml=replace(settings.ml, alpha_predictor=MODE_PRIOR)))

    assert isinstance(predictor, PriorPredictor)
    assert mode == MODE_PRIOR


async def test_a_confident_model_answer_skips_the_alpha_agent(settings: Settings):
    client = CountingClient()
    flow = DualAuditFlow(settings, client=client, predictor=FakePredictor(78.0))

    outcome = await flow.audit_detailed(snapshot())

    assert client.alpha_calls == 0, "the whole point: no alpha call for a confident prediction"
    assert client.risk_calls == 1, "the risk auditor still runs"
    assert outcome.alpha_source == "model"
    assert outcome.alpha.alpha_score == 78.0
    assert outcome.alpha.model == "fake:test"
    assert outcome.model_alpha_score == 78.0
    assert outcome.escalation_reason is None
    assert "Learned scorer" in outcome.alpha.thesis


async def test_a_boundary_prediction_is_escalated_to_the_llm(settings: Settings):
    client = CountingClient()
    flow = DualAuditFlow(settings, client=client, predictor=FakePredictor(63.0))

    outcome = await flow.audit_detailed(snapshot())

    assert client.alpha_calls == 1
    assert outcome.alpha_source == "llm"
    assert outcome.alpha.alpha_score == 80.0
    assert outcome.escalation_reason.startswith("near_boundary")
    assert outcome.model_alpha_score == 63.0


async def test_shadow_mode_calls_both_and_records_the_pair(settings: Settings):
    client = CountingClient()
    shadow = replace(settings, ml=replace(settings.ml, shadow=True))
    flow = DualAuditFlow(shadow, client=client, predictor=FakePredictor(78.0))

    outcome = await flow.audit_detailed(snapshot())

    assert client.alpha_calls == 1 and client.risk_calls == 1
    assert outcome.alpha_source == "llm"
    assert outcome.alpha.alpha_score == 80.0
    assert outcome.model_alpha_score == 78.0
    assert outcome.escalation_reason == "shadow"


async def test_prior_mode_never_asks_for_an_alpha_judgement(settings: Settings):
    client = CountingClient()
    prior_settings = replace(settings, ml=replace(settings.ml, alpha_predictor=MODE_PRIOR))
    flow = DualAuditFlow(prior_settings, client=client)

    outcome = await flow.audit_detailed(snapshot())

    assert client.alpha_calls == 0
    assert client.risk_calls == 1
    assert outcome.alpha_source == MODE_PRIOR
    assert outcome.alpha.model == "prior"


async def test_a_broken_predictor_falls_back_to_the_llm(settings: Settings):
    class Exploding:
        model_id = "boom"

        def predict(self, snapshot: TokenSnapshot):
            raise RuntimeError("model unavailable")

    client = CountingClient()
    flow = DualAuditFlow(settings, client=client, predictor=Exploding())

    outcome = await flow.audit_detailed(snapshot())

    assert client.alpha_calls == 1
    assert outcome.alpha_source == "llm"
    assert outcome.verdict.status.value in {"approved", "watch", "rejected", "vetoed"}


def test_escalation_margins_come_from_settings(settings: Settings):
    wide = replace(settings, ml=replace(settings.ml, escalate_margin=20.0))
    prediction = Prediction(alpha_score=82.0, confidence=0.95, model_id="gbm")

    assert escalation_reason(prediction, thresholds=wide.thresholds, margin=wide.ml.escalate_margin, min_confidence=0.6)


def test_the_score_is_finite_in_every_branch():
    for value in (-50.0, 0.0, 50.0, 200.0):
        prediction = ModelPredictor(bundle(ConstantModel(value))).predict(snapshot())
        assert not math.isnan(prediction.alpha_score)
        assert 0.0 <= prediction.alpha_score <= 100.0
