"""Hot-store snapshots, and the rehearsal that proves they restore.

The SQLite hot store is the only copy of verdicts, runs and alerts. The Parquet
lake is analytics history, and the optional Cloudflare R2 mirror covers *that*
lake only - nothing in the repository has ever copied ``alpha_sentinel.db``. So
losing the disk did not look like a failure: the store self-initialises on an
empty volume and the daemon comes up normally, having forgotten every verdict it
ever made.

Two properties make a backup worth having, and neither of them is "a file
appeared somewhere":

* **Consistency.** WAL mode means the database is three files, so a ``shutil``
  copy can capture a torn page. SQLite's online backup API reads through the
  write-ahead log, so a snapshot taken in the middle of a scan cycle is still a
  database that existed at a single instant.
* **Restorability.** :func:`rehearse` copies the snapshot to a scratch directory
  and boots a :class:`~storage.hot_store.HotStore` on it, running the same
  migrations a real restore would. It then compares row counts before and after,
  so a migration that silently drops rows fails here instead of in production.

A backup that has never been restored is a hope, not a backup.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from config.settings import R2Settings
from core.logging import get_logger
from core.models import utcnow
from storage.hot_store import SCHEMA_VERSION, HotStore

log = get_logger(__name__)

#: Snapshot filenames sort chronologically, which is what retention relies on.
BACKUP_PREFIX = "alpha_sentinel"
BACKUP_SUFFIX = ".db"
DEFAULT_KEEP = 7
#: Folder inside the bucket; the lake keeps its own prefix, so they cannot collide.
REMOTE_FOLDER = "hot-backups"


class BackupError(RuntimeError):
    """Raised when a snapshot cannot be taken, or does not restore."""


def _uri(path: Path, mode: str) -> str:
    """A ``file:`` URI, percent-encoded by ``Path`` so odd directories still work."""
    return f"{path.resolve().as_uri()}?mode={mode}"


def backup_name(moment: datetime | None = None) -> str:
    """``alpha_sentinel-20261030T145700Z.db`` - sorts by the time it was taken."""
    stamp = (moment or utcnow()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{BACKUP_PREFIX}-{stamp}{BACKUP_SUFFIX}"


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row count per user table.

    ``sqlite_%`` is excluded because those are SQLite's own bookkeeping tables
    (``sqlite_sequence`` exists the moment any table uses AUTOINCREMENT), and a
    restore report that counts them tells an operator nothing.
    """
    names = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in names}


def missing_rows(before: Mapping[str, int], after: Mapping[str, int]) -> list[str]:
    """Tables that would come back with fewer rows than the snapshot holds.

    Separate from :func:`rehearse` so the rule can be tested as a rule. Building a
    database that genuinely loses rows under today's migrations would mean writing
    a fixture for a migration bug that does not exist yet, which proves nothing
    about the guard; comparing two counts proves it exactly.
    """
    problems: list[str] = []
    for name, count in before.items():
        now = after.get(name)
        if now is None:
            problems.append(f"table {name} disappeared during migration")
        elif now != count:
            problems.append(f"table {name} went from {count} to {now} rows")
    return problems


@dataclass(slots=True)
class Snapshot:
    """A verified snapshot on disk."""

    path: Path
    size_bytes: int
    schema_version: int
    tables: dict[str, int]
    source: Path | None = None

    @property
    def rows(self) -> int:
        return sum(self.tables.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "source": str(self.source) if self.source else None,
            "size_bytes": self.size_bytes,
            "schema_version": self.schema_version,
            "rows": self.rows,
            "tables": dict(sorted(self.tables.items())),
        }

    def format(self) -> str:
        return (
            f"{self.path.name}: {self.size_bytes:,} bytes, schema v{self.schema_version}, "
            f"{self.rows:,} rows across {len(self.tables)} tables"
        )


