"""SQLite "hot" store: last 72h of verdicts, runs, alerts and scan state.

Why SQLite on top of Parquet? Dashboards need indexed, millisecond point lookups
(``latest verdict for token X``) which are a poor fit for a columnar lake. The
hot DB therefore holds only recent, queryable state while Parquet remains the
immutable source of truth for analytics.

Concurrency notes
-----------------
* ``journal_mode=WAL`` + ``synchronous=NORMAL`` gives one writer and many
  concurrent readers - exactly the scanner (writer) + Streamlit (readers) model.
* Each thread gets its own connection via :class:`threading.local`; SQLite
  connections are not thread-safe, but one connection per thread is.
* Writes use ``BEGIN IMMEDIATE`` so the lock is acquired up-front instead of
  failing half-way through a transaction.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from core.logging import get_logger
from core.models import FinalVerdict, iso, utcnow

log = get_logger(__name__)

SCHEMA_VERSION = 3
#: Columns added after the first release. ``initialize`` backfills them on an
#: existing database so an upgrade never requires a manual migration step.
MIGRATED_COLUMNS: dict[str, dict[str, str]] = {
    "verdicts": {"asset_class": "TEXT NOT NULL DEFAULT 'crypto'"},
    "alerts": {"status": "TEXT NOT NULL DEFAULT ''"},
}


@dataclass(slots=True)
class RunRecord:
    id: str
    mode: str
    status: str
    started_at: str
    finished_at: str | None = None
    tokens_scanned: int = 0
    verdicts_written: int = 0
    approved: int = 0
    vetoed: int = 0
    errors: int = 0
    duration_ms: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "tokens_scanned": self.tokens_scanned,
            "verdicts_written": self.verdicts_written,
            "approved": self.approved,
            "vetoed": self.vetoed,
            "errors": self.errors,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address       TEXT    NOT NULL,
    chain               TEXT    NOT NULL,
    asset_class         TEXT    NOT NULL DEFAULT 'crypto',
    symbol              TEXT    NOT NULL DEFAULT '',
    status              TEXT    NOT NULL,
    final_score         REAL    NOT NULL,
    prior_score         REAL    NOT NULL,
    blended_score       REAL    NOT NULL,
    alpha_score         REAL    NOT NULL,
    alpha_confidence    REAL    NOT NULL DEFAULT 0,
    alpha_thesis        TEXT    NOT NULL DEFAULT '',
    risk_score          REAL    NOT NULL,
    risk_veto           INTEGER NOT NULL DEFAULT 0,
    risk_severity       TEXT    NOT NULL DEFAULT 'medium',
    risk_summary        TEXT    NOT NULL DEFAULT '',
    position_multiplier REAL    NOT NULL DEFAULT 0,
    rationale           TEXT    NOT NULL DEFAULT '',
    security_flags      TEXT    NOT NULL DEFAULT '',
    payload_json        TEXT    NOT NULL,
    latency_ms          REAL    NOT NULL DEFAULT 0,
    decided_at          TEXT    NOT NULL,
    created_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verdicts_decided ON verdicts(decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_verdicts_token   ON verdicts(token_address, decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_verdicts_status  ON verdicts(status, final_score DESC);

CREATE TABLE IF NOT EXISTS runs (
    id               TEXT PRIMARY KEY,
    mode             TEXT    NOT NULL DEFAULT 'scan',
    status           TEXT    NOT NULL DEFAULT 'running',
    started_at       TEXT    NOT NULL,
    finished_at      TEXT,
    tokens_scanned   INTEGER NOT NULL DEFAULT 0,
    verdicts_written INTEGER NOT NULL DEFAULT 0,
    approved         INTEGER NOT NULL DEFAULT 0,
    vetoed           INTEGER NOT NULL DEFAULT 0,
    errors           INTEGER NOT NULL DEFAULT 0,
    duration_ms      REAL    NOT NULL DEFAULT 0,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

CREATE TABLE IF NOT EXISTS seen_tokens (
    token_address TEXT    NOT NULL,
    chain         TEXT    NOT NULL,
    asset_class   TEXT    NOT NULL DEFAULT 'crypto',
    first_seen    TEXT    NOT NULL,
    last_seen     TEXT    NOT NULL,
    scan_count    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (token_address, chain, asset_class)
);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    chain         TEXT NOT NULL,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT '',
    message       TEXT NOT NULL DEFAULT '',
    payload_json  TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_VERDICT_COLUMNS = (
    "token_address, chain, asset_class, symbol, status, final_score, prior_score, blended_score, "
    "alpha_score, alpha_confidence, alpha_thesis, risk_score, risk_veto, risk_severity, "
    "risk_summary, position_multiplier, rationale, security_flags, payload_json, "
    "latency_ms, decided_at, created_at"
)


class HotStore:
    """Indexed, low-latency companion to :class:`storage.parquet_store.ParquetStore`."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 30.0,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.timeout = timeout
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()

    # -- connection plumbing ------------------------------------------------
    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                self.path,
                timeout=self.timeout,
                isolation_level=None,  # explicit transactions only
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
            self._local.conn = conn
        return conn

    @contextmanager
    def _cursor(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self._conn
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write and conn.in_transaction:
                conn.execute("COMMIT")
        except Exception:
            if write and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def initialize(self) -> None:
        conn = self._conn
        conn.executescript(_SCHEMA)
        self._migrate(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        log.info("hot_store_ready", extra={"path": str(self.path), "schema_version": SCHEMA_VERSION})

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns and rebuild keys introduced after the first release."""
        for table, columns in MIGRATED_COLUMNS.items():
            existing = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    log.info("hot_store_migrated", extra={"table": table, "column": column})
        # Existing alerts stored the status inside the payload; backfill it so the
        # dedupe key can distinguish an APPROVED alert from a later VETO.
        try:
            conn.execute(
                "UPDATE alerts SET status = COALESCE(json_extract(payload_json, '$.status'), '') "
                "WHERE status = ''"
            )
        except sqlite3.Error:  # pragma: no cover - SQLite without JSON1
            pass
        HotStore._migrate_seen_tokens(conn)

    @staticmethod
    def _migrate_seen_tokens(conn: sqlite3.Connection) -> None:
        """``seen_tokens`` must key on ``(address, chain, asset_class)``.

        SQLite cannot widen a primary key, so the table is rebuilt once and the
        old rows are carried over (defaulting to crypto, which is what they were).
        """
        info = conn.execute("PRAGMA table_info(seen_tokens)").fetchall()
        columns = {str(row["name"]) for row in info}
        primary_key = {str(row["name"]) for row in info if int(row["pk"]) > 0}
        if "asset_class" in columns and primary_key == {"token_address", "chain", "asset_class"}:
            return
        conn.execute("ALTER TABLE seen_tokens RENAME TO seen_tokens_legacy")
        conn.executescript(
            """
            CREATE TABLE seen_tokens (
                token_address TEXT    NOT NULL,
                chain         TEXT    NOT NULL,
                asset_class   TEXT    NOT NULL DEFAULT 'crypto',
                first_seen    TEXT    NOT NULL,
                last_seen     TEXT    NOT NULL,
                scan_count    INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (token_address, chain, asset_class)
            );
            """
        )
        if "asset_class" in columns:
            conn.execute(
                "INSERT OR REPLACE INTO seen_tokens "
                "(token_address, chain, asset_class, first_seen, last_seen, scan_count) "
                "SELECT token_address, chain, asset_class, first_seen, last_seen, scan_count "
                "FROM seen_tokens_legacy"
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO seen_tokens "
                "(token_address, chain, asset_class, first_seen, last_seen, scan_count) "
                "SELECT token_address, chain, 'crypto', first_seen, last_seen, scan_count "
                "FROM seen_tokens_legacy"
            )
        conn.execute("DROP TABLE seen_tokens_legacy")
        log.info("hot_store_migrated", extra={"table": "seen_tokens", "column": "asset_class"})

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def table_names(self) -> list[str]:
        with self._cursor() as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return [row["name"] for row in rows]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"HotStore(path={self.path!s})"

    # -- verdicts -----------------------------------------------------------
    def record_verdict(self, verdict: FinalVerdict) -> int:
        """Persist one :class:`FinalVerdict`; returns the new row id."""
        payload = json.dumps(verdict.to_dict(), ensure_ascii=False, default=str)
        values = (
            verdict.token_address,
            verdict.chain,
            verdict.asset_class,
            verdict.symbol,
            verdict.status.value,
            float(verdict.final_score),
            float(verdict.prior_score),
            float(verdict.blended_score),
            float(verdict.alpha.alpha_score),
            float(verdict.alpha.confidence),
            verdict.alpha.thesis[:4000],
            float(verdict.risk.risk_score),
            int(verdict.risk.effective_veto()),
            verdict.risk.severity.value,
            verdict.risk.reasoning_summary[:4000],
            float(verdict.position_multiplier),
            verdict.rationale[:4000],
            ",".join(verdict.security_flags),
            payload,
            float(verdict.latency_ms),
            iso(verdict.decided_at),
            iso(utcnow()),
        )
        placeholders = ", ".join("?" for _ in values)
        with self._cursor(write=True) as conn:
            cursor = conn.execute(
                f"INSERT INTO verdicts ({_VERDICT_COLUMNS}) VALUES ({placeholders})",
                values,
            )
            row_id = int(cursor.lastrowid or 0)
        log.debug(
            "verdict_persisted",
            extra={"id": row_id, "token": verdict.token_address, "status": verdict.status.value},
        )
        return row_id

    @staticmethod
    def _row_to_dict(row: sqlite3.Row, *, include_payload: bool = True) -> dict[str, Any]:
        record = dict(row)
        raw = record.get("payload_json")
        if include_payload and raw:
            try:
                record["payload"] = json.loads(raw)
            except json.JSONDecodeError:  # pragma: no cover - defensive
                record["payload"] = {}
        record.pop("payload_json", None)
        record["risk_veto"] = bool(record.get("risk_veto"))
        # The dashboard speaks "severity"; SQLite stores "risk_severity". Alias it
        # here so every reader (table + detail) sees the same column name.
        record.setdefault("severity", record.get("risk_severity"))
        return record

    def _verdict_where(
        self,
        status: str | None,
        chain: str | None,
        since_hours: float | None,
        token_address: str | None = None,
        asset_class: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if chain:
            clauses.append("chain = ?")
            params.append(chain)
        if asset_class:
            clauses.append("asset_class = ?")
            params.append(asset_class)
        if token_address:
            clauses.append("token_address = ?")
            params.append(token_address)
        if since_hours is not None:
            clauses.append("decided_at >= ?")
            params.append(iso(utcnow() - timedelta(hours=since_hours)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def recent_verdicts(
        self,
        limit: int = 50,
        *,
        status: str | None = None,
        chain: str | None = None,
        asset_class: str | None = None,
        since_hours: float | None = None,
        include_payload: bool = True,
    ) -> list[dict[str, Any]]:
        where, params = self._verdict_where(status, chain, since_hours, asset_class=asset_class)
        sql = f"SELECT * FROM verdicts {where} ORDER BY decided_at DESC, id DESC LIMIT ?"
        with self._cursor() as conn:
            rows = conn.execute(sql, [*params, max(int(limit), 1)]).fetchall()
        return [self._row_to_dict(row, include_payload=include_payload) for row in rows]

    def verdict_history(
        self,
        token_address: str,
        *,
        chain: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where, params = self._verdict_where(None, chain, None, token_address)
        sql = f"SELECT * FROM verdicts {where} ORDER BY decided_at DESC, id DESC LIMIT ?"
        with self._cursor() as conn:
            rows = conn.execute(sql, [*params, max(int(limit), 1)]).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def latest_verdict(self, token_address: str, *, chain: str | None = None) -> dict[str, Any] | None:
        history = self.verdict_history(token_address, chain=chain, limit=1)
        return history[0] if history else None

    def get_verdict(self, verdict_id: int) -> dict[str, Any] | None:
        """Fetch one verdict by row id (used by the dashboard detail pane)."""
        with self._cursor() as conn:
            row = conn.execute("SELECT * FROM verdicts WHERE id = ?", (int(verdict_id),)).fetchone()
        return self._row_to_dict(row) if row else None

    def top_scores(self, limit: int = 20, *, since_hours: float = 24.0) -> list[dict[str, Any]]:
        """Best actionable (approved/watch) verdicts in the recent window."""
        where, params = self._verdict_where(None, None, since_hours)
        clause = "WHERE" if not where else "AND"
        sql = (
            f"SELECT * FROM verdicts {where} {clause} status IN ('approved', 'watch') "
            "ORDER BY final_score DESC LIMIT ?"
        )
        with self._cursor() as conn:
            rows = conn.execute(sql, [*params, max(int(limit), 1)]).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def status_breakdown(self, *, since_hours: float = 24.0) -> dict[str, int]:
        where, params = self._verdict_where(None, None, since_hours)
        with self._cursor() as conn:
            rows = conn.execute(
                f"SELECT status, COUNT(*) AS total FROM verdicts {where} GROUP BY status", params
            ).fetchall()
        return {row["status"]: int(row["total"]) for row in rows}

    def count_verdicts(self, *, status: str | None = None, since_hours: float | None = None) -> int:
        where, params = self._verdict_where(status, None, since_hours)
        with self._cursor() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS total FROM verdicts {where}", params).fetchone()
        return int(row["total"]) if row else 0

    # -- runs ---------------------------------------------------------------
    def start_run(self, mode: str = "scan") -> str:
        run_id = uuid.uuid4().hex
        with self._cursor(write=True) as conn:
            conn.execute(
                "INSERT INTO runs (id, mode, status, started_at) VALUES (?, ?, 'running', ?)",
                (run_id, mode, iso(utcnow())),
            )
        log.info("run_started", extra={"run_id": run_id, "mode": mode})
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str = "completed",
        tokens_scanned: int = 0,
        verdicts_written: int = 0,
        approved: int = 0,
        vetoed: int = 0,
        errors: int = 0,
        error: str | None = None,
    ) -> None:
        finished = utcnow()
        duration = 0.0
        with self._cursor(write=True) as conn:
            row = conn.execute("SELECT started_at FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row:
                try:
                    duration = max((finished - datetime.fromisoformat(row["started_at"])).total_seconds() * 1000, 0.0)
                except ValueError:  # pragma: no cover - defensive
                    duration = 0.0
            conn.execute(
                """
                UPDATE runs SET status = ?, finished_at = ?, tokens_scanned = ?, verdicts_written = ?,
                       approved = ?, vetoed = ?, errors = ?, duration_ms = ?, error = ?
                WHERE id = ?
                """,
                (
                    status,
                    iso(finished),
                    tokens_scanned,
                    verdicts_written,
                    approved,
                    vetoed,
                    errors,
                    duration,
                    (error or "")[:2000] or None,
                    run_id,
                ),
            )
        log.info(
            "run_finished",
            extra={
                "run_id": run_id,
                "status": status,
                "tokens": tokens_scanned,
                "verdicts": verdicts_written,
                "errors": errors,
                "duration_ms": round(duration, 1),
            },
        )

    def fail_stale_runs(self, older_than_minutes: int = 180) -> int:
        """Mark runs abandoned by a killed process so the dashboard stays honest."""
        cutoff = iso(utcnow() - timedelta(minutes=older_than_minutes))
        with self._cursor(write=True) as conn:
            cursor = conn.execute(
                "UPDATE runs SET status = 'abandoned', error = COALESCE(error, 'process interrupted') "
                "WHERE status = 'running' AND started_at < ?",
                (cutoff,),
            )
            return int(cursor.rowcount or 0)

    def latest_run(self) -> dict[str, Any] | None:
        with self._cursor() as conn:
            row = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return RunRecord(**dict(row)).to_dict() if row else None

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (max(int(limit), 1),)
            ).fetchall()
        return [RunRecord(**dict(row)).to_dict() for row in rows]

    # -- scan state (dedupe / cooldown) -------------------------------------
    def is_due(
        self,
        token_address: str,
        chain: str,
        cooldown_hours: float,
        *,
        asset_class: str = "crypto",
    ) -> bool:
        """True when the instrument was never scanned or its cooldown expired."""
        cutoff = iso(utcnow() - timedelta(hours=max(cooldown_hours, 0.0)))
        with self._cursor() as conn:
            row = conn.execute(
                "SELECT last_seen FROM seen_tokens "
                "WHERE token_address = ? AND chain = ? AND asset_class = ?",
                (token_address, chain, asset_class),
            ).fetchone()
        if row is None:
            return True
        return str(row["last_seen"]) <= cutoff

    def mark_seen(self, token_address: str, chain: str, *, asset_class: str = "crypto") -> None:
        now = iso(utcnow())
        with self._cursor(write=True) as conn:
            conn.execute(
                """
                INSERT INTO seen_tokens
                    (token_address, chain, asset_class, first_seen, last_seen, scan_count)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(token_address, chain, asset_class) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    scan_count = seen_tokens.scan_count + 1
                """,
                (token_address, chain, asset_class, now, now),
            )

    def seen_count(self, *, since_hours: float | None = None) -> int:
        sql = "SELECT COUNT(*) AS total FROM seen_tokens"
        params: list[Any] = []
        if since_hours is not None:
            sql += " WHERE last_seen >= ?"
            params.append(iso(utcnow() - timedelta(hours=since_hours)))
        with self._cursor() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["total"]) if row else 0

    # -- alerts -------------------------------------------------------------
    def record_alert(
        self,
        token_address: str,
        chain: str,
        kind: str,
        message: str = "",
        payload: dict[str, Any] | None = None,
        *,
        status: str = "",
    ) -> int:
        with self._cursor(write=True) as conn:
            cursor = conn.execute(
                "INSERT INTO alerts (token_address, chain, kind, status, message, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    token_address,
                    chain,
                    kind,
                    status,
                    message[:2000],
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                    iso(utcnow()),
                ),
            )
            return int(cursor.lastrowid or 0)

    def recent_alerts(self, limit: int = 50, *, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            sql, params = "SELECT * FROM alerts WHERE kind = ? ORDER BY created_at DESC LIMIT ?", [kind]
        else:
            sql, params = "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", []
        with self._cursor() as conn:
            rows = conn.execute(sql, [*params, max(int(limit), 1)]).fetchall()
        alerts = []
        for row in rows:
            record = dict(row)
            try:
                record["payload"] = json.loads(record.get("payload_json") or "{}")
            except json.JSONDecodeError:  # pragma: no cover - defensive
                record["payload"] = {}
            record.pop("payload_json", None)
            alerts.append(record)
        return alerts

    def alert_was_sent(
        self,
        token_address: str,
        kind: str,
        *,
        chain: str | None = None,
        status: str | None = None,
        within_hours: float = 24.0,
    ) -> bool:
        """Dedupe lookup that never hides a change of state.

        The key includes the venue (``chain``) and the verdict status: the same
        ticker on two venues is two instruments, and an APPROVED alert must not
        suppress the VETO that follows a honeypot detection next cycle.
        """
        cutoff = iso(utcnow() - timedelta(hours=within_hours))
        sql = "SELECT 1 FROM alerts WHERE token_address = ? AND kind = ? AND created_at >= ?"
        params: list[Any] = [token_address, kind, cutoff]
        if chain:
            sql += " AND chain = ?"
            params.append(chain)
        if status:
            sql += " AND status = ?"
            params.append(status)
        with self._cursor() as conn:
            row = conn.execute(sql + " LIMIT 1", params).fetchone()
        return row is not None

    # -- key/value state ----------------------------------------------------
    def put_state(self, key: str, value: Any) -> None:
        with self._cursor(write=True) as conn:
            conn.execute(
                "INSERT INTO kv (key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False, default=str), iso(utcnow())),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._cursor() as conn:
            row = conn.execute("SELECT value_json FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:  # pragma: no cover - defensive
            return default

    def delete_state(self, key: str) -> bool:
        with self._cursor(write=True) as conn:
            cursor = conn.execute("DELETE FROM kv WHERE key = ?", (key,))
        return bool(cursor.rowcount)

    # -- maintenance --------------------------------------------------------
    def prune(self, *, verdict_days: int = 7, alert_days: int = 30) -> dict[str, int]:
        """Trim the hot DB; Parquet keeps the long-term history."""
        n_verdicts = n_alerts = 0
        with self._cursor(write=True) as conn:
            if verdict_days > 0:
                cursor = conn.execute(
                    "DELETE FROM verdicts WHERE decided_at < ?",
                    (iso(utcnow() - timedelta(days=verdict_days)),),
                )
                n_verdicts = int(cursor.rowcount or 0)
            if alert_days > 0:
                cursor = conn.execute(
                    "DELETE FROM alerts WHERE created_at < ?",
                    (iso(utcnow() - timedelta(days=alert_days)),),
                )
                n_alerts = int(cursor.rowcount or 0)
            conn.execute(
                "DELETE FROM runs WHERE started_at < ?",
                (iso(utcnow() - timedelta(days=max(verdict_days, 7))),),
            )
        log.info("hot_store_pruned", extra={"verdicts": n_verdicts, "alerts": n_alerts})
        return {"verdicts": n_verdicts, "alerts": n_alerts}

    def vacuum(self) -> None:
        conn = self._conn
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")

    def stats(self) -> dict[str, Any]:
        with self._cursor() as conn:
            verdicts = conn.execute("SELECT COUNT(*) AS n FROM verdicts").fetchone()["n"]
            runs = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
            alerts = conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]
            seen = conn.execute("SELECT COUNT(*) AS n FROM seen_tokens").fetchone()["n"]
            last = conn.execute("SELECT MAX(decided_at) AS ts FROM verdicts").fetchone()["ts"]
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "path": str(self.path),
            "verdicts": int(verdicts),
            "runs": int(runs),
            "alerts": int(alerts),
            "seen_tokens": int(seen),
            "last_verdict_at": last,
            "size_bytes": size,
        }

    def health(self) -> dict[str, Any]:
        try:
            with self._cursor() as conn:
                conn.execute("SELECT 1").fetchone()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            return {"healthy": False, "error": str(exc), "path": str(self.path)}
        try:
            stats = self.stats()
        except (sqlite3.Error, OSError) as exc:  # pragma: no cover - defensive
            return {"healthy": False, "error": str(exc), "path": str(self.path)}
        return {"healthy": True, "error": None, **stats}

    # -- async twins --------------------------------------------------------
    async def record_verdict_async(self, verdict: FinalVerdict) -> int:
        return await asyncio.to_thread(self.record_verdict, verdict)

    async def mark_seen_async(self, token_address: str, chain: str, *, asset_class: str = "crypto") -> None:
        await asyncio.to_thread(self.mark_seen, token_address, chain, asset_class=asset_class)

    async def record_alert_async(self, *args: Any, **kwargs: Any) -> int:
        return await asyncio.to_thread(self.record_alert, *args, **kwargs)


__all__ = ["HotStore", "RunRecord", "SCHEMA_VERSION"]
