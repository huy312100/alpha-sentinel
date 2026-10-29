"""The dual-agent audit flow: V3 finds the edge, R1 tries to kill it.

Pipeline
--------
1. ``weighted_prior`` collapses the measured layers into a deterministic prior.
2. **Agent 1** (``ALPHA_MODEL``, e.g. a fast chat model) scores asymmetric upside, seeing the prior.
3. **Agent 2** (``RISK_MODEL``, usually the strongest reasoner) audits downside *and sees Agent 1's thesis*
   so it can attack the weakest assumption instead of re-deriving the whole picture.
4. Deterministic risk floors (GoPlus + wash-trading) are applied on top of the LLM.
5. :func:`core.scoring.reconcile` owns the veto hierarchy and the final status.

Failure policy is **fail closed**: if the risk auditor cannot answer, the token is
vetoed. An unreadable LLM is not evidence of safety.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ai_engine.llm_client import LLMBudgetExceeded, LLMClient
from ai_engine.parsing import (
    ResponseParseError,
    extract_json,
    parse_alpha,
    parse_risk,
    require_risk_payload,
)
from ai_engine.prompts import (
    ALPHA_SYSTEM_PROMPT,
    PROMPT_VERSION,
    RISK_SYSTEM_PROMPT,
    build_alpha_user_prompt,
    build_risk_user_prompt,
)
from config.settings import Settings
from core.logging import get_logger
from core.models import (
    AlphaAssessment,
    FinalVerdict,
    RiskAudit,
    RiskFinding,
    SecurityReport,
    Severity,
    TokenSnapshot,
    clamp,
)
from core.scoring import Decision, deterministic_risk_score, reconcile, weighted_prior
from ml.predictor import (
    MODE_LLM,
    MODE_MODEL,
    MODE_PRIOR,
    AlphaPredictor,
    Prediction,
    build_predictor,
    escalation_reason,
)

log = get_logger(__name__)

AUDITOR_UNAVAILABLE_RISK = 80.0


@dataclass(slots=True)
class AuditOutcome:
    """Rich result for the bot/dashboard; ``verdict`` is the persisted record."""

    verdict: FinalVerdict
    decision: Decision
    alpha: AlphaAssessment
    risk: RiskAudit
    prior_score: float
    prompt_version: str = PROMPT_VERSION
    degraded: bool = False
    errors: list[str] = field(default_factory=list)
    #: ``llm`` or the model id that actually produced ``alpha``. Persisted so the
    #: audit trail cannot claim an LLM judgement that never happened.
    alpha_source: str = MODE_LLM
    #: Why the cheap path handed this token to the LLM (``None`` = it did not).
    escalation_reason: str | None = None
    #: The model's opinion even when the LLM decided - the shadow-mode pair.
    model_alpha_score: float | None = None

    @property
    def veto_reasons(self) -> list[str]:
        return list(self.decision.veto_reasons)


@dataclass(slots=True)
class _AlphaRun:
    """Internal: the assessment plus who produced it, so the caller can record it."""

    assessment: AlphaAssessment
    source: str = MODE_LLM
    escalation_reason: str | None = None
    model_alpha: float | None = None


class DualAuditFlow:
    """Orchestrates both agents for one snapshot (and many snapshots concurrently)."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: LLMClient | None = None,
        short_circuit_hard_flags: bool = True,
        predictor: AlphaPredictor | None = None,
    ) -> None:
        self._settings = settings
        self._client: Any = client
        self._owns_client = client is None
        self.short_circuit_hard_flags = short_circuit_hard_flags
        # Resolved lazily: with the default configuration no model is loaded and
        # no pandas work happens per token.
        self._predictor = predictor
        self._predictor_mode: str | None = None
        self._predictor_resolved = False

    # -- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> "DualAuditFlow":
        await self.client()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def client(self) -> Any:
        if self._client is None:
            if self._settings.runtime.demo_mode:
                from ai_engine.demo_client import DemoLLMClient

                self._client = DemoLLMClient()
                log.info("dual_audit_demo_agents_enabled")
            else:
                self._client = LLMClient(self._settings.llm)
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            close = getattr(self._client, "close", None)
            if close is not None:
                await close()
        self._client = None

    @property
    def stats(self) -> dict[str, float | int]:
        return self._client.stats if self._client is not None else {}

    # -- spend metering ----------------------------------------------------
    def start_cycle(self, *, day_spend_usd: float = 0.0) -> None:
        """Reset the spend meter for a new cycle and seed today's total.

        ``day_spend_usd`` comes from the hot store, so restarting the process
        mid-day does not hand the daemon a fresh allowance.
        """
        starter = getattr(self._client, "start_cycle", None)
        if starter is not None:
            starter(day_spend_usd=day_spend_usd)

    @property
    def cycle_spend_usd(self) -> float:
        return float(getattr(self._client, "cycle_spend_usd", 0.0) or 0.0)

    def predictor(self) -> tuple[AlphaPredictor | None, str]:
        """The learned scorer (if any) and which source is authoritative for alpha."""
        if not self._predictor_resolved:
            if self._predictor is not None:
                # An injected predictor is authoritative unless shadow is on.
                self._predictor_mode = MODE_LLM if self._settings.ml.shadow else MODE_MODEL
            else:
                self._predictor, self._predictor_mode = build_predictor(self._settings)
            self._predictor_resolved = True
        return self._predictor, self._predictor_mode or MODE_LLM

    # -- public API --------------------------------------------------------
    async def audit_detailed(self, snapshot: TokenSnapshot) -> AuditOutcome:
        started = time.perf_counter()
        prior = weighted_prior(snapshot.signals)
        errors: list[str] = []

        alpha_run = await self._run_alpha(snapshot, prior, errors)
        alpha = alpha_run.assessment
        risk = await self._run_risk(snapshot, alpha, prior, errors)
        self._apply_deterministic_floor(snapshot, risk, errors)

        decision = reconcile(
            prior_score=prior,
            alpha_score=alpha.alpha_score,
            risk=risk,
            security=snapshot.security,
            wash=snapshot.wash,
            thresholds=self._settings.thresholds,
        )
        latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
        verdict = FinalVerdict(
            token_address=snapshot.token_address,
            chain=snapshot.chain,
            asset_class=snapshot.asset_class,
            symbol=snapshot.symbol or "UNKNOWN",
            status=decision.status,
            final_score=decision.final_score,
            prior_score=prior,
            blended_score=decision.blended_score,
            alpha=alpha,
            risk=risk,
            position_multiplier=decision.position_multiplier,
            rationale=decision.rationale,
            latency_ms=latency_ms,
            security_flags=sorted(set(snapshot.security.flags)),
        )
        outcome = AuditOutcome(
            verdict=verdict,
            decision=decision,
            alpha=alpha,
            risk=risk,
            prior_score=prior,
            degraded=bool(errors),
            errors=errors,
            alpha_source=alpha_run.source,
            escalation_reason=alpha_run.escalation_reason,
            model_alpha_score=alpha_run.model_alpha,
        )
        log.info(
            "audit_completed",
            extra={
                "token": f"{snapshot.chain}:{snapshot.token_address}",
                "status": verdict.status.value,
                "final_score": verdict.final_score,
                "prior": round(prior, 2),
                "alpha": round(alpha.alpha_score, 2),
                "risk": round(risk.risk_score, 2),
                "veto": verdict.vetoed,
                "latency_ms": latency_ms,
                "errors": errors,
            },
        )
        return outcome

    async def audit(self, snapshot: TokenSnapshot) -> FinalVerdict:
        return (await self.audit_detailed(snapshot)).verdict

    async def audit_many(self, snapshots: list[TokenSnapshot], *, concurrency: int | None = None) -> list[AuditOutcome]:
        """Audit many snapshots without ever raising for a single failure."""
        limit = max(1, concurrency or self._settings.scanners.token_concurrency)
        semaphore = asyncio.Semaphore(limit)

        async def run(snapshot: TokenSnapshot) -> AuditOutcome:
            async with semaphore:
                return await self.audit_detailed(snapshot)

        results = await asyncio.gather(*(run(snapshot) for snapshot in snapshots), return_exceptions=True)
        outcomes: list[AuditOutcome] = []
        for snapshot, result in zip(snapshots, results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                log.error(
                    "audit_many_failed",
                    extra={
                        "token": snapshot.token_address,
                        "error": f"{type(result).__name__}: {str(result)[:200]}",
                    },
                )
                continue
            outcomes.append(result)
        return outcomes

    # -- agent 1 -----------------------------------------------------------
    async def _run_alpha(self, snapshot: TokenSnapshot, prior: float, errors: list[str]) -> _AlphaRun:
        """Model first, LLM only when the prediction is not safe to act on.

        The escalation policy (see :mod:`ml.predictor`) is deliberately about the
        *decision*, not about how sure the model claims to be: a prediction two
        points away from the approval floor could flip the verdict, so it costs
        an LLM call. Everything further out is taken at face value.
        """
        predictor, mode = self.predictor()
        prediction: Prediction | None = None
        if predictor is not None:
            try:
                prediction = predictor.predict(snapshot)
            except Exception as exc:  # noqa: BLE001 - a broken model is not a broken audit
                log.error(
                    "alpha_predictor_failed",
                    extra={"token": snapshot.token_address, "error": f"{type(exc).__name__}: {str(exc)[:200]}"},
                )
                prediction = None
        reason = self._escalation(prediction, mode)
        shadow = self._settings.ml.shadow or mode == MODE_LLM

        if prediction is not None and not shadow and reason is None:
            assessment = AlphaAssessment(
                alpha_score=prediction.alpha_score,
                confidence=prediction.confidence,
                thesis=(
                    f"Learned scorer {prediction.model_id} predicted {prediction.alpha_score:.1f}/100 "
                    f"(deterministic prior {prior:.1f}). No LLM call was needed."
                ),
                model=prediction.model_id,
            )
            log.info(
                "alpha_from_model",
                extra={
                    "token": f"{snapshot.chain}:{snapshot.token_address}",
                    "model": prediction.model_id,
                    "alpha": prediction.alpha_score,
                    "prior": round(prior, 2),
                    "confidence": prediction.confidence,
                },
            )
            return _AlphaRun(assessment=assessment, source=mode, model_alpha=prediction.alpha_score)

        assessment = await self._alpha_from_llm(snapshot, prior, errors)
        if prediction is not None:
            log.info(
                "alpha_shadow_compare",
                extra={
                    "token": f"{snapshot.chain}:{snapshot.token_address}",
                    "model": prediction.model_id,
                    "model_alpha": prediction.alpha_score,
                    "llm_alpha": assessment.alpha_score,
                    "delta": round(assessment.alpha_score - prediction.alpha_score, 2),
                    "model_confidence": prediction.confidence,
                    "escalation": reason or "shadow",
                },
            )
            if reason is None:
                reason = "shadow"
        return _AlphaRun(
            assessment=assessment,
            source=MODE_LLM,
            escalation_reason=reason,
            model_alpha=prediction.alpha_score if prediction is not None else None,
        )

    def _escalation(self, prediction: Prediction | None, mode: str) -> str | None:
        if mode == MODE_PRIOR:
            # ``prior`` is an explicit A/B of the deterministic path; escalating
            # would quietly re-enable the spend it was chosen to remove.
            return None
        return escalation_reason(
            prediction,
            thresholds=self._settings.thresholds,
            margin=self._settings.ml.escalate_margin,
            min_confidence=self._settings.ml.min_confidence,
        )

    async def _alpha_from_llm(self, snapshot: TokenSnapshot, prior: float, errors: list[str]) -> AlphaAssessment:
        llm = self._settings.llm
        try:
            payload, result = await self._call_json(
                system=ALPHA_SYSTEM_PROMPT,
                user=build_alpha_user_prompt(snapshot, prior),
                model=llm.alpha_model,
                temperature=llm.alpha_temperature,
                max_tokens=llm.max_tokens,
                label="alpha",
                errors=errors,
            )
            return parse_alpha(payload, model=result.model, usage=result.usage, latency_ms=result.latency_ms)
        except LLMBudgetExceeded:
            # Not a provider failure: a ceiling must surface as "we chose to stop
            # spending", never as an unavailable analyst (which is score-neutral)
            # and never as the fail-closed veto below.
            raise
        except Exception as exc:  # noqa: BLE001 - the pipeline must never die on one token
            message = f"alpha agent failed: {type(exc).__name__}: {str(exc)[:200]}"
            errors.append(message)
            log.warning("alpha_agent_failed", extra={"token": snapshot.token_address, "error": message})
            return AlphaAssessment(
                alpha_score=round(prior, 2),
                confidence=0.0,
                thesis=(
                    "Agent 1 unavailable; falling back to the deterministic layer prior "
                    f"({prior:.1f}/100). No discretionary edge was assessed."
                ),
                model="unavailable",
            )

    # -- agent 2 -----------------------------------------------------------
    async def _run_risk(
        self,
        snapshot: TokenSnapshot,
        alpha: AlphaAssessment,
        prior: float,
        errors: list[str],
    ) -> RiskAudit:
        deterministic = deterministic_risk_score(snapshot.security, snapshot.wash)
        hard_flags = snapshot.security.critical_flags()
        if self.short_circuit_hard_flags and hard_flags:
            log.info("risk_audit_short_circuited", extra={"token": snapshot.token_address, "flags": hard_flags})
            return RiskAudit(
                risk_score=max(deterministic, 90.0),
                veto=True,
                severity=Severity.CRITICAL,
                findings=[
                    RiskFinding(
                        category="security",
                        severity=Severity.CRITICAL,
                        description="Deterministic security screen found hard-risk contract behaviour.",
                        evidence=", ".join(hard_flags),
                        score_impact=max(deterministic, 90.0),
                    )
                ],
                reasoning_summary=(
                    "LLM audit skipped: the deterministic GoPlus screen already proves an "
                    f"unacceptable downside ({', '.join(hard_flags)})."
                ),
                model="deterministic-screen",
                confidence=1.0,
            )

        llm = self._settings.llm
        try:
            payload, result = await self._call_json(
                system=RISK_SYSTEM_PROMPT,
                user=build_risk_user_prompt(snapshot, alpha, prior),
                model=llm.risk_model,
                temperature=llm.risk_temperature,
                max_tokens=llm.risk_max_tokens,
                label="risk",
                errors=errors,
                validate=require_risk_payload,
            )
            return parse_risk(payload, model=result.model, usage=result.usage, latency_ms=result.latency_ms)
        except LLMBudgetExceeded:
            # Fail-closed is for an auditor that *cannot* answer. Here the answer
            # was never requested, so a CRITICAL veto would be fabricated - and
            # ALERT_ON_VETO would page for an operator setting, not a finding.
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed, never fail open
            message = f"risk agent failed: {type(exc).__name__}: {str(exc)[:200]}"
            errors.append(message)
            log.error("risk_agent_failed", extra={"token": snapshot.token_address, "error": message})
            return RiskAudit(
                risk_score=max(deterministic, AUDITOR_UNAVAILABLE_RISK),
                veto=True,
                severity=Severity.CRITICAL,
                findings=[
                    RiskFinding(
                        category="execution",
                        severity=Severity.CRITICAL,
                        description="Risk auditor unavailable - unverified downside is unacceptable.",
                        evidence=message,
                        score_impact=max(deterministic, AUDITOR_UNAVAILABLE_RISK),
                    )
                ],
                reasoning_summary="Fail-closed: the risk auditor did not return a usable verdict.",
                model="unavailable",
                confidence=0.0,
            )

    # -- helpers -----------------------------------------------------------
    @property
    def watchdog_seconds(self) -> float:
        """Outer timeout sized to the client's retry budget, not one attempt.

        The aiohttp session already caps each attempt at ``timeout_seconds``; a
        429 + backoff + a normal reasoner call can consume that budget more than
        once. Sizing the watchdog to one attempt would cancel healthy retries and
        fail closed on tokens the provider would have accepted.
        """
        llm = self._settings.llm
        return llm.timeout_seconds * max(1, llm.max_retries) + 15.0

    async def _call_json(
        self,
        *,
        system: str,
        user: str,
        model: str,
        temperature: float,
        max_tokens: int,
        label: str,
        errors: list[str],
        validate: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], Any]:
        """One JSON call plus a single repair attempt when the model breaks format.

        ``validate`` lets a caller reject a structurally valid object that is
        unusable (a risk audit without a risk score, for example) and share the
        same repair path as malformed JSON.
        """
        client = await self.client()

        def accept(payload: dict[str, Any]) -> dict[str, Any]:
            if validate is not None:
                validate(payload)
            return payload

        async def call(prompt: str) -> Any:
            return await asyncio.wait_for(
                client.chat_json(
                    model=model,
                    system=system,
                    user=prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=True,
                ),
                timeout=self.watchdog_seconds,
            )

        result = await call(user)
        try:
            return accept(extract_json(result.content)), result
        except ResponseParseError as exc:
            reason = str(exc)[:200]
            errors.append(f"{label}: unusable JSON; requesting a repair")
            log.warning("agent_json_repair", extra={"agent": label, "model": model, "reason": reason})
        repaired = await call(
            f"{user}\n\nYour previous reply was rejected: it was not a single valid JSON "
            f"object with the required fields ({reason}). Reply with exactly one JSON object "
            "and nothing else."
        )
        return accept(extract_json(repaired.content)), repaired

    def _apply_deterministic_floor(self, snapshot: TokenSnapshot, risk: RiskAudit, errors: list[str]) -> None:
        """The measured screen is a *floor* on danger, never a ceiling."""
        deterministic = deterministic_risk_score(snapshot.security, snapshot.wash)
        if deterministic <= risk.risk_score:
            return
        log.warning(
            "risk_floor_applied",
            extra={
                "token": snapshot.token_address,
                "llm_risk": risk.risk_score,
                "deterministic_risk": deterministic,
            },
        )
        errors.append(f"deterministic risk floor raised {risk.risk_score:.1f} -> {deterministic:.1f}")
        risk.risk_score = round(clamp(deterministic), 2)
        if risk.risk_score >= 80.0:
            risk.veto = True
            risk.severity = Severity.CRITICAL


