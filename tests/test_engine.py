"""Cycle orchestration with scripted agents: no network, no LLM tokens spent."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from ai_engine.dual_audit_flow import AuditOutcome
from bot.alerts import AlertResult
from bot.engine import (
    BOOT_STATE_KEY,
    LAST_BACKUP_KEY,
    PRODUCTIVE_RUN_KEY,
    UNPRODUCTIVE_SINCE_KEY,
    ScanEngine,
)
from bot.watchlist import WatchToken
from core.models import VerdictStatus, iso, utcnow
from core.scoring import Decision
from scanners.pipeline import CollectedSnapshot
from storage import backup as hot_backup
from storage.backup import UploadOutcome
from storage.hot_store import SCHEMA_VERSION
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
        production would be evidence invented by a rehearsal. The boot marker goes the
        same way, and for a sharper reason - it grants the "booting" grace in `--health`,
        so refreshing it on a live store hides a daemon whose cycles are all stale.
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
        assert store.get_state(BOOT_STATE_KEY) is None       # and neither is the boot stamp
        assert store.latest_run()["verdicts_written"] == 1   # the real history is unchanged

    async def test_an_engine_built_as_a_rehearsal_persists_nothing(self, settings, store, make_verdict):
        """`dry_run` is one intent, not two flags a caller has to remember.

        `run_forever` invokes `run_cycle` without naming `dry_run`, so an engine built
        as a rehearsal would otherwise write verdicts, run rows and cold partitions for
        every cycle of itself - the flag would only hold for callers that repeated it.
        """
        engine = make_engine(settings, store, factory=make_verdict, dry_run=True)
        await engine.start()
        try:
            report = await engine.run_cycle(tokens=[token()])
        finally:
            await engine.close()

        assert report.dry_run is True
        assert report.verdicts == 1               # the work still happens
        assert store.count_verdicts() == 0        # and is stored nowhere
        assert store.latest_run() is None
        assert store.get_state(PRODUCTIVE_RUN_KEY) is None
        assert store.get_state(BOOT_STATE_KEY) is None

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


class TestScheduledBackups:
    """Maintenance takes the snapshot that makes a rollback more than a hope.

    `make backup` was the only path to a copy, so the newest snapshot was a function of
    someone remembering to run it. The two tables with no second copy anywhere else -
    the scan schedule in `seen_tokens` and the spend ledger in `kv` - were unprotected
    for exactly as long as nobody remembered, and the one on disk here was two days old
    when this was found.
    """

    @staticmethod
    def _snapshots(settings) -> list[Path]:
        directory = Path(hot_backup.default_backup_dir(settings.storage.data_dir))
        return sorted(directory.glob("*.db"))

    def test_maintenance_snapshots_the_hot_store(self, settings, store):
        engine = make_engine(settings, store)

        result = engine.maintenance(force=True)

        assert "backup_error" not in result
        assert len(self._snapshots(settings)) == 1
        assert result["backup"]["schema_version"] == SCHEMA_VERSION
        assert store.get_state(LAST_BACKUP_KEY) > 0

    def test_the_cadence_is_honoured_within_one_process(self, settings, store):
        engine = make_engine(settings, store)

        engine.maintenance(force=True)
        second = engine.maintenance(force=True)

        assert "backup" not in second  # `force` means maintenance, not "snapshot again"
        assert len(self._snapshots(settings)) == 1

    def test_the_deadline_survives_a_restart(self, settings, store):
        """A restarted daemon does not get to reset the backup clock.

        The supervisor restarts exactly the services that are misbehaving, so a cadence
        held in memory would be postponed by every restart - and an unhealthy loop could
        keep running without ever producing a second copy.
        """
        make_engine(settings, store).maintenance(force=True)
        marker = store.get_state(LAST_BACKUP_KEY)

        result = make_engine(settings, store).maintenance(force=True)

        assert "backup" not in result
        assert store.get_state(LAST_BACKUP_KEY) == marker

    def test_an_expired_deadline_is_taken_again(self, settings, store):
        engine = make_engine(settings, store)
        engine.maintenance(force=True)
        store.put_state(LAST_BACKUP_KEY, time.time() - 99_999.0)

        result = engine.maintenance(force=True)

        assert "backup" in result
        assert store.get_state(LAST_BACKUP_KEY) > 0

    def test_the_cadence_can_be_switched_off(self, settings, store):
        """`0` leaves snapshots to cron, and takes none itself."""
        offline = replace(settings, storage=replace(settings.storage, backup_interval_seconds=0))

        result = make_engine(offline, store).maintenance(force=True)

        assert "backup" not in result
        assert self._snapshots(offline) == []

    def test_a_dry_run_takes_no_snapshot_and_moves_no_deadline(self, settings, store):
        """`--maintenance --dry-run` is a rehearsal, and a rehearsal persists nothing.

        Without this guard a dry run would copy the store, offer it to R2, and advance
        the backup deadline - after which the real schedule waits a full interval for a
        snapshot that was never meant to count.
        """
        engine = make_engine(settings, store, dry_run=True)

        result = engine.maintenance(force=True)

        assert "backup" not in result
        assert self._snapshots(settings) == []
        assert store.get_state(LAST_BACKUP_KEY, 0.0) == 0.0

    def test_a_failed_upload_is_reported_without_losing_the_snapshot(self, settings, store, monkeypatch):
        """The local copy is the product; the off-site mirror still has to be honest.

        `upload` used to return ``None`` for both "not configured" and "failed", so a
        daemon with broken credentials looked exactly like one that never had R2.
        """
        monkeypatch.setattr(
            hot_backup, "upload", lambda taken, r2, **kw: UploadOutcome(attempted=True, error="boom")
        )
        engine = make_engine(settings, store)

        result = engine.maintenance(force=True)

        assert "backup" in result and result["backup"]["remote_key"] is None
        assert "upload: boom" in result["backup_error"]
        assert len(self._snapshots(settings)) == 1  # the local copy stands
        assert store.get_state(LAST_BACKUP_KEY) > 0  # and the deadline advanced

    def test_retention_that_cannot_delete_is_reported(self, settings, store, monkeypatch):
        """`prune` skips a file it cannot delete, so its return value cannot be trusted.

        Disk creep on a machine sized around free space is the failure this whole
        feature is meant to avoid, so the folder is counted rather than believed.
        """
        stale = Path(hot_backup.default_backup_dir(settings.storage.data_dir)) / "alpha_sentinel-20260101T000000Z.db"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"x")
        monkeypatch.setattr(hot_backup, "prune", lambda directory, *, keep: [])
        tight = replace(settings, storage=replace(settings.storage, backup_keep=1))

        result = make_engine(tight, store).maintenance(force=True)

        assert "retention:" in result["backup_error"]
        assert result["backup"]["pruned"] == 0

    def test_a_kv_failure_after_a_good_snapshot_does_not_break_maintenance(self, settings, store, monkeypatch):
        """The snapshot is already safe on disk; a counter must not undo that.

        Writing the deadline is the last step, and a locked `kv` row there would
        otherwise escape the method and abort maintenance - losing the report of work
        that genuinely happened. The deadline is deliberately left unadvanced so the
        next pass retries, rather than trusting a snapshot nobody recorded.
        """
        engine = make_engine(settings, store)

        def refuse(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(store, "put_state", refuse)

        result = engine.maintenance(force=True)

        assert "backup" not in result
        assert "database is locked" in result["backup_error"]
        assert len(self._snapshots(settings)) == 1  # the copy still exists
        assert result["stale_runs"] == 0  # and maintenance still reported normally
        # The deadline is deliberately left where it was, so the next pass retries
        # instead of trusting a snapshot that nothing recorded.
        assert store.get_state(LAST_BACKUP_KEY, 0.0) == 0.0

    def test_a_kv_read_failure_is_reported_not_raised(self, settings, store, monkeypatch):
        """A locked counter skips this pass; it does not swallow the maintenance report.

        The read of the backup deadline sits at the top of the method, outside the
        block that guards the write, so without its own guard a SQLite error here
        would propagate and lose `stale_runs` / `pruned` for a pass that had already
        done that work. The deadline is left unadvanced, so the next pass retries.
        """
        engine = make_engine(settings, store)

        def refuse(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(store, "get_state", refuse)

        result = engine.maintenance(force=True)

        assert "state read:" in result["backup_error"]
        assert "backup" not in result
        assert self._snapshots(settings) == []
        assert result["stale_runs"] == 0

    def test_upload_and_retention_failures_are_reported_together(self, settings, store, monkeypatch, caplog):
        """Two problems, two reports - and both loud enough to grep for.

        An `elif` let a recurring upload failure hide unbounded snapshot growth, which
        is the faster failure on a machine whose free space sized this design. And a
        failure carried only inside `maintenance_finished` at INFO sits among the
        routine counters, where nobody looks for it.
        """
        stale = Path(hot_backup.default_backup_dir(settings.storage.data_dir)) / "alpha_sentinel-20260101T000000Z.db"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"x")
        monkeypatch.setattr(hot_backup, "upload", lambda taken, r2, **kw: UploadOutcome(attempted=True, error="boom"))
        monkeypatch.setattr(hot_backup, "prune", lambda directory, *, keep: [])
        tight = replace(settings, storage=replace(settings.storage, backup_keep=1))

        with caplog.at_level("WARNING", logger="bot.engine"):
            result = make_engine(tight, store).maintenance(force=True)

        assert "upload: boom" in result["backup_error"]
        assert "retention:" in result["backup_error"]
        warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
        assert "backup_incomplete" in warnings, warnings

    def test_an_existing_remote_key_is_not_claimed_as_this_run_upload(self, settings, store, monkeypatch):
        """The key exists; whether it holds these bytes is a different question.

        Snapshot names are second-precise, so a manual and a scheduled backup can collide
        on one key. The maintenance result must say `exists` and must not hand the
        operator a `remote_key` that implies this run's copy is off-site.
        """
        monkeypatch.setattr(
            hot_backup, "upload", lambda taken, r2, **kw: UploadOutcome(attempted=True, key="pre/key", skipped="exists")
        )

        result = make_engine(settings, store).maintenance(force=True)

        assert result["backup"]["remote"] == "exists"
        assert result["backup"]["remote_key"] is None
        assert "backup_error" not in result  # not a failure, just not this run's upload

    def test_a_failed_snapshot_does_not_break_maintenance(self, settings, store, monkeypatch):
        """Unprotected is not the same as broken.

        Maintenance prunes and trims the lake before it snapshots; refusing to report
        that work, or stopping the scanner, would turn a backup problem into an outage -
        the trade this project has already refused for provider failures.
        """

        def boom(*args, **kwargs):
            raise hot_backup.BackupError("disk full")

        monkeypatch.setattr(hot_backup, "snapshot", boom)
        engine = make_engine(settings, store)

        result = engine.maintenance(force=True)

        assert "backup_error" in result
        assert result["stale_runs"] == 0
        assert "pruned" in result
        assert self._snapshots(settings) == []
        assert store.get_state(LAST_BACKUP_KEY, 0.0) == 0.0  # deadline not advanced
