"""Hot store (SQLite): the fast path the scanner and dashboard both read."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from core.models import Severity, VerdictStatus, utcnow
from storage.hot_store import HotStore
from tests.conftest import OTHER_TOKEN, TOKEN


def test_initialize_creates_the_full_schema(store: HotStore):
    tables = set(store.table_names())
    assert {"verdicts", "runs", "alerts", "seen_tokens", "kv"} <= tables
    assert store.health()["healthy"] is True


def test_verdict_roundtrip_keeps_the_payload(store: HotStore, make_verdict):
    verdict = make_verdict()
    row_id = store.record_verdict(verdict)
    assert row_id > 0

    record = store.get_verdict(row_id)
    assert record is not None
    assert record["token_address"] == TOKEN
    assert record["status"] == "approved"
    assert record["risk_veto"] is False
    assert record["payload"]["symbol"] == "TEST"
    assert record["payload"]["alpha"]["thesis"].startswith("Momentum")

    assert store.get_verdict(row_id + 999) is None


def test_recent_verdicts_filters_and_omits_payload(store: HotStore, make_verdict):
    store.record_verdict(make_verdict())
    store.record_verdict(make_verdict(token_address=OTHER_TOKEN, status=VerdictStatus.VETOED, final_score=12.0))

    rows = store.recent_verdicts(10, include_payload=False)
    assert len(rows) == 2
    assert all("payload" not in row for row in rows)

    vetoed = store.recent_verdicts(10, status="vetoed")
    assert [row["token_address"] for row in vetoed] == [OTHER_TOKEN]
    assert store.recent_verdicts(10, chain="solana") == []
    assert len(store.recent_verdicts(10, since_hours=1)) == 2
    assert store.count_verdicts() == 2
    assert store.status_breakdown() == {"approved": 1, "vetoed": 1}

    empty_since = store.recent_verdicts(10, since_hours=-1)
    assert empty_since == []


def test_top_scores_only_returns_actionable_rows(store: HotStore, make_verdict):
    store.record_verdict(make_verdict(final_score=66.0))
    store.record_verdict(make_verdict(token_address=OTHER_TOKEN, status=VerdictStatus.WATCH, final_score=58.0))
    store.record_verdict(
        make_verdict(token_address="0x" + "ee" * 20, status=VerdictStatus.REJECTED, final_score=99.0)
    )
    top = store.top_scores(limit=5)
    assert [row["status"] for row in top] == ["approved", "watch"]
    assert [row["final_score"] for row in top] == [66.0, 58.0]


def test_verdict_history_is_per_token(store: HotStore, make_verdict):
    older = utcnow() - timedelta(hours=3)
    store.record_verdict(make_verdict(decided_at=older, final_score=55.0))
    store.record_verdict(make_verdict(final_score=75.0))
    store.record_verdict(make_verdict(token_address=OTHER_TOKEN, final_score=90.0))

    history = store.verdict_history(TOKEN)
    assert [row["final_score"] for row in history] == [75.0, 55.0]
    latest = store.latest_verdict(TOKEN)
    assert latest is not None and latest["final_score"] == 75.0
    assert store.latest_verdict("0x" + "ff" * 20) is None


def test_cooldown_gate(store: HotStore):
    assert store.is_due(TOKEN, "ethereum", 6.0) is True
    store.mark_seen(TOKEN, "ethereum")
    assert store.is_due(TOKEN, "ethereum", 6.0) is False
    assert store.is_due(TOKEN, "ethereum", 0.0) is True
    assert store.is_due(TOKEN, "solana", 6.0) is True
    assert store.seen_count() == 1

    store.mark_seen(TOKEN, "ethereum")
    assert store.seen_count() == 1  # upsert, not a second row


def test_alert_dedupe_window(store: HotStore):
    assert store.alert_was_sent(TOKEN, "verdict") is False
    alert_id = store.record_alert(TOKEN, "ethereum", "verdict", "hello", {"score": 77.0})
    assert alert_id > 0
    assert store.alert_was_sent(TOKEN, "verdict") is True
    assert store.alert_was_sent(TOKEN, "other") is False
    assert store.alert_was_sent(TOKEN, "verdict", within_hours=-1) is False

    alerts = store.recent_alerts(limit=5)
    assert alerts[0]["payload"] == {"score": 77.0}
    assert "payload_json" not in alerts[0]


def test_run_lifecycle(store: HotStore):
    run_id = store.start_run("scan")
    store.finish_run(run_id, tokens_scanned=3, verdicts_written=2, approved=1, vetoed=1, errors=0)

    latest = store.latest_run()
    assert latest is not None
    assert latest["id"] == run_id
    assert latest["status"] == "completed"
    assert latest["tokens_scanned"] == 3
    assert latest["duration_ms"] >= 0.0
    assert len(store.recent_runs()) == 1


def test_stale_runs_are_abandoned(store: HotStore):
    run_id = store.start_run("scan")
    assert store.fail_stale_runs(older_than_minutes=-1) == 1
    assert store.latest_run()["status"] == "abandoned"
    assert store.latest_run()["id"] == run_id


def test_kv_state(store: HotStore):
    assert store.get_state("macro", default={}) == {}
    store.put_state("macro", {"risk_off": True})
    assert store.get_state("macro") == {"risk_off": True}
    store.put_state("macro", {"risk_off": False})
    assert store.get_state("macro") == {"risk_off": False}
    assert store.delete_state("macro") is True
    assert store.get_state("macro") is None


def test_prune_removes_old_rows(store: HotStore, make_verdict):
    store.record_verdict(make_verdict(decided_at=utcnow() - timedelta(days=30)))
    store.record_verdict(make_verdict(decided_at=utcnow()))
    result = store.prune(verdict_days=7, alert_days=30)
    assert result["verdicts"] == 1
    assert store.count_verdicts() == 1
    assert store.stats()["verdicts"] == 1


def test_verdict_severity_is_persisted(store: HotStore, make_verdict):
    verdict = make_verdict()
    verdict.risk.severity = Severity.HIGH
    row_id = store.record_verdict(verdict)
    assert store.get_verdict(row_id)["risk_severity"] == "high"


def test_asset_class_is_persisted_and_filterable(store: HotStore, make_verdict):
    store.record_verdict(make_verdict())
    store.record_verdict(
        make_verdict(
            token_address="AAPL",
            chain="us_equity",
            symbol="AAPL",
            asset_class="equity",
        )
    )
    assert store.count_verdicts() == 2
    equities = store.recent_verdicts(limit=10, asset_class="equity")
    assert [row["token_address"] for row in equities] == ["AAPL"]
    assert store.recent_verdicts(limit=10, asset_class="crypto")[0]["asset_class"] == "crypto"
    assert store.get_verdict(equities[0]["id"])["payload"]["asset_class"] == "equity"


def test_alert_dedupe_is_scoped_to_venue_and_status(store: HotStore):
    store.record_alert(TOKEN, "ethereum", "verdict", "approved", {"status": "approved"}, status="approved")

    assert store.alert_was_sent(TOKEN, "verdict", chain="ethereum", status="approved") is True
    # A veto must never be suppressed by an earlier approval.
    assert store.alert_was_sent(TOKEN, "verdict", chain="ethereum", status="vetoed") is False
    # The same ticker on another venue is a different instrument.
    assert store.alert_was_sent(TOKEN, "verdict", chain="solana", status="approved") is False


def test_cooldown_is_scoped_to_asset_class(store: HotStore):
    store.mark_seen(TOKEN, "ethereum", asset_class="crypto")

    assert store.is_due(TOKEN, "ethereum", 6.0, asset_class="crypto") is False
    assert store.is_due(TOKEN, "ethereum", 6.0, asset_class="equity") is True


def test_recent_verdicts_expose_the_dashboard_severity_alias(store: HotStore, make_verdict):
    verdict = make_verdict()
    verdict.risk.severity = Severity.HIGH
    store.record_verdict(verdict)

    row = store.recent_verdicts(1, include_payload=False)[0]

    assert row["risk_severity"] == "high"
    assert row["severity"] == "high"


def test_legacy_seen_tokens_table_is_rebuilt_with_asset_class(tmp_path):
    path = tmp_path / "legacy_seen.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE seen_tokens (
            token_address TEXT NOT NULL,
            chain TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            scan_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (token_address, chain)
        );
        """
    )
    now = "2026-01-01T00:00:00+00:00"
    connection.execute(
        "INSERT INTO seen_tokens (token_address, chain, first_seen, last_seen, scan_count) "
        "VALUES (?, 'ethereum', ?, ?, 4)",
        (TOKEN, now, utcnow().isoformat()),
    )
    connection.commit()
    connection.close()

    store = HotStore(path)
    store.initialize()
    try:
        assert store.seen_count() == 1
        assert store.is_due(TOKEN, "ethereum", 6.0, asset_class="crypto") is False
        store.mark_seen(TOKEN, "ethereum", asset_class="equity")
        assert store.seen_count() == 2
    finally:
        store.close()


