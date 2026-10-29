"""Spend ceilings: the client refuses, the engine reports, nothing is faked.

The contract these tests pin down:

* the ceiling is enforced at the socket - inside the concurrency semaphore, so a
  request that queued behind others sees the spend of everything that finished
  while it waited;
* today's total survives a restart because it lives in the hot store, keyed by
  UTC day, and a dry run never writes it;
* a budget stop never becomes a verdict. The fail-closed risk path would file a
  CRITICAL veto and page the operator for a ceiling they chose themselves.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from ai_engine.dual_audit_flow import DualAuditFlow
from ai_engine.llm_client import LLMBudgetExceeded, LLMClient, LLMError
from bot.engine import ScanEngine, day_spend_key
from config.settings import Settings
from tests.conftest import OTHER_TOKEN, SOL_TOKEN, TOKEN
from tests.test_dual_audit_flow import ScriptedClient
from tests.test_engine import FakeAlerter, FakeAudit, FakePipeline, token
from tests.test_llm_client import FakeSession, install, make_settings, ok_body


def with_budget(settings: Settings, *, cycle: float = 0.0, day: float = 0.0) -> Settings:
    """Copy of the fixture settings with the ceilings applied."""
    return replace(
        settings,
        llm=replace(settings.llm, max_usd_per_cycle=cycle, max_usd_per_day=day),
    )


class MeteredAudit(FakeAudit):
    """``FakeAudit`` that also exposes the spend meter a real flow exposes."""

    def __init__(self, factory, *, per_token: float = 0.25, stop_at: str | None = None):
        super().__init__(factory)
        self.per_token = per_token
        self.stop_at = stop_at
        self._cycle = 0.0
        self.seeded: float | None = None

    def start_cycle(self, *, day_spend_usd: float = 0.0) -> None:
        self._cycle = 0.0
        self.seeded = day_spend_usd

    @property
    def cycle_spend_usd(self) -> float:
        return round(self._cycle, 6)

    async def audit_detailed(self, snapshot):
        if snapshot.token_address == self.stop_at:
            raise LLMBudgetExceeded(scope="cycle", spent_usd=0.5, limit_usd=0.5)
        outcome = await super().audit_detailed(snapshot)
        self._cycle += self.per_token
        return outcome


# -- the client is the authority ------------------------------------------------


async def test_a_cycle_ceiling_refuses_the_next_request(monkeypatch):
    requests = install(monkeypatch, [ok_body()])
    client = LLMClient(make_settings(max_usd_per_cycle=1.0), session=FakeSession())
    client.start_cycle()

    await client.chat_json(system="s", user="u")  # costs 2.0, above the 1.0 cap
    with pytest.raises(LLMBudgetExceeded) as raised:
        await client.chat_json(system="s", user="u")

    assert len(requests) == 1, "the second request must never reach the provider"
    assert raised.value.scope == "cycle"
    assert raised.value.limit_usd == 1.0
    assert client.cycle_spend_usd == pytest.approx(2.0)


async def test_a_restart_does_not_hand_the_daemon_a_fresh_daily_allowance(monkeypatch):
    requests = install(monkeypatch, [])  # any send would raise IndexError
    client = LLMClient(make_settings(max_usd_per_day=5.0), session=FakeSession())
    client.start_cycle(day_spend_usd=5.0)

    with pytest.raises(LLMBudgetExceeded) as raised:
        await client.chat_json(system="s", user="u")

    assert raised.value.scope == "day"
    assert requests == []


async def test_zero_means_no_ceiling(monkeypatch):
    install(monkeypatch, [ok_body(), ok_body()])
    client = LLMClient(make_settings(max_usd_per_cycle=0.0, max_usd_per_day=0.0), session=FakeSession())
    client.start_cycle(day_spend_usd=1_000.0)

    await client.chat_json(system="s", user="u")
    await client.chat_json(system="s", user="u")

    assert client.cycle_spend_usd == pytest.approx(4.0)


async def test_start_cycle_resets_the_cycle_meter_but_keeps_the_day(monkeypatch):
    install(monkeypatch, [ok_body()])
    client = LLMClient(make_settings(), session=FakeSession())
    client.start_cycle()

    await client.chat_json(system="s", user="u")
    client.start_cycle(day_spend_usd=3.0)

    assert client.cycle_spend_usd == 0.0
    assert client.day_spend_usd == pytest.approx(3.0)


async def test_a_billed_but_unusable_response_still_counts(monkeypatch):
    """A gateway error envelope is billed; a ceiling that ignored it could be walked around."""
    body = {"choices": [], "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000}}
    install(monkeypatch, [body])
    client = LLMClient(make_settings(), session=FakeSession())
    client.start_cycle()

    with pytest.raises(LLMError):
        await client.chat_json(system="s", user="u")

    assert client.cycle_spend_usd == pytest.approx(1.0)


async def test_a_response_without_usage_is_flagged(monkeypatch):
    """No usage block means the ledger cannot see that call: say so instead of pretending."""
    body = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    install(monkeypatch, [body])
    client = LLMClient(make_settings(), session=FakeSession())
    client.start_cycle()

    await client.chat_json(system="s", user="u")

    assert client.stats["usage_missing"] == 1
    assert client.stats["cost_usd"] == 0.0
    assert client.stats["requests"] == 1


# -- settings refuse a ceiling that could never bind ----------------------------


def test_a_ceiling_without_prices_is_a_boot_problem(settings):
    capped = replace(
        settings,
        llm=replace(
            settings.llm,
            input_usd_per_mtok=0.0,
            output_usd_per_mtok=0.0,
            max_usd_per_cycle=1.0,
        ),
    )

    problems = capped.validate()

    assert any("spend cannot be measured" in problem for problem in problems)


def test_the_ceilings_are_parsed_from_the_environment():
    loaded = Settings.load({"LLM_MAX_USD_PER_CYCLE": "0.5", "LLM_MAX_USD_PER_DAY": "5"})
    assert loaded.llm.max_usd_per_cycle == pytest.approx(0.5)
    assert loaded.llm.max_usd_per_day == pytest.approx(5.0)


# -- the audit flow re-raises instead of inventing a verdict --------------------


async def test_the_analyst_stop_propagates(settings, make_snapshot):
    class StoppingClient(ScriptedClient):
        async def chat_json(self, **kwargs):
            raise LLMBudgetExceeded(scope="day", spent_usd=5.0, limit_usd=5.0)

    flow = DualAuditFlow(settings, client=StoppingClient(alpha="{}", risk=["{}"]))
    try:
        with pytest.raises(LLMBudgetExceeded):
            await flow.audit_detailed(make_snapshot())
    finally:
        await flow.close()


async def test_the_auditor_stop_does_not_fabricate_a_critical_veto(settings, make_snapshot):
    class StoppingClient(ScriptedClient):
        async def chat_json(
            self, *, model=None, system, user, temperature=0.2, max_tokens=None, json_mode=True
        ):
            if "SENTINEL-RISK" in system:
                raise LLMBudgetExceeded(scope="cycle", spent_usd=1.0, limit_usd=1.0)
            return await super().chat_json(
                model=model,
                system=system,
                user=user,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )

    client = StoppingClient(
        alpha='{"alpha_score": 80, "confidence": 0.8, "thesis": "scripted"}',
        risk=['{"risk_score": 18, "veto": false, "severity": "low"}'],
    )
    flow = DualAuditFlow(settings, client=client)
    try:
        with pytest.raises(LLMBudgetExceeded):
            await flow.audit_detailed(make_snapshot())
    finally:
        await flow.close()


# -- the engine reports the stop and stops spending -----------------------------


async def test_a_ceiling_stop_reports_the_queue_instead_of_filing_vetoes(settings, store, make_verdict):
    targets = [
        token(TOKEN),
        token(OTHER_TOKEN, symbol="OTHER"),
        token(SOL_TOKEN, chain="solana", symbol="WSOL"),
    ]
    audit = MeteredAudit(make_verdict, stop_at=OTHER_TOKEN)
    alerter = FakeAlerter()
    engine = ScanEngine(settings, store=store, pipeline=FakePipeline(), audit=audit, alerter=alerter)
    await engine.start()
    try:
        report = await engine.run_cycle(tokens=targets)
    finally:
        await engine.close()

    assert report.collected == 3
    assert report.approved == 1
    assert report.vetoed == 0, "a ceiling is an operator decision, not a risk finding"
    assert report.errors == 2, "the stopped token plus the one queued behind it"
    assert alerter.calls == [(TOKEN, [])], "no alert may fire for the stop"
    stopped = [result for result in report.results if result.error]
    assert [result.token.token_address for result in stopped] == [OTHER_TOKEN, SOL_TOKEN]
    assert all("llm_budget_exhausted" in (result.error or "") for result in stopped)
    assert store.count_verdicts() == 1
    assert store.latest_run()["status"] == "partial"


async def test_the_day_ledger_survives_a_restart(settings, store, make_verdict):
    audit = MeteredAudit(make_verdict, per_token=0.25)
    engine = ScanEngine(settings, store=store, pipeline=FakePipeline(), audit=audit, alerter=FakeAlerter())
    targets = [token(TOKEN), token(OTHER_TOKEN, symbol="OTHER")]
    await engine.start()
    try:
        first = await engine.run_cycle(tokens=targets)
        assert audit.seeded == 0.0
        assert first.llm_spend_usd == pytest.approx(0.5)
        assert store.get_state(day_spend_key()) == pytest.approx(0.5)

        second = await engine.run_cycle(tokens=targets)
    finally:
        await engine.close()

    assert audit.seeded == pytest.approx(0.5), "the next cycle must start from today's bill"
    assert second.llm_spend_usd == pytest.approx(0.5)
    assert store.get_state(day_spend_key()) == pytest.approx(1.0)


async def test_a_dry_run_reports_spend_but_writes_no_ledger(settings, store, make_verdict):
    audit = MeteredAudit(make_verdict, per_token=0.1)
    engine = ScanEngine(settings, store=store, pipeline=FakePipeline(), audit=audit, alerter=FakeAlerter())
    await engine.start()
    try:
        report = await engine.run_cycle(tokens=[token()], dry_run=True)
    finally:
        await engine.close()

    assert report.llm_spend_usd == pytest.approx(0.1)
    assert store.get_state(day_spend_key()) is None, "dry runs persist nothing, ledger included"


async def test_a_spent_day_stops_before_collecting(settings, store, make_verdict):
    capped = with_budget(settings, day=1.0)
    store.put_state(day_spend_key(), 1.25)
    pipeline, audit, alerter = FakePipeline(), FakeAudit(make_verdict), FakeAlerter()
    engine = ScanEngine(capped, store=store, pipeline=pipeline, audit=audit, alerter=alerter)
    await engine.start()
    try:
        report = await engine.run_cycle(tokens=[token(TOKEN), token(OTHER_TOKEN, symbol="OTHER")])
    finally:
        await engine.close()

    assert pipeline.calls == [], "collecting for a refused audit would burn scanner time"
    assert audit.seen == []
    assert report.collected == 0
    assert report.errors == 2
    assert report.verdicts == 0
    assert store.count_verdicts() == 0
    assert store.latest_run()["status"] == "failed"


def test_an_unreadable_ledger_fails_open(settings, store, make_verdict, monkeypatch):
    engine = ScanEngine(
        settings, store=store, pipeline=FakePipeline(), audit=FakeAudit(make_verdict), alerter=FakeAlerter()
    )

    def locked(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "get_state", locked)

    # The per-cycle ceiling inside the client still binds, so the worst case is a
    # bounded overspend - not a bot that refuses to work over an unreadable counter.
    assert engine._day_spend_usd() == 0.0