@dataclass(slots=True)
class Rehearsal:
    """The result of restoring a snapshot into a scratch directory."""

    snapshot: Snapshot
    ok: bool
    schema_version_after: int = 0
    tables_after: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "backup": self.snapshot.to_dict(),
            "schema_version_after": self.schema_version_after,
            "tables_after": dict(sorted(self.tables_after.items())),
            "problems": list(self.problems),
        }

    def format(self) -> str:
        head = "AlphaSentinel backup rehearsal: OK" if self.ok else "AlphaSentinel backup rehearsal: FAILED"
        lines = [
            head,
            f"  snapshot: {self.snapshot.format()}",
            f"  migrations: schema v{self.snapshot.schema_version} -> v{self.schema_version_after}",
        ]
        for name, before in sorted(self.snapshot.tables.items()):
            lines.append(f"  {name:<12} {before:>7} -> {self.tables_after.get(name, 0):>7}")
        for problem in self.problems:
            lines.append(f"  ! {problem}")
        return "\n".join(lines)


def snapshot(source: str | Path, destination: str | Path) -> Snapshot:
    """Copy ``source`` to ``destination`` and return the verified result.

    The copy uses SQLite's online backup API rather than ``shutil``: in WAL mode a
    plain file copy can capture a torn page, and the failure is intermittent
    enough to survive any amount of manual testing.
    """
    src = Path(source).expanduser().resolve()
    dst = Path(destination).expanduser().resolve()
    if not src.is_file():
        raise BackupError(f"nothing to back up: {src} does not exist")
    dst.parent.mkdir(parents=True, exist_ok=True)

    source_conn = sqlite3.connect(_uri(src, "rw"), uri=True, timeout=30.0)
    try:
        target_conn = sqlite3.connect(str(dst), isolation_level=None)
        try:
            source_conn.backup(target_conn)
            # The copy inherits ``journal_mode=WAL`` from the source header, which
            # would leave a ``-wal`` sidecar beside it: a snapshot that restores
            # only while three files travel together is not a backup. Folding the
            # log back in leaves one self-contained file, which is what an
            # operator copies to another machine.
            target_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            target_conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            target_conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"could not copy {src.name}: {exc}") from exc
    finally:
        source_conn.close()

    result = inspect(dst, source=src)
    log.info(
        "hot_store_snapshot",
        extra={"path": str(result.path), "bytes": result.size_bytes, "rows": result.rows},
    )
    return result


def inspect(path: str | Path, *, source: Path | None = None) -> Snapshot:
    """Open a candidate snapshot read-only and refuse it unless it is sound."""
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise BackupError(f"snapshot not found: {target}")
    try:
        conn = sqlite3.connect(_uri(target, "ro"), uri=True, timeout=10.0)
        try:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            tables = table_counts(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"{target.name} is not a readable SQLite database: {exc}") from exc
    if integrity != "ok":
        # Truncation, a half-flushed page or a stray write all land here, and the
        # message is the only chance an operator gets to notice before a restore.
        raise BackupError(f"{target.name} failed integrity_check: {integrity[:200]}")
    if version <= 0:
        raise BackupError(f"{target.name} has no schema version - it is empty, not a hot store")
    if version > SCHEMA_VERSION:
        raise BackupError(
            f"{target.name} is schema v{version}, newer than this release (v{SCHEMA_VERSION}); "
            "it was written by a later build and cannot be read backwards"
        )
    return Snapshot(
        path=target,
        size_bytes=target.stat().st_size,
        schema_version=version,
        tables=tables,
        source=source,
    )


