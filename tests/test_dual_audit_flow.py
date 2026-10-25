"""Dual-agent flow: fail-closed parsing, a retry-sized watchdog and isolation."""

from __future__ import annotations

from dataclasses import replace

from ai_engine.dual_audit_flow import DualAuditFlow
from ai_engine.llm_client import ChatResult
from core.models import TokenSnapshot


class ScriptedClient:
    """Two-agent stand-in that answers with queued strings (no network)."""

    def __init__(self, *, alpha: str, risk: list[str]) -> None:
        self.alpha = alpha
        self.risk = list(risk)
        self.risk_calls = 0

    async def chat_json(
        self,
        *,
        model: str | None = None,
        system: str,
        user: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        json_mode: bool = True,
    ) -> ChatResult:
        if "SENTINEL-RISK" in system:
            self.risk_calls += 1
            content = self.risk.pop(0) if len(self.risk) > 1 else self.risk[0]
        else:
            content = self.alpha
        return ChatResult(content=content, model="scripted")


def snapshot(symbol: str = "GOOD") -> TokenSnapshot:
    return TokenSnapshot(
        token_address="0x" + "ab" * 20,
        chain="ethereum",
        asset_class="crypto",
        symbol=symbol,
    )


async def test_missing_risk_score_triggers_a_repair_and_then_fails_closed(settings):
    client = ScriptedClient(
        alpha='{"alpha_score": 80, "confidence": 0.8, "thesis": "scripted"}',
        risk=['{"note": "no score here"}'],
    )
    flow = DualAuditFlow(settings, client=client)

    outcome = await flow.audit_detailed(snapshot())

    assert client.risk_calls == 2  # first answer + one repair attempt
    assert outcome.risk.veto is True
    assert outcome.risk.model == "unavailable"
    assert any("risk agent failed" in error for error in outcome.errors)
    assert any("requesting a repair" in error for error in outcome.errors)


async def test_a_repaired_risk_payload_is_accepted(settings):
    client = ScriptedClient(
        alpha='{"alpha_score": 80, "confidence": 0.8, "thesis": "scripted"}',
        risk=['{"note": "no score here"}', '{"risk_score": 18, "veto": false, "severity": "low"}'],
    )
    flow = DualAuditFlow(settings, client=client)

    outcome = await flow.audit_detailed(snapshot())

    assert client.risk_calls == 2
    assert outcome.risk.risk_score == 18.0
    assert outcome.risk.veto is False


async def test_audit_many_isolates_one_failed_snapshot(settings):
    class Flaky(DualAuditFlow):
        async def audit_detailed(self, token: TokenSnapshot):
            if token.symbol == "BAD":
                raise RuntimeError("scanner exploded")
            return await super().audit_detailed(token)

    client = ScriptedClient(
        alpha='{"alpha_score": 80, "confidence": 0.8, "thesis": "scripted"}',
        risk=['{"risk_score": 18, "veto": false, "severity": "low"}'],
    )
    flow = Flaky(settings, client=client)

    outcomes = await flow.audit_many([snapshot("GOOD"), snapshot("BAD")], concurrency=2)

    assert [outcome.verdict.symbol for outcome in outcomes] == ["GOOD"]


def test_watchdog_covers_the_retry_budget(settings):
    llm = replace(settings.llm, timeout_seconds=10.0, max_retries=3)
    flow = DualAuditFlow(replace(settings, llm=llm), client=ScriptedClient(alpha="{}", risk=["{}"]))
    assert flow.watchdog_seconds == 45.0
