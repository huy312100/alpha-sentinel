"""Cycle orchestration with scripted agents: no network, no LLM tokens spent."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from ai_engine.dual_audit_flow import AuditOutcome
from bot.alerts import AlertResult
from bot.engine import BOOT_STATE_KEY, PRODUCTIVE_RUN_KEY, UNPRODUCTIVE_SINCE_KEY, ScanEngine
from bot.watchlist import WatchToken
from core.models import VerdictStatus, iso, utcnow
from core.scoring import Decision
from scanners.pipeline import CollectedSnapshot
from storage.parquet_store import StorageError
from tests.conftest import OTHER_TOKEN, TOKEN


class FakePipeline:
    """Collector stand-in that never touches the network."""

    def __init__(self, *, missing=(), signals=None):
        self.missing = set(missing)
        self.signals = list(signals or [])
        self.calls: list[list[str]] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        await self.close()

    async def close(self):
        self.closed = True

    async def collect_many(self, snapshots):
        self.calls.append([snapshot.token_address for snapshot in snapshots])
        collected = []
        for snapshot in snapshots:
            if snapshot.token_address in self.missing:
                continue
            if self.signals:
                snapshot = replace(snapshot, signals=list(self.signals))
            collected.append(CollectedSnapshot(snapshot=snapshot, prior_score=72.0, duration_ms=5.0))
        return collected

    def health_report(self):
        return {"layers": {"dex": "ok"}}


class FakeAudit:
    """Deterministic two-agent stand-in returning pre-built verdicts."""

    def __init__(self, factory, *, failing=(), degraded=()):
        self.factory = factory
        self.failing = set(failing)
        self.degraded = set(degraded)
        self.seen: list[str] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        await self.close()

    async def close(self):
        self.closed = True

    async def audit_detailed(self, snapshot):
        self.seen.append(snapshot.token_address)
        if snapshot.token_address in self.failing:
            raise RuntimeError("risk agent exploded")
        verdict = self.factory(
            token_address=snapshot.token_address,
            chain=snapshot.chain,
            symbol=snapshot.symbol,
            decided_at=utcnow(),
        )
        decision = Decision(
            status=verdict.status,
            final_score=verdict.final_score,
            blended_score=verdict.blended_score,
            position_multiplier=verdict.position_multiplier,
            rationale=verdict.rationale,
            veto_reasons=["scripted veto"] if verdict.status is VerdictStatus.VETOED else [],
        )
        return AuditOutcome(
            verdict=verdict,
            decision=decision,
            alpha=verdict.alpha,
            risk=verdict.risk,
            prior_score=72.0,
            degraded=snapshot.token_address in self.degraded,
        )


class FakeAlerter:
    def __init__(self, *, sent=True, error: Exception | None = None):
        self.sent = sent
        self.error = error
        self.calls: list[tuple[str, list[str]]] = []
        self.closed = False

    async def send_verdict(self, verdict, *, reasons=None, already_alerted=None):
        self.calls.append((verdict.token_address, list(reasons or [])))
        if self.error is not None:
            raise self.error
        return AlertResult(sent=self.sent, message_id=1)

    async def close(self):
        self.closed = True


def make_engine(settings, store, *, pipeline=None, audit=None, alerter=None, factory=None, **kwargs):
    return ScanEngine(
        settings,
        store=store,
        pipeline=pipeline or FakePipeline(),
        audit=audit or FakeAudit(factory),
        alerter=alerter or FakeAlerter(),
        **kwargs,
    )


def token(address=TOKEN, chain="ethereum", symbol="TEST") -> WatchToken:
    return WatchToken(token_address=address, chain=chain, symbol=symbol)


class TestCycle:
    async def test_full_cycle_persists_and_alerts(self, settings, store, make_verdict, make_snapshot):
        pipeline = FakePipeline(signals=make_snapshot().signals)
        audit, alerter = FakeAudit(make_verdict), FakeAlerter()
        engine = make_engine(settings, store, pipeline=pipeline, audit=audit, alerter=alerter)
        await engine.start()
        try:
            report = await engine.run_cycle(tokens=[token()])
        finally:
            await engine.close()

        assert report.requested == 1
        assert report.collected == 1
        assert report.verdicts == 1
        assert report.approved == 1
        assert report.errors == 0
        assert report.alerts_sent == 1
        assert report.health == {"layers": {"dex": "ok"}}
        assert not report.dry_run

        assert pipeline.calls == [[TOKEN]]
        assert audit.seen == [TOKEN]
        assert alerter.calls == [(TOKEN, [])]
        assert alerter.closed is True

        # SQLite: verdict + seen marker + finished run.
        assert store.count_verdicts() == 1
        assert store.is_due(TOKEN, "ethereum", 6.0) is False
        run = store.latest_run()
        assert run["status"] == "completed"
        assert run["mode"] == "scan"
        assert run["verdicts_written"] == 1

        # Parquet mirror.
        assert set(engine.cold.list_datasets()) == {"verdicts", "layer_signals", "training_rows", "runs"}
        verdicts_frame = engine.cold.read_frame("verdicts")
        assert len(verdicts_frame) == 1
        assert verdicts_frame.iloc[0]["token_address"] == TOKEN
        assert verdicts_frame.iloc[0]["status"] == "approved"
        signals_frame = engine.cold.read_frame("layer_signals")
        assert len(signals_frame) == 3
        assert set(signals_frame["layer"]) == {"dex", "social", "derivatives"}

        # One training row per audit, carrying both features and labels.
        training_frame = engine.cold.read_frame("training_rows")
        assert len(training_frame) == 1
        row = training_frame.iloc[0]
        assert row["token_address"] == TOKEN
        assert 0.0 <= float(row["prior_score"]) <= 100.0
        assert row["alpha_source"] == "llm"
        assert row["final_status"] == "approved"
        assert "llm_alpha_score" in training_frame.columns
        assert "dex_score" in training_frame.columns

    async def test_dry_run_persists_nothing_but_reports(self, settings, store, make_verdict):
        alerter = FakeAlerter()
        engine = make_engine(settings, store, alerter=alerter, factory=make_verdict)
        await engine.start()
        try:
            report = await engine.run_cycle(tokens=[token()], dry_run=True)
        finally:
            await engine.close()

        assert report.dry_run is True
        assert report.approved == 1
        assert store.count_verdicts() == 0
        assert store.seen_count() == 0
        assert store.recent_alerts() == []
        assert alerter.calls == []
        assert engine.cold.list_datasets() == []
        assert store.latest_run() is None  # dry runs persist nothing at all

    async def test_alert_delivery_failure_does_not_abort_the_cycle(self, settings, store, make_verdict):
        alerter = FakeAlerter(error=RuntimeError("telegram exploded"))
        engine = make_engine(settings, store, alerter=alerter, factory=make_verdict)
        await engine.start()
        try:
            report = await engine.run_cycle(tokens=[token(), token(OTHER_TOKEN, symbol="OTHER")])
        finally:
            await engine.close()

        assert report.verdicts == 2
        assert report.errors == 2
        assert report.alerts_sent == 0
        # The run must be closed even when delivery raises.
        assert store.latest_run()["status"] == "partial"

    async def test_target_selection_failure_is_contained(self, settings, store):
        engine = make_engine(settings, store)

        class BrokenLoader:
            def load(self):
                raise RuntimeError("bad watchlist yaml")

        engine.loader = BrokenLoader()
        await engine.start()
        try:
            report = await engine.run_cycle()
        finally:
            await engine.close()

        assert report.verdicts == 0
        assert report.errors == 1
        assert store.latest_run()["status"] == "failed"

    async def test_one_bad_token_does_not_abort_the_cycle(self, settings, store, make_verdict):
        audit = FakeAudit(make_verdict, failing=[TOKEN])
        engine = make_engine(settings, store, audit=audit, factory=make_verdict)
        await engine.start()
        try:
            report = await engine.run_cycle(tokens=[token(), token(OTHER_TOKEN, symbol="OTHER")])
        finally:
            await engine.close()

        assert report.requested == 2
        assert report.errors == 1
        assert report.verdicts == 1
        assert report.approved == 1
        assert store.count_verdicts() == 1
        errors = [result for result in report.results if result.error]
        assert errors and "risk agent exploded" in errors[0].error
        assert store.latest_run()["status"] == "partial"

    async def test_collection_gap_is_reported(self, settings, store, make_verdict):
        pipeline = FakePipeline(missing=[TOKEN])
        engine = make_engine(settings, store, pipeline=pipeline, factory=make_verdict)
        await engine.start()
        try:
            report = await engine.run_cycle(tokens=[token()])
        finally:
            await engine.close()

        assert report.collected == 0
        assert report.errors == 1
        assert report.results[0].error == "collection_failed"
        assert store.count_verdicts() == 0

    async def test_degraded_audit_counts_as_error(self, settings, store, make_verdict):
        audit = FakeAudit(make_verdict, degraded=[TOKEN])
        engine = make_engine(settings, store, audit=audit, factory=make_verdict)
        await engine.start()
        try:
            report = await engine.run_cycle(tokens=[token()])
        finally:
            await engine.close()

        assert report.verdicts == 1
        assert report.errors == 1
        assert store.latest_run()["status"] == "partial"

    async def test_failed_persist_still_returns_a_verdict(self, settings, store, make_verdict, monkeypatch):
        engine = make_engine(settings, store, factory=make_verdict)

        def boom(verdict):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(store, "record_verdict", boom)
        await engine.start()
        try:
            report = await engine.run_cycle(tokens=[token()])
        finally:
            await engine.close()

        assert report.verdicts == 1
        assert report.errors == 0
        assert store.count_verdicts() == 0
        # The run row must not claim work that never landed: `verdicts_written` is the
        # column `--health` measures productivity against, and a verdict produced but
        # not persisted would keep a storage outage looking productive forever.
        assert report.persisted == 0
        assert report.persist_errors == 1
        run = store.latest_run()
        assert run["verdicts_written"] == 0
        assert run["status"] == "partial"
        # The durable productivity marker must not be stamped either: it is what
        # `--health` measures against, and a verdict that never landed is not
        # production no matter how the cycle finished.
        assert store.get_state(PRODUCTIVE_RUN_KEY) is None

    async def test_a_written_verdict_is_counted_as_productive(self, settings, store, make_verdict):
        """The healthy path must still record production, not just the failure path."""
        engine = make_engine(settings, store, factory=make_verdict)
        await engine.start()
        try:
            report = await engine.run_cycle(tokens=[token()])
        finally:
            await engine.close()

        assert report.persisted == report.verdicts == 1
        assert report.persist_errors == 0
        run = store.latest_run()
        assert run["verdicts_written"] == 1
        assert run["status"] == "completed"
        # Productivity is recorded durably as well as in the run row, because the run
        # table is pruned and the boot marker is rewritten by every restart.
        assert store.get_state(PRODUCTIVE_RUN_KEY) is not None
        assert store.get_state(UNPRODUCTIVE_SINCE_KEY) is None

    async def test_a_restart_keeps_the_silence_it_inherited(self, settings, store, make_verdict):
        """Starting again must not erase how long the bot has been producing nothing.

        The supervisor restarts precisely the bots that look unhealthy, so a start
        that reset the silence clock would keep extending the deadline it missed.
        """
        silence = iso(utcnow() - timedelta(hours=200))
        store.put_state(UNPRODUCTIVE_SINCE_KEY, silence)
        engine = make_engine(settings, store, factory=make_verdict)
        await engine.start()
        await engine.close()

        assert store.get_state(UNPRODUCTIVE_SINCE_KEY) == silence

    async def test_an_upgraded_store_does_not_get_a_fresh_deadline(self, settings, store, make_verdict):
        """Backfill the productive marker from history the engine can still see.

        A store upgraded into this marker would otherwise start counting silence from
        the upgrade, while the verdict history that proves it was productive ages out
        of `runs` entirely - and the deadline would then have nothing left to correct it.
        """
        run_id = store.start_run("scan")
        stamp = iso(utcnow() - timedelta(hours=200))
        store.finish_run(run_id, tokens_scanned=1, verdicts_written=1)
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE runs SET started_at = ?, finished_at = ? WHERE id = ?", (stamp, stamp, run_id))

        engine = make_engine(settings, store, factory=make_verdict)
        await engine.start()
        await engine.close()

        assert store.get_state(PRODUCTIVE_RUN_KEY) == stamp
        assert store.get_state(UNPRODUCTIVE_SINCE_KEY) is None

    async def test_a_marker_outrun_by_newer_history_is_reconciled(self, settings, store, make_verdict):
        """A process on older code produces verdicts without touching the marker.

        Left alone, the marker then measures a silence that ended hours ago and the
        deadline can alarm on a bot that is working, so boot has to prefer whichever
        source is newer rather than whichever was written last.
        """
        store.put_state(PRODUCTIVE_RUN_KEY, iso(utcnow() - timedelta(hours=200)))
        run_id = store.start_run("scan")
        fresh = iso(utcnow() - timedelta(hours=2))
        store.finish_run(run_id, tokens_scanned=1, verdicts_written=1)
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE runs SET started_at = ?, finished_at = ? WHERE id = ?", (fresh, fresh, run_id))

        engine = make_engine(settings, store, factory=make_verdict)
        await engine.start()
        await engine.close()

        assert store.get_state(PRODUCTIVE_RUN_KEY) == fresh

    async def test_a_dry_run_leaves_no_evidence_of_work(self, settings, store, make_verdict):
        """A dry run must not write the productivity marker, even during startup.

        The spend ledger already refuses to persist for a dry run, for the same reason:
        what the cycle "accomplished" was never stored anywhere, so recording it as
        production would be evidence invented by a rehearsal. The boot marker is the
        exception - it states when a process ran, not what it achieved - and without it
        a fresh volume whose first process is a dry run would fail its own healthcheck.
        """
        stale = iso(utcnow() - timedelta(hours=200))
        store.put_state(PRODUCTIVE_RUN_KEY, stale)
        run_id = store.start_run("scan")
        fresh = iso(utcnow() - timedelta(hours=2))
        store.finish_run(run_id, tokens_scanned=1, verdicts_written=1)
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE runs SET started_at = ?, finished_at = ? WHERE id = ?", (fresh, fresh, run_id))

        engine = make_engine(settings, store, factory=make_verdict, dry_run=True)
        await engine.start()
        await engine.close()

        assert store.get_state(PRODUCTIVE_RUN_KEY) == stale  # untouched by a rehearsal
        assert store.get_state(BOOT_STATE_KEY) is not None   # liveness is still recorded
        assert store.latest_run()["verdicts_written"] == 1   # the real history is unchanged

    async def test_a_fresher_marker_is_not_dragged_back_to_old_run_history(self, settings, store, make_verdict):
        """The other direction: run rows pruned away must not shorten the marker.

        Without both directions this is just a backfill, and the durable marker exists
        precisely because the run table stops being the whole record.
        """
        fresh = iso(utcnow() - timedelta(hours=1))
        store.put_state(PRODUCTIVE_RUN_KEY, fresh)
        run_id = store.start_run("scan")
        old = iso(utcnow() - timedelta(hours=300))
        store.finish_run(run_id, tokens_scanned=1, verdicts_written=1)
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE runs SET started_at = ?, finished_at = ? WHERE id = ?", (old, old, run_id))

        engine = make_engine(settings, store, factory=make_verdict)
        await engine.start()
        await engine.close()

        assert store.get_state(PRODUCTIVE_RUN_KEY) == fresh

    async def test_cold_store_failure_is_swallowed(self, settings, store, make_verdict, monkeypatch):
        engine = make_engine(settings, store, factory=make_verdict)

        def boom(*args, **kwargs):
            raise StorageError("parquet is read-only")

        monkeypatch.setattr(engine.cold, "write_frame", boom)
        await engine.start()
        try:
            report = await engine.run_cycle(tokens=[token()])
        finally:
            await engine.close()

        assert report.verdicts == 1
        assert store.count_verdicts() == 1

    async def test_cycle_requires_start(self, settings, store, make_verdict):
        engine = make_engine(settings, store, factory=make_verdict)
        with pytest.raises(RuntimeError):
            await engine.run_cycle(tokens=[token()])

    async def test_vetoed_verdict_is_alerted_with_reasons(self, settings, store, make_verdict):
        vetoed = make_verdict(status=VerdictStatus.VETOED, final_score=12.0)
        alerter = FakeAlerter()
        engine = make_engine(settings, store, alerter=alerter, factory=lambda **kw: vetoed)
        await engine.start()
        try:
            report = await engine.run_cycle(tokens=[token()])
        finally:
            await engine.close()

        assert report.vetoed == 1
        assert alerter.calls == [(TOKEN, ["scripted veto"])]


class TestTargetSelection:
    async def test_cooldown_skips_recently_scanned_tokens(self, settings, store, make_verdict, watchlist_file):
        pipeline = FakePipeline()
        engine = make_engine(settings, store, pipeline=pipeline, factory=make_verdict)
        await engine.start()
        try:
            first = await engine.run_cycle(limit=1)
            assert first.collected == 1
            second = await engine.run_cycle(limit=1)
        finally:
            await engine.close()

        assert first.skipped == 0
        assert second.requested == 1
        assert second.skipped == 1
        assert second.collected == 0
        assert second.results[0].skip_reason == "cooldown"
        assert second.results[0].skipped is True
        assert len(pipeline.calls) == 1

    async def test_a_cycle_where_everything_is_cooling_down_is_completed(
        self, settings, store, make_verdict, watchlist_file
    ):
        """`partial` has to mean something, and the schedule is not it.

        With a 6-hour cooldown and a 5-minute interval, nearly every cycle of a
        healthy daemon is this one: nothing due, nothing collected, nothing wrong.
        Counting those skips as a deficiency made `partial` the permanent status of
        a working bot, so the runs where a provider genuinely failed were
        indistinguishable from the runs where nothing was scheduled.
        """
        engine = make_engine(settings, store, factory=make_verdict)
        await engine.start()
        try:
            await engine.run_cycle(limit=1)
            cooling = await engine.run_cycle(limit=1)
        finally:
            await engine.close()

        assert cooling.skipped == 1
        assert cooling.errors == 0
        # Read this cycle's own run row rather than `latest_run()`: run stamps are
        # second-precision, so two cycles in the same second tie and the ordering
        # says nothing about which one you meant.
        run = next(row for row in store.recent_runs(limit=5) if row["id"] == cooling.run_id)
        assert run["status"] == "completed"
        assert run["errors"] == 0

    async def test_force_bypasses_cooldown(self, settings, store, make_verdict, watchlist_file):
        pipeline = FakePipeline()
        engine = make_engine(settings, store, pipeline=pipeline, factory=make_verdict)
        await engine.start()
        try:
            await engine.run_cycle(limit=1)
            forced = await engine.run_cycle(limit=1, force=True)
        finally:
            await engine.close()

        assert forced.skipped == 0
        assert forced.collected == 1
        assert len(pipeline.calls) == 2

    async def test_watchlist_selection_respects_named_lists(self, settings, store, make_verdict, watchlist_file):
        engine = make_engine(settings, store, factory=make_verdict)
        await engine.start()
        try:
            report = await engine.run_cycle(watchlists=["degen"], force=True)
        finally:
            await engine.close()

        assert report.requested == 1
        assert report.results[0].token.symbol == "WSOL"
        assert report.results[0].token.chain == "solana"
