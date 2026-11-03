"""Cycle-freshness check: the question a container healthcheck has to ask.

``--status`` can always answer, so a healthcheck built on it can never fail. These
tests pin the other contract: a loop that stopped, or a cycle that hung, must be
reported unhealthy - while a fresh install, a daemon that just restarted and a
locked database must not flap the container.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

import bot.main as bot_main
from bot.engine import BOOT_STATE_KEY, ScanEngine
from bot.health import MIN_STALE_SECONDS, evaluate, stale_window_seconds
from core.models import iso, utcnow
from storage.hot_store import HotStore
from tests.conftest import clear_inherited_settings
from tests.test_engine import FakeAlerter, FakeAudit, FakePipeline


def finish_one_cycle(store: HotStore, status: str = "completed", errors: int = 0) -> None:
    run_id = store.start_run("scan")
    store.finish_run(run_id, status=status, tokens_scanned=1, verdicts_written=1, approved=1, errors=errors)


def backdate(db_path: Path, *, hours: float) -> None:
    stamp = iso(utcnow() - timedelta(hours=hours))
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE runs SET started_at = ?, finished_at = ?", (stamp, stamp))


# -- the window -----------------------------------------------------------------


def test_the_window_follows_the_scan_interval(settings):
    # 300s * 3 is below the floor, so the floor wins.
    assert stale_window_seconds(settings) == MIN_STALE_SECONDS

    slow = replace(settings, scanners=replace(settings.scanners, scan_interval_seconds=3_600))
    assert stale_window_seconds(slow) == 10_800


def test_an_explicit_window_wins(settings):
    pinned = replace(settings, scanners=replace(settings.scanners, health_max_stale_seconds=90))
    assert stale_window_seconds(pinned) == 90


# -- verdicts -------------------------------------------------------------------


def test_a_fresh_install_is_healthy(store, settings):
    report = evaluate(store, settings)

    assert report.healthy is True
    assert report.reason == "no_runs_yet"


def test_a_finished_cycle_inside_the_window_is_healthy(store, settings):
    finish_one_cycle(store)

    report = evaluate(store, settings)

    assert report.healthy is True
    assert report.reason == "cycle_fresh"
    assert report.status == "completed"
    assert report.age_seconds is not None and report.age_seconds < 60


def test_a_silent_loop_is_unhealthy(store, settings):
    finish_one_cycle(store)
    window = stale_window_seconds(settings)

    report = evaluate(store, settings, now=utcnow() + timedelta(seconds=window + 60))

    assert report.healthy is False
    assert report.reason == "cycle_stale"
    assert report.age_seconds is not None and report.age_seconds > window


def test_a_restarted_daemon_is_not_called_stalled(store, settings):
    finish_one_cycle(store)
    moment = utcnow() + timedelta(seconds=stale_window_seconds(settings) + 60)
    store.put_state(BOOT_STATE_KEY, iso(moment - timedelta(seconds=10)))

    report = evaluate(store, settings, now=moment)

    assert report.healthy is True
    assert report.reason == "booting"


def test_a_cycle_that_never_finishes_is_unhealthy(store, settings):
    store.start_run("scan")
    window = stale_window_seconds(settings)

    report = evaluate(store, settings, now=utcnow() + timedelta(seconds=window + 60))

    assert report.healthy is False
    assert report.reason == "cycle_hung"


def test_a_slow_cycle_still_inside_the_window_is_healthy(store, settings):
    store.start_run("scan")

    report = evaluate(store, settings)

    assert report.healthy is True
    assert report.reason == "cycle_running"


def test_a_daemon_that_never_finished_its_first_cycle_is_unhealthy(store, settings):
    window = stale_window_seconds(settings)
    store.put_state(BOOT_STATE_KEY, iso(utcnow() - timedelta(seconds=window + 60)))

    report = evaluate(store, settings)

    assert report.healthy is False
    assert report.reason == "no_cycle_finished_since_boot"


def test_an_unreadable_timestamp_does_not_flap(store, settings, monkeypatch):
    monkeypatch.setattr(
        store,
        "latest_run",
        lambda: {"id": "x", "status": "completed", "started_at": "not-a-date", "finished_at": None},
    )

    report = evaluate(store, settings)

    assert report.healthy is True
    assert report.reason == "timestamp_unparseable"


def test_a_locked_store_is_not_a_dead_loop(store, settings, monkeypatch):
    def locked():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "latest_run", locked)

    report = evaluate(store, settings)

    assert report.healthy is True
    assert report.reason == "store_unreadable"


def test_a_failed_but_fresh_cycle_stays_healthy(store, settings):
    # A provider outage must not restart the container: the loop is alive and the
    # error count already reports the failure.
    finish_one_cycle(store, status="failed", errors=3)

    report = evaluate(store, settings)

    assert report.healthy is True
    assert report.reason == "cycle_fresh"
    assert report.status == "failed"


def test_the_report_is_machine_readable(store, settings):
    finish_one_cycle(store)

    payload = evaluate(store, settings).to_dict()

    assert payload["healthy"] is True
    assert payload["window_seconds"] == MIN_STALE_SECONDS
    assert payload["last_run_status"] == "completed"
    assert payload["bound"] == "cycle"
    assert json.dumps(payload)  # must be serialisable for --health --json


# -- wiring ---------------------------------------------------------------------


async def test_starting_the_engine_marks_the_boot(settings, store, make_verdict):
    engine = ScanEngine(
        settings, store=store, pipeline=FakePipeline(), audit=FakeAudit(make_verdict), alerter=FakeAlerter()
    )
    await engine.start()
    try:
        assert store.get_state(BOOT_STATE_KEY), "without this marker a restart looks like a stall"
    finally:
        await engine.close()


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A hermetic environment for the real ``main()``, rooted in ``tmp_path``."""
    hot_dir = tmp_path / "data" / "hot"
    hot_dir.mkdir(parents=True)
    clear_inherited_settings(monkeypatch)
    monkeypatch.setattr("config.settings._maybe_load_dotenv", lambda: None)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("LOG_JSON", "false")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOT_DB_PATH", str(hot_dir / "alpha_sentinel.db"))
    monkeypatch.setenv("PARQUET_ROOT", str(tmp_path / "data" / "cold"))
    monkeypatch.setenv("WATCHLIST_PATH", str(tmp_path / "watchlists.yaml"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    return hot_dir / "alpha_sentinel.db"


def test_the_health_flag_answers_on_a_fresh_store(cli_env, capsys):
    assert asyncio.run(bot_main.main(["--health"])) == bot_main.EXIT_OK
    assert "AlphaSentinel health: OK" in capsys.readouterr().out


def test_the_health_flag_fails_the_container_when_the_loop_stopped(cli_env, capsys):
    store = HotStore(cli_env)
    try:
        store.initialize()
        finish_one_cycle(store)
    finally:
        store.close()
    backdate(cli_env, hours=5)

    assert asyncio.run(bot_main.main(["--health"])) == bot_main.EXIT_ERROR
    out = capsys.readouterr().out
    assert "UNHEALTHY" in out
    assert "cycle_stale" in out


def test_the_health_flag_speaks_json_for_alerting(cli_env, capsys):
    assert asyncio.run(bot_main.main(["--health", "--json"])) == bot_main.EXIT_OK
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{") :])  # logs share stdout with the report
    assert payload["healthy"] is True
    assert payload["reason"] == "no_runs_yet"


# -- the second bound: producing, not merely running -----------------------------


def add_run(store: HotStore, *, verdicts: int, hours_ago: float) -> str:
    """A finished run stamped at a chosen time, so freshness can be dialled in."""
    run_id = store.start_run("scan")
    store.finish_run(run_id, tokens_scanned=1 if verdicts else 0, verdicts_written=verdicts)
    stamp = iso(utcnow() - timedelta(hours=hours_ago))
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE runs SET started_at = ?, finished_at = ? WHERE id = ?", (stamp, stamp, run_id))
    return run_id


def test_a_scheduler_that_produces_nothing_is_reported_stale(store, settings):
    """The failure a cycle-freshness check cannot see.

    A 6-hour cooldown makes empty cycles normal, so a daemon can keep finishing
    them for a week and stay green while writing no verdict at all. The loop is
    fine; the work is not, and only a bound on the last productive run says so.
    """
    add_run(store, verdicts=1, hours_ago=200)
    add_run(store, verdicts=0, hours_ago=0)
    store.put_state(BOOT_STATE_KEY, iso(utcnow() - timedelta(hours=200)))

    report = evaluate(store, settings)

    assert report.healthy is False
    assert report.reason == "verdicts_stale"


def test_a_fresh_volume_is_not_failed_for_having_produced_nothing_yet(store, settings):
    """Same discipline as the cycle check: a new install must not flap the container."""
    add_run(store, verdicts=0, hours_ago=0)
    store.put_state(BOOT_STATE_KEY, iso(utcnow()))

    assert evaluate(store, settings).healthy is True


def test_a_boot_that_never_wrote_a_verdict_is_a_stall(store, settings):
    """Cycles keep finishing, so only the boot marker says how long this has lasted."""
    add_run(store, verdicts=0, hours_ago=0)
    store.put_state(BOOT_STATE_KEY, iso(utcnow() - timedelta(hours=200)))

    report = evaluate(store, settings)

    assert report.healthy is False
    assert report.reason == "no_verdict_since_boot"


def test_the_verdict_bound_can_be_switched_off(store, settings):
    """An operator whose cooldowns outlast the default window gets to opt out."""
    disabled = replace(settings, scanners=replace(settings.scanners, health_max_stale_verdict_seconds=0))
    add_run(store, verdicts=1, hours_ago=900)
    add_run(store, verdicts=0, hours_ago=0)

    assert evaluate(store, disabled).healthy is True


def test_the_stale_verdict_report_names_the_bound_that_fired(store, settings):
    """Two windows now measure two different runs, so the output has to say which.

    Printing the verdict window next to the newest cycle's age reads as if nothing is
    wrong - 172800s against 240s looks enormously slack - while the run that actually
    tripped the check sits unreported. The age has to belong to the bound that fired.
    """
    add_run(store, verdicts=1, hours_ago=200)
    add_run(store, verdicts=0, hours_ago=0)

    report = evaluate(store, settings)

    assert report.bound == "verdict"
    assert report.verdict_run_at is not None
    assert report.verdict_run_at != report.last_run_at  # the fresh cycle is not the stale one
    line = report.format()
    assert "(verdict)" in line
    assert "last verdict:" in line
    assert "last run: completed at" in line


def test_the_boot_without_any_verdict_says_so_rather_than_printing_never_date(store, settings):
    """`no_verdict_since_boot` has no verdict run to name, and must not invent one."""
    add_run(store, verdicts=0, hours_ago=0)
    store.put_state(BOOT_STATE_KEY, iso(utcnow() - timedelta(hours=200)))

    report = evaluate(store, settings)

    assert report.bound == "verdict"
    assert report.verdict_run_at is None
    assert "last verdict: never" in report.format()


def test_an_unreadable_verdict_history_fails_open(store, settings, monkeypatch):
    """One read error is not evidence that the pipeline has stalled."""

    def boom() -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "last_verdict_run", boom)

    assert evaluate(store, settings).healthy is True
