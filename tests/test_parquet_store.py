"""Parquet lake: idempotent compaction, honest uploads and scoped retention."""

from __future__ import annotations

import pandas as pd
import pytest

from config.settings import R2Settings, SecretStr
from storage.parquet_store import ParquetStore
from core.models import utcnow
from datetime import timedelta


class _NotFound(Exception):
    response = {"Error": {"Code": "404"}}


class _Paginator:
    def __init__(self, client: "FakeS3") -> None:
        self.client = client

    def paginate(self, Bucket, Prefix):  # noqa: N803 - boto3 kwargs
        self.client.prefixes.append(Prefix)
        yield {"Contents": list(self.client.listing)}


class FakeS3:
    """Minimal S3 surface: existence checks, uploads, deletes and listings."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}
        self.uploaded: list[str] = []
        self.deleted: list[str] = []
        self.prefixes: list[str] = []
        self.listing: list[dict] = []

    def head_object(self, Bucket, Key):  # noqa: N803 - boto3 kwargs
        if Key in self.objects:
            return {"ContentLength": 1}
        raise _NotFound(f"404 for {Key}")

    def upload_file(self, filename, bucket, key, ExtraArgs=None):  # noqa: N803
        self.uploaded.append(key)
        self.objects[key] = filename

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _Paginator(self)


def r2(prefix: str = "alpha-sentinel") -> R2Settings:
    return R2Settings(
        enabled=True,
        endpoint_url="https://example.r2.cloudflarestorage.com",
        bucket="bucket",
        access_key_id=SecretStr("key"),
        secret_access_key=SecretStr("secret"),
        prefix=prefix,
    )


@pytest.fixture()
def remote(tmp_path):
    store = ParquetStore(
        tmp_path / "lake",
        r2=r2(),
        compression="snappy",
        local_retention_days=0,
        remote_retention_days=365,
    )
    fake = FakeS3()
    store._s3 = fake
    return store, fake


def write(store: ParquetStore, row_id: int, *, upload: bool = False):
    frame = pd.DataFrame(
        [{"id": row_id, "ts": pd.Timestamp("2020-01-01T00:00:00Z"), "value": row_id * 10}]
    )
    return store.write_frame(
        "events",
        frame,
        timestamp_column="ts",
        dedupe_keys=("id",),
        partition="2020-01-01",
        upload=upload,
    )


def test_compact_dedupes_across_files_despite_fresh_ingested_at(tmp_path):
    store = ParquetStore(tmp_path / "lake", compression="snappy")
    write(store, 1)
    write(store, 1)  # retried write: same identity, different ingested_at
    write(store, 2)

    results = store.compact("events", older_than_days=1, dedupe_keys=("id",))

    assert len(results) == 1
    assert results[0].rows == 2
    assert len(store.read_frame("events")) == 2


def test_compact_uses_ingested_at_free_fallback_without_keys(tmp_path):
    store = ParquetStore(tmp_path / "lake", compression="snappy")
    write(store, 1)
    write(store, 1)

    results = store.compact("events", older_than_days=1)

    assert results[0].rows == 1


def test_upload_without_overwrite_uploads_missing_keys(remote):
    store, fake = remote
    result = write(store, 1)
    assert fake.uploaded == []

    assert store.upload_file("events", "2020-01-01", result.path, overwrite=False) is True
    assert fake.uploaded == ["alpha-sentinel/events/dt=2020-01-01/" + result.path.name]
    # A second call sees the object and leaves it alone.
    assert store.upload_file("events", "2020-01-01", result.path, overwrite=False) is False


def test_compact_uploads_replacement_then_removes_old_remote_parts(remote):
    store, fake = remote
    first, second = write(store, 1), write(store, 2)

    store.compact("events", older_than_days=1, dedupe_keys=("id",))

    assert len(fake.uploaded) == 1
    assert set(fake.deleted) == {
        "alpha-sentinel/events/dt=2020-01-01/" + first.path.name,
        "alpha-sentinel/events/dt=2020-01-01/" + second.path.name,
    }
    assert len(fake.objects) == 1  # only the compacted file remains


def test_remote_listing_prefix_is_namespaced(remote):
    store, fake = remote
    fake.listing = [{"Key": "alpha-sentinel/events/dt=2020-01-01/x.parquet", "Size": 1}]
    assert store.remote_listing("events")
    assert fake.prefixes == ["alpha-sentinel/events/"]


def test_remote_retention_only_deletes_own_parquet_partitions(remote):
    store, fake = remote
    old = utcnow() - timedelta(days=400)
    fake.listing = [
        {"Key": "alpha-sentinel/events/dt=2020-01-01/old.parquet", "LastModified": old},
        {"Key": "alpha-sentinel/events/dt=2020-01-01/notes.txt", "LastModified": old},
        {"Key": "alpha-sentinel-staging/events/dt=2020-01-01/old.parquet", "LastModified": old},
        {"Key": "alpha-sentinel/events/loose.parquet", "LastModified": old},
        {"Key": "alpha-sentinel/events/dt=not-a-date/old.parquet", "LastModified": old},
        {"Key": "alpha-sentinel/events/dt=2020-02-30/old.parquet", "LastModified": old},
        {"Key": "alpha-sentinel/events/dt=2020-01-01/nested/old.parquet", "LastModified": old},
        {"Key": "alpha-sentinel/backups/events/dt=2020-01-01/old.parquet", "LastModified": old},
        {"Key": "alpha-sentinel/INVALID/dt=2020-01-01/old.parquet", "LastModified": old},
        {"Key": "alpha-sentinel/events/dt=2026-01-01/new.parquet", "LastModified": utcnow()},
    ]

    assert store.apply_remote_retention(now=utcnow()) == 1
    assert fake.deleted == ["alpha-sentinel/events/dt=2020-01-01/old.parquet"]


@pytest.mark.parametrize("prefix", ["", "/", "///"])
def test_remote_retention_refuses_an_empty_prefix(tmp_path, prefix):
    store = ParquetStore(tmp_path / "lake", r2=r2(prefix), remote_retention_days=365)
    fake = FakeS3()
    store._s3 = fake
    fake.listing = [{"Key": "someone-else/objects/dt=2020-01-01/x.parquet", "LastModified": utcnow() - timedelta(days=400)}]

    assert store.apply_remote_retention(now=utcnow()) == 0
    assert fake.deleted == []