__all__ = ["AUDITOR_UNAVAILABLE_RISK", "AuditOutcome", "DualAuditFlow"]


# ---------------------------------------------------------------------------
# ``python -m ai_engine.dual_audit_flow --demo`` - offline wiring check
# ---------------------------------------------------------------------------
def _demo_snapshot(honeypot: bool = False) -> TokenSnapshot:
    from core.models import Layer, LayerSignal, SignalStatus, WashTradingReport

    signals = [
        LayerSignal(Layer.ONCHAIN, 74.0, 0.8, 1.2, summary="net exchange outflow, holders +6% w/w"),
        LayerSignal(Layer.DERIVATIVES, 61.0, 0.6, 0.9, summary="funding neutral, OI up"),
        LayerSignal(Layer.SOCIAL, 68.0, 0.4, 0.7, summary="mentions velocity +18%"),
        LayerSignal(Layer.DEX, 71.0, 0.7, 1.1, summary="vol/liq 0.9, buy pressure skew"),
        LayerSignal(Layer.AIRDROP, 50.0, 0.0, 0.5, status=SignalStatus.SKIPPED, summary="no campaign"),
        LayerSignal(Layer.MACRO, 55.0, 0.3, 0.6, summary="risk-on but tightening"),
        LayerSignal(Layer.TECHNICALS, 66.0, 0.6, 0.8, summary="reclaimed 50d, RSI 58"),
    ]
    return TokenSnapshot(
        token_address="0x000000000000000000000000000000000000dEaD",
        chain="ethereum",
        symbol="DEMO",
        name="AlphaSentinel Demo Token",
        price_usd=0.0421,
        market_cap_usd=42_000_000.0,
        liquidity_usd=1_800_000.0,
        volume_24h_usd=2_400_000.0,
        holder_count=12_400,
        age_hours=2_100.0,
        security=SecurityReport(
            available=True,
            honeypot=honeypot,
            buy_tax_pct=1.0,
            sell_tax_pct=2.0,
            holder_count=12_400,
            top10_holder_pct=38.0,
            lp_locked_pct=92.0,
            flags=["honeypot"] if honeypot else [],
        ),
        wash=WashTradingReport(evaluated=True, suspicion_score=12.0, trade_count=480),
        signals=signals,
    )


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import os

    from config.settings import get_settings

    parser = argparse.ArgumentParser(description="Run the dual-agent audit for one token.")
    parser.add_argument("--demo", action="store_true", help="use scripted agents (no API key)")
    parser.add_argument("--honeypot", action="store_true", help="demo snapshot with a honeypot flag")
    parser.add_argument("--address", default="", help="token address to audit (live mode)")
    parser.add_argument("--chain", default="ethereum", help="chain slug (live mode)")
    args = parser.parse_args(argv)

    if args.demo:
        os.environ["DEMO_MODE"] = "true"
    settings = get_settings()

    async def run() -> int:
        snapshot = _demo_snapshot(honeypot=args.honeypot)
        if args.address:
            snapshot = TokenSnapshot(token_address=args.address, chain=args.chain, symbol="LIVE")
        async with DualAuditFlow(settings) as flow:
            outcome = await flow.audit_detailed(snapshot)
        payload = {
            "status": outcome.verdict.status.value,
            "final_score": outcome.verdict.final_score,
            "prior_score": round(outcome.prior_score, 2),
            "alpha": outcome.alpha.to_dict(),
            "risk": outcome.risk.to_dict(),
            "rationale": outcome.verdict.rationale,
            "degraded": outcome.degraded,
            "errors": outcome.errors,
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(_main())