def test_legacy_alerts_backfill_status_from_the_payload(tmp_path):
    path = tmp_path / "legacy_alerts.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_address TEXT NOT NULL,
            chain TEXT NOT NULL,
            kind TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO alerts (token_address, chain, kind, payload_json, created_at) "
        "VALUES (?, 'ethereum', 'verdict', '{\"status\": \"approved\"}', ?)",
        (TOKEN, utcnow().isoformat()),
    )
    connection.commit()
    connection.close()

    store = HotStore(path)
    store.initialize()
    try:
        assert store.recent_alerts(1)[0]["status"] == "approved"
        assert store.alert_was_sent(TOKEN, "verdict", chain="ethereum", status="approved") is True
    finally:
        store.close()


def test_legacy_database_is_migrated_in_place(tmp_path, make_verdict):
    """A schema-v1 DB must keep working: the column is added, old rows default."""
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_address TEXT NOT NULL,
            chain TEXT NOT NULL,
            symbol TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            final_score REAL NOT NULL,
            prior_score REAL NOT NULL,
            blended_score REAL NOT NULL,
            alpha_score REAL NOT NULL,
            alpha_confidence REAL NOT NULL DEFAULT 0,
            alpha_thesis TEXT NOT NULL DEFAULT '',
            risk_score REAL NOT NULL,
            risk_veto INTEGER NOT NULL DEFAULT 0,
            risk_severity TEXT NOT NULL DEFAULT 'medium',
            risk_summary TEXT NOT NULL DEFAULT '',
            position_multiplier REAL NOT NULL DEFAULT 0,
            rationale TEXT NOT NULL DEFAULT '',
            security_flags TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            latency_ms REAL NOT NULL DEFAULT 0,
            decided_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO verdicts (token_address, chain, symbol, status, final_score, prior_score, "
        "blended_score, alpha_score, risk_score, payload_json, decided_at, created_at) "
        "VALUES ('0xabc', 'ethereum', 'OLD', 'watch', 55, 55, 55, 55, 20, '{}', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    store = HotStore(path)
    store.initialize()
    try:
        rows = store.recent_verdicts(limit=5)
        assert rows[0]["asset_class"] == "crypto"
        store.record_verdict(make_verdict(asset_class="equity", token_address="MSFT", chain="us_equity"))
        assert len(store.recent_verdicts(limit=5, asset_class="equity")) == 1
    finally:
        store.close()