def rehearse(path: str | Path) -> Rehearsal:
    """Restore a snapshot into a throwaway directory and boot the store on it.

    Nothing is written next to the snapshot: the rehearsal works on a copy, so
    running it against a real backup cannot leave a half-migrated database where
    the good one used to be.
    """
    taken = inspect(path)
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="alphasentinel-restore-") as workdir:
        restored = Path(workdir) / "restored.db"
        shutil.copyfile(taken.path, restored)
        store = HotStore(restored)
        try:
            store.initialize()
        except sqlite3.Error as exc:
            problems.append(f"restored database did not initialise: {exc}")
        finally:
            store.close()
        if not problems:
            conn = sqlite3.connect(_uri(restored, "ro"), uri=True, timeout=10.0)
            try:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                after = table_counts(conn)
            finally:
                conn.close()
            problems.extend(missing_rows(taken.tables, after))
            if version != SCHEMA_VERSION:
                problems.append(f"restored schema is v{version}, expected v{SCHEMA_VERSION}")
        else:
            version = 0
            after = {}

    report = Rehearsal(
        snapshot=taken,
        ok=not problems,
        schema_version_after=version,
        tables_after=after,
        problems=problems,
    )
    log.info(
        "hot_store_rehearsal",
        extra={"path": str(taken.path), "ok": report.ok, "problems": len(problems)},
    )
    return report


def prune(directory: str | Path, *, keep: int = DEFAULT_KEEP) -> list[Path]:
    """Delete all but the newest ``keep`` snapshots; returns what was removed.

    ``keep <= 0`` is treated as "remove nothing" rather than "remove everything":
    a typo in a cron flag must not be able to destroy the only remaining copies.
    """
    folder = Path(directory).expanduser().resolve()
    if keep <= 0 or not folder.is_dir():
        return []
    snapshots = sorted(folder.glob(f"{BACKUP_PREFIX}-*{BACKUP_SUFFIX}"))
    removed: list[Path] = []
    for path in snapshots[:-keep] if len(snapshots) > keep else []:
        try:
            path.unlink()
            # Sidecars go with their snapshot, or a pruned backup leaves a stray
            # -wal behind that a later snapshot of the same name would inherit.
            for suffix in ("-wal", "-shm"):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
            removed.append(path)
        except OSError as exc:
            log.warning("backup_prune_failed", extra={"path": str(path), "error": str(exc)[:200]})
    if removed:
        log.info("backup_pruned", extra={"removed": len(removed), "keep": keep})
    return removed


def upload(taken: Snapshot, r2: R2Settings, *, overwrite: bool = False) -> str | None:
    """Copy a snapshot to S3-compatible storage; returns the key, or ``None``.

    Best-effort by design: the local file is the product, so a failed upload must
    not fail the job that produced it or delete a good snapshot. It is logged at
    warning level instead, because a backup routine that silently stops uploading
    is indistinguishable from no backup routine at all.
    """
    if not r2.is_configured():
        return None
    prefix = (r2.prefix or "").strip("/")
    key = "/".join(part for part in (prefix, REMOTE_FOLDER, taken.path.name) if part)
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            **r2.client_kwargs(),
            config=Config(retries={"max_attempts": 3, "mode": "standard"}),
        )
        extra = {"ContentType": "application/vnd.sqlite3"}
        if not overwrite:
            # upload_file has no "create only" mode, and a snapshot name carries a
            # second-resolution timestamp: a second run in the same second would
            # silently replace the first, so the collision is checked explicitly.
            try:
                client.head_object(Bucket=r2.bucket, Key=key)
                log.warning("backup_upload_exists", extra={"key": key})
                return None
            except Exception:  # noqa: BLE001 - a missing key is the expected path
                pass
        client.upload_file(str(taken.path), r2.bucket, key, ExtraArgs=extra)
    except Exception as exc:  # noqa: BLE001 - boto3 raises ClientError / NoCredentialsError / ImportError
        log.warning(
            "backup_upload_failed",
            extra={"key": key, "error": f"{type(exc).__name__}: {str(exc)[:300]}"},
        )
        return None
    log.info("backup_uploaded", extra={"key": key, "bucket": r2.bucket})
    return key


__all__ = [
    "BACKUP_PREFIX",
    "BACKUP_SUFFIX",
    "DEFAULT_KEEP",
    "BackupError",
    "Rehearsal",
    "Snapshot",
    "backup_name",
    "inspect",
    "missing_rows",
    "prune",
    "rehearse",
    "snapshot",
    "table_counts",
    "upload",
]
