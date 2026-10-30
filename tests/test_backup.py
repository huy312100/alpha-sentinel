"""Hot-store snapshots: a backup is only worth having if it restores.

These tests hold the two properties that make the module useful. The copy has to
be a *point in time* (not a link, not a torn WAL read), and a snapshot that would
lose rows when restored has to be refused here rather than discovered on the day
someone needs it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from config.settings import R2Settings
from storage.backup import (
    BackupError,
    backup_name,
    inspect,
    missing_rows,
    prune,
    rehearse,
    snapshot,
    upload,
)
from storage.hot_store import SCHEMA_VERSION
from tests.conftest import OTHER_TOKEN


def test_a_snapshot_carries_every_row(store, settings, make_verdict, tmp_path):
    store.record_verdict(make_verdict())
    store.record_verdict(make_verdict(token_address=OTHER_TOKEN))
    store.start_run("scan")

    taken = snapshot(settings.storage.hot_db_path, tmp_path / "copy.db")

    assert taken.tables["verdicts"] == 2
    assert taken.tables["runs"] == 1
    assert taken.schema_version == SCHEMA_VERSION
    assert taken.path.is_file()
    assert taken.size_bytes > 0


def test_a_snapshot_is_one_self_contained_file(store, settings, make_verdict, tmp_path):
    """A backup that needs its -wal to travel with it is not a backup."""
    store.record_verdict(make_verdict())
    folder = tmp_path / "backups"

    taken = snapshot(settings.storage.hot_db_path, folder / "copy.db")

    assert sorted(path.name for path in folder.iterdir()) == [taken.path.name]
    assert inspect(taken.path).tables["verdicts"] == 1


def test_the_snapshot_is_a_point_in_time_not_a_live_view(store, settings, make_verdict, tmp_path):
    """The writer keeps its connection open, exactly as a live scan cycle would."""
    store.record_verdict(make_verdict())
    taken = snapshot(settings.storage.hot_db_path, tmp_path / "copy.db")

    store.record_verdict(make_verdict(token_address=OTHER_TOKEN))

    assert inspect(taken.path).tables["verdicts"] == 1


def test_a_second_snapshot_in_the_same_second_overwrites_cleanly(store, settings, make_verdict, tmp_path):
    """Cron re-runs and two hosts can both produce the same name."""
    target = tmp_path / "copy.db"
    store.record_verdict(make_verdict())
    snapshot(settings.storage.hot_db_path, target)

    later = snapshot(settings.storage.hot_db_path, target)

    # A merged or appended file would show more rows than the source has.
    assert later.tables["verdicts"] == 1


def test_snapshot_refuses_when_there_is_no_database(tmp_path):
    with pytest.raises(BackupError, match="does not exist"):
        snapshot(tmp_path / "absent.db", tmp_path / "copy.db")


def test_a_truncated_file_is_refused(tmp_path):
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all" * 64)

    with pytest.raises(BackupError):
        inspect(broken)


def test_a_path_with_uri_metacharacters_cannot_rewrite_the_access_mode(tmp_path):
    """``as_uri`` escapes ``?``, ``#`` and ``%``, so the path stays a path.

    Added after a reviewer flagged ``sqlite3.connect(uri=True)`` as an injection
    risk: the parameters select an access mode, not SQL, and the encoding means a
    filename cannot contribute any parameter at all. This pins that.
    """
    weird = tmp_path / "we?ird#name%.db"
    conn = sqlite3.connect(weird)
    try:
        conn.execute("CREATE TABLE verdicts (id INTEGER PRIMARY KEY)")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()

    assert inspect(weird).tables == {"verdicts": 0}


def test_a_database_that_opens_but_fails_integrity_check_is_refused(tmp_path):
    """The copy that still opens is the one an operator would trust.

    Built rather than corrupted by byte offset, because the damage has to be one
    that leaves ``sqlite_master`` readable: dropping the index from the schema
    leaves its b-tree pages allocated but unreferenced, so the file opens, the
    tables count, and only ``integrity_check`` says "Page N: never used". A
    corruption that makes the file fail to open would prove nothing about the
    check - SQLite refuses that on its own.
    """
    target = tmp_path / "damaged.db"
    conn = sqlite3.connect(target)
    try:
        conn.execute("CREATE TABLE verdicts (id INTEGER PRIMARY KEY, sym TEXT)")
        conn.executemany("INSERT INTO verdicts (sym) VALUES (?)", [("x" * 50,)] * 300)
        conn.execute("CREATE INDEX idx_sym ON verdicts(sym)")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("DELETE FROM sqlite_master WHERE type='index' AND name='idx_sym'")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(BackupError, match="integrity_check"):
        inspect(target)


def test_a_damaged_btree_page_is_refused_even_though_the_header_is_intact(tmp_path):
    """The other mechanism: SQLite raises before it can report at all."""
    target = tmp_path / "flattened.db"
    conn = sqlite3.connect(target)
    try:
        conn.execute("CREATE TABLE verdicts (id INTEGER PRIMARY KEY, padding TEXT)")
        conn.executemany("INSERT INTO verdicts (padding) VALUES (?)", [("x" * 200,)] * 200)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
    finally:
        conn.close()
    assert pages > 2, "the fixture needs a table b-tree page of its own"

    # Page 2 is the root of the table's b-tree: the header still says the file is
    # fine, so this is the shape of a half-written copy rather than a stray file.
    data = bytearray(target.read_bytes())
    data[page_size:2 * page_size] = b"\x00" * page_size
    target.write_bytes(bytes(data))

    with pytest.raises(BackupError):
        inspect(target)


def test_an_empty_database_is_refused(tmp_path):
    """A zero-length file opens happily in SQLite; it is still not a hot store."""
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()

    with pytest.raises(BackupError, match="schema version"):
        inspect(empty)


def test_a_newer_schema_is_refused_rather_than_half_read(store, settings, make_verdict, tmp_path):
    store.record_verdict(make_verdict())
    taken = snapshot(settings.storage.hot_db_path, tmp_path / "copy.db")
    conn = sqlite3.connect(taken.path)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(BackupError, match="newer than this release"):
        inspect(taken.path)


def test_the_rehearsal_boots_the_copy_and_keeps_every_row(store, settings, make_verdict, tmp_path):
    store.record_verdict(make_verdict())
    store.start_run("scan")
    taken = snapshot(settings.storage.hot_db_path, tmp_path / "copy.db")

    report = rehearse(taken.path)

    assert report.ok, report.problems
    assert report.tables_after == taken.tables
    assert report.schema_version_after == SCHEMA_VERSION
    assert report.problems == []


def test_the_rehearsal_notices_rows_that_would_not_come_back():
    """The rule on its own: a restore may add tables, never lose rows."""
    before = {"verdicts": 2, "runs": 3, "alerts": 0}

    assert missing_rows(before, {"verdicts": 2, "runs": 3, "alerts": 0, "seen_tokens": 0}) == []
    assert missing_rows(before, {"verdicts": 1, "runs": 3, "alerts": 0}) == [
        "table verdicts went from 2 to 1 rows"
    ]
    assert missing_rows(before, {"runs": 3, "alerts": 0}) == ["table verdicts disappeared during migration"]


def test_the_rehearsal_reports_a_copy_that_would_not_boot(store, settings, make_verdict, tmp_path, monkeypatch):
    """A snapshot that opens but cannot be migrated is the dangerous case."""
    store.record_verdict(make_verdict())
    taken = snapshot(settings.storage.hot_db_path, tmp_path / "copy.db")

    def refuse(_self) -> None:
        raise sqlite3.DatabaseError("no such column: asset_class")

    monkeypatch.setattr("storage.backup.HotStore.initialize", refuse)

    report = rehearse(taken.path)

    assert report.ok is False
    assert any("did not initialise" in problem for problem in report.problems)


def test_the_rehearsal_compares_counts_before_and_after_the_migrations(
    store, settings, make_verdict, tmp_path, monkeypatch
):
    """The call site, not just the rule: a restore that loses rows must be refused."""
    store.record_verdict(make_verdict())
    taken = snapshot(settings.storage.hot_db_path, tmp_path / "copy.db")

    def drop_the_rows(target) -> None:
        conn = sqlite3.connect(target.path)
        try:
            conn.execute("DELETE FROM verdicts")
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr("storage.backup.HotStore.initialize", drop_the_rows)

    report = rehearse(taken.path)

    assert report.ok is False
    assert "table verdicts went from 1 to 0 rows" in report.problems


def test_the_rehearsal_does_not_touch_the_snapshot(store, settings, make_verdict, tmp_path):
    store.record_verdict(make_verdict())
    taken = snapshot(settings.storage.hot_db_path, tmp_path / "copy.db")
    before = taken.path.read_bytes()

    rehearse(taken.path)

    assert taken.path.read_bytes() == before


def test_prune_keeps_the_newest_snapshots(tmp_path):
    for day in range(1, 6):
        (tmp_path / f"alpha_sentinel-2026010{day}T000000Z.db").write_bytes(b"x")

    removed = prune(tmp_path, keep=2)

    assert len(removed) == 3
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "alpha_sentinel-20260104T000000Z.db",
        "alpha_sentinel-20260105T000000Z.db",
    ]


def test_prune_deletes_nothing_when_asked_to_keep_nothing(tmp_path):
    """``keep=0`` is a typo in a cron flag, not an instruction to delete the lot."""
    for day in range(1, 4):
        (tmp_path / f"alpha_sentinel-2026010{day}T000000Z.db").write_bytes(b"x")

    assert prune(tmp_path, keep=0) == []
    assert len(list(tmp_path.iterdir())) == 3


def test_prune_leaves_unrelated_files_alone(tmp_path):
    (tmp_path / "alpha_sentinel-20260101T000000Z.db").write_bytes(b"x")
    (tmp_path / "alpha_sentinel-20260102T000000Z.db").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("operator notes", encoding="utf-8")

    prune(tmp_path, keep=1)

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "alpha_sentinel-20260102T000000Z.db",
        "notes.txt",
    ]


def test_prune_takes_the_wal_sidecars_of_what_it_deletes(tmp_path):
    """A stray -wal beside a pruned name would be inherited by the next snapshot."""
    kept = tmp_path / "alpha_sentinel-20260102T000000Z.db"
    doomed = tmp_path / "alpha_sentinel-20260101T000000Z.db"
    for base in (kept, doomed):
        base.write_bytes(b"x")
        base.with_name(base.name + "-wal").write_bytes(b"w")
        base.with_name(base.name + "-shm").write_bytes(b"s")

    prune(tmp_path, keep=1)

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "alpha_sentinel-20260102T000000Z.db",
        "alpha_sentinel-20260102T000000Z.db-shm",
        "alpha_sentinel-20260102T000000Z.db-wal",
    ]


def test_snapshot_names_sort_chronologically():
    start = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
    names = [backup_name(start + timedelta(seconds=offset)) for offset in (0, 60, 3600)]

    assert names == sorted(names)
    assert names[0].endswith("Z.db")


def test_upload_is_a_no_op_when_r2_is_not_configured(store, settings, make_verdict, tmp_path):
    """No bucket configured must not be an error: local backups are the product."""
    store.record_verdict(make_verdict())
    taken = snapshot(settings.storage.hot_db_path, tmp_path / "copy.db")

    assert upload(taken, R2Settings()) is None
