"""High-performance compressed Parquet lake with optional Cloudflare R2 sync.

Layout (Hive-partitioned, one partition per UTC day per dataset)::

    <root>/<dataset>/dt=2026-10-25/part-2026-10-25-1729872000-a1b2c3d4.parquet

Design decisions
----------------
* **Atomic**: every file is written to ``*.tmp`` then ``os.replace``d, so a crash
  mid-write can never leave a truncated Parquet that breaks a full-table read.
* **Idempotent-ish**: optional de-duplication on caller-supplied keys inside a
  batch; :meth:`ParquetStore.compact` performs cross-file de-duplication on the
  same keys (or on every column except ``ingested_at``).
* **Non-blocking**: every public sync method has an ``*_async`` twin that runs in
  a worker thread (``asyncio.to_thread``). The event loop never touches disk.
* **Pushdown**: reads use ``pyarrow.dataset`` so timestamp filters and column
  projection are resolved by Arrow, not by loading everything into pandas.
* **R2/S3 optional**: boto3 is imported lazily; the store works fully offline.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from config.settings import R2Settings
from core.logging import get_logger
from core.models import utcnow

log = get_logger(__name__)

_DATASET_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
PARTITION_COLUMN = "dt"

#: Datasets that exist to train models rather than to operate the bot. A model
#: needs years of history; a dashboard needs weeks. Applying the operational
#: window to these deletes the training corpus silently - nothing breaks until
#: ``ml-train`` starts refusing rows, long after the data is gone.
DEFAULT_DATASET_RETENTION_DAYS: dict[str, int] = {"training_rows": 1095}


def _retention_cutoff(days: int, moment: datetime) -> datetime | None:
    """The instant before which data has expired; ``None`` means keep forever.

    The ``<= 0`` rule lives here and nowhere else. It used to be spelled twice -
    a ``continue`` in the local pass, a ``None`` sentinel in the remote one - and
    a reader checking "does zero really keep this?" had to find both.
    """
    return None if days <= 0 else moment - timedelta(days=days)


class StorageError(RuntimeError):
    """Raised for invalid dataset names, missing credentials or corrupt layout."""


def _is_not_found(exc: Exception) -> bool:
    """True for an S3/boto3 404 (``NoSuchKey``/``NotFound``), false for anything else."""
    response = getattr(exc, "response", None) or {}
    error = response.get("Error") if isinstance(response, dict) else None
    code = str((error or {}).get("Code") or "")
    if code in {"404", "NoSuchKey", "NotFound"}:
        return True
    return getattr(exc, "status", None) == 404


@dataclass(frozen=True, slots=True)
class WriteResult:
    dataset: str
    partition: str
    path: Path
    rows: int
    bytes_written: int
    uploaded: bool = False
    deduplicated: int = 0

    @property
    def empty(self) -> bool:
        return self.rows == 0


@dataclass(frozen=True, slots=True)
class DatasetStat:
    dataset: str
    partition: str
    files: int
    rows: int
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "partition": self.partition,
            "files": self.files,
            "rows": self.rows,
            "bytes": self.bytes,
        }


class ParquetStore:
    """Local Parquet lake + S3-compatible (Cloudflare R2) mirror."""

    def __init__(
        self,
        root: str | Path,
        *,
        r2: R2Settings | None = None,
        compression: str = "zstd",
        row_group_size: int = 262_144,
        local_retention_days: int = 90,
        remote_retention_days: int = 365,
        dataset_retention_days: Mapping[str, int] | None = None,
        timestamp_column: str = "timestamp",
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.r2 = r2
        self.compression = compression
        self.row_group_size = max(int(row_group_size), 1_024)
        self.local_retention_days = local_retention_days
        self.remote_retention_days = remote_retention_days
        # Defaults always apply, so a caller that forgets the argument cannot
        # shorten the ML window. Override a dataset by passing its key, or set
        # it to ``0`` to keep that dataset forever.
        self.dataset_retention_days = {**DEFAULT_DATASET_RETENTION_DAYS, **(dataset_retention_days or {})}
        self.timestamp_column = timestamp_column
        self.auto_upload = bool(r2 and r2.is_configured())
        self._s3: Any = None
        self._s3_lock = threading.Lock()

    # -- paths --------------------------------------------------------------
    @staticmethod
    def _validate_dataset(dataset: str) -> str:
        if not _DATASET_RE.match(dataset or ""):
            raise StorageError(
                f"invalid dataset name {dataset!r}: use lowercase letters, digits and underscores (2-64 chars)"
            )
        return dataset

    def dataset_path(self, dataset: str) -> Path:
        return self.root / self._validate_dataset(dataset)

    def partition_dir(self, dataset: str, partition: str | date | datetime | None = None) -> Path:
        if partition is None:
            partition = utcnow().date()
        key = partition.isoformat() if isinstance(partition, (date, datetime)) else str(partition)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", key):
            raise StorageError(f"partition must be YYYY-MM-DD, got {key!r}")
        return self.dataset_path(dataset) / f"{PARTITION_COLUMN}={key}"

    def partitions(self, dataset: str) -> list[str]:
        base = self.dataset_path(dataset)
        if not base.exists():
            return []
        return sorted(
            child.name.split("=", 1)[1]
            for child in base.iterdir()
            if child.is_dir() and child.name.startswith(f"{PARTITION_COLUMN}=")
        )

    # -- normalisation ------------------------------------------------------
    def _normalise(
        self,
        frame: pd.DataFrame,
        dataset: str,
        timestamp_column: str | None,
        dedupe_keys: Sequence[str] | None,
    ) -> tuple[pd.DataFrame, int, pd.Timestamp]:
        df = frame.copy()
        ts_column = timestamp_column or self.timestamp_column

        if ts_column not in df.columns:
            if self.timestamp_column in df.columns:
                ts_column = self.timestamp_column
            else:
                df[ts_column] = utcnow()
                log.debug("timestamp_column_added", extra={"dataset": dataset, "column": ts_column})

        parsed = pd.to_datetime(df[ts_column], utc=True, errors="coerce")
        parsed = parsed.fillna(pd.Timestamp(utcnow()))
        df[ts_column] = parsed.astype("datetime64[us, UTC]")

        # Normalise every other datetime column Arrow might reject.
        for column in df.columns:
            if column == ts_column:
                continue
            if str(df[column].dtype).startswith("datetime64"):
                df[column] = pd.to_datetime(df[column], utc=True, errors="coerce").astype("datetime64[us, UTC]")

        df["ingested_at"] = pd.Timestamp(utcnow()).as_unit("us")

        deduplicated = 0
        if dedupe_keys:
            present = [key for key in dedupe_keys if key in df.columns]
            if present:
                before = len(df)
                df = df.drop_duplicates(subset=present, keep="last")
                deduplicated = before - len(df)

        df = df.reset_index(drop=True)
        latest = df[ts_column].max()
        if isinstance(latest, pd.Timestamp) and not pd.isna(latest):
            partition_ts = latest.tz_convert("UTC") if latest.tzinfo else latest
        else:
            partition_ts = pd.Timestamp(utcnow())
        return df, deduplicated, partition_ts

    @staticmethod
    def _sanitise_objects(df: pd.DataFrame) -> pd.DataFrame:
        """Arrow cannot store dict/list inside a scalar column - JSON-encode them."""
        for column in df.columns:
            if df[column].dtype != object:
                continue
            non_null = df[column].dropna()
            if non_null.empty:
                continue
            if non_null.map(lambda value: isinstance(value, (dict, list, tuple, set))).any():
                df[column] = df[column].map(
                    lambda value: json.dumps(value, ensure_ascii=False, default=str)
                    if isinstance(value, (dict, list, tuple, set))
                    else value
                )
        return df

    def _write_table(self, table: pa.Table, target: Path) -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".parquet.tmp")
        pq.write_table(
            table,
            tmp,
            compression=self.compression if self.compression != "none" else "none",
            compression_level=3 if self.compression == "zstd" else None,
            row_group_size=self.row_group_size,
            use_dictionary=True,
            write_statistics=True,
        )
        os.replace(tmp, target)
        return target.stat().st_size

    # -- write --------------------------------------------------------------
    def write_frame(
        self,
        dataset: str,
        frame: pd.DataFrame,
        *,
        timestamp_column: str | None = None,
        dedupe_keys: Sequence[str] | None = None,
        partition: str | date | datetime | None = None,
        upload: bool | None = None,
    ) -> WriteResult:
        """Compress ``frame`` into a new Parquet file and return its metadata."""
        self._validate_dataset(dataset)
        if frame is None or frame.empty:
            return WriteResult(dataset, str(partition or utcnow().date()), self.dataset_path(dataset), 0, 0)

        df, deduplicated, partition_ts = self._normalise(frame, dataset, timestamp_column, dedupe_keys)
        partition_key = (
            partition.isoformat() if isinstance(partition, (date, datetime)) else str(partition)
        ) if partition else partition_ts.strftime("%Y-%m-%d")

        df = self._sanitise_objects(df)
        table = pa.Table.from_pandas(df, preserve_index=False)

        filename = f"part-{partition_key}-{int(partition_ts.timestamp())}-{uuid.uuid4().hex[:8]}.parquet"
        target = self.partition_dir(dataset, partition_key) / filename
        size = self._write_table(table, target)

        should_upload = self.auto_upload if upload is None else upload
        uploaded = False
        if should_upload:
            uploaded = self.upload_file(dataset, partition_key, target)

        log.info(
            "parquet_written",
            extra={
                "dataset": dataset,
                "partition": partition_key,
                "rows": len(df),
                "deduplicated": deduplicated,
                "bytes": size,
                "uploaded": uploaded,
                "file": target.name,
            },
        )
        return WriteResult(dataset, partition_key, target, len(df), size, uploaded, deduplicated)

    async def write_frame_async(self, dataset: str, frame: pd.DataFrame, **kwargs: Any) -> WriteResult:
        return await asyncio.to_thread(self.write_frame, dataset, frame, **kwargs)

    # -- read ---------------------------------------------------------------
    def _files_for_range(
        self,
        dataset: str,
        start: datetime | str | None,
        end: datetime | str | None,
    ) -> list[Path]:
        partitions = self.partitions(dataset)
        if not partitions:
            return []
        start_day = pd.Timestamp(start).date().isoformat() if start is not None else None
        end_day = pd.Timestamp(end).date().isoformat() if end is not None else None
        files: list[Path] = []
        for partition in partitions:
            if start_day and partition < start_day:
                continue
            if end_day and partition > end_day:
                continue
            files.extend(sorted(self.partition_dir(dataset, partition).glob("*.parquet")))
        return files

    def _dataset_filter(
        self,
        dset: ds.Dataset,
        start: datetime | str | None,
        end: datetime | str | None,
        filters: dict[str, Any] | None,
    ) -> Any:
        expression = None
        if self.timestamp_column in dset.schema.names:
            field_type = dset.schema.field(self.timestamp_column).type
            field = ds.field(self.timestamp_column)
            if start is not None:
                expression = field >= pa.scalar(pd.Timestamp(start).to_pydatetime(), type=field_type)
            if end is not None:
                upper = field <= pa.scalar(pd.Timestamp(end).to_pydatetime(), type=field_type)
                expression = upper if expression is None else expression & upper
        for column, value in (filters or {}).items():
            if column not in dset.schema.names:
                continue
            clause = ds.field(column) == pa.scalar(value, type=dset.schema.field(column).type)
            expression = clause if expression is None else expression & clause
        return expression

    def read_frame(
        self,
        dataset: str,
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        columns: Sequence[str] | None = None,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Read a time range with predicate/projection pushdown."""
        self._validate_dataset(dataset)
        files = self._files_for_range(dataset, start, end)
        if not files:
            return pd.DataFrame()

        try:
            dset = ds.dataset([str(path) for path in files], format="parquet")
            expression = self._dataset_filter(dset, start, end, filters)
            projection = list(columns) if columns else None
            if limit is not None:
                table = dset.head(limit, columns=projection, filter=expression)
            else:
                table = dset.to_table(columns=projection, filter=expression)
            frame = table.to_pandas()
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError, OSError, ValueError) as exc:
            log.warning("parquet_pushdown_fallback", extra={"dataset": dataset, "error": str(exc)[:300]})
            frame = self._read_files_fallback(files, columns, start, end, filters)

        if limit is not None and len(frame) > limit:
            frame = frame.head(limit)
        return frame.reset_index(drop=True)

    def _read_files_fallback(
        self,
        files: Sequence[Path],
        columns: Sequence[str] | None,
        start: datetime | str | None,
        end: datetime | str | None,
        filters: dict[str, Any] | None,
    ) -> pd.DataFrame:
        frames = []
        for path in files:
            try:
                frame = pd.read_parquet(path, columns=list(columns) if columns else None)
            except (pa.ArrowInvalid, ValueError, OSError):
                frame = pd.read_parquet(path)
                if columns:
                    frame = frame[[column for column in columns if column in frame.columns]]
            frames.append(frame)
        if not frames:
            return pd.DataFrame()
        frame = pd.concat(frames, ignore_index=True)
        if self.timestamp_column in frame.columns:
            stamps = pd.to_datetime(frame[self.timestamp_column], utc=True, errors="coerce")
            if start is not None:
                mask = stamps >= pd.Timestamp(start)
                frame, stamps = frame[mask], stamps[mask]
            if end is not None:
                frame = frame[stamps <= pd.Timestamp(end)]
        for column, value in (filters or {}).items():
            if column in frame.columns:
                frame = frame[frame[column] == value]
        return frame

    async def read_frame_async(self, dataset: str, **kwargs: Any) -> pd.DataFrame:
        return await asyncio.to_thread(self.read_frame, dataset, **kwargs)

    def iter_batches(
        self,
        dataset: str,
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        columns: Sequence[str] | None = None,
        batch_size: int = 65_536,
    ):
        """Stream batches for datasets that do not fit in memory."""
        files = self._files_for_range(dataset, start, end)
        if not files:
            return
        dset = ds.dataset([str(path) for path in files], format="parquet")
        scanner = dset.scanner(
            columns=list(columns) if columns else None,
            filter=self._dataset_filter(dset, start, end, None),
            batch_size=max(batch_size, 1_024),
        )
        for batch in scanner.to_batches():
            yield batch.to_pandas()

    # -- introspection ------------------------------------------------------
    def list_datasets(self) -> list[str]:
        return sorted(
            child.name
            for child in self.root.iterdir()
            if child.is_dir() and _DATASET_RE.match(child.name)
        )

    def dataset_stats(self, dataset: str | None = None) -> list[DatasetStat]:
        stats: list[DatasetStat] = []
        for name in [dataset] if dataset else self.list_datasets():
            for partition in self.partitions(name):
                rows = 0
                size = 0
                files = sorted(self.partition_dir(name, partition).glob("*.parquet"))
                for path in files:
                    try:
                        rows += pq.ParquetFile(path).metadata.num_rows
                        size += path.stat().st_size
                    except (OSError, pa.ArrowInvalid):
                        continue
                stats.append(DatasetStat(name, partition, len(files), rows, size))
        return stats

    def total_size_bytes(self) -> int:
        return sum(stat.bytes for stat in self.dataset_stats())

    # -- maintenance --------------------------------------------------------
    def compact(
        self,
        dataset: str,
        *,
        older_than_days: int = 1,
        dedupe: bool = True,
        dedupe_keys: Sequence[str] | None = None,
    ) -> list[WriteResult]:
        """Merge every small file of a closed partition into one compacted file.

        ``dedupe_keys`` must be the same identity the writer used. Without it the
        comparison falls back to every column except ``ingested_at`` - that column
        is stamped per write, so including it would make a retried write look like
        a new row and leave duplicates in the lake.
        """
        self._validate_dataset(dataset)
        cutoff = (utcnow() - timedelta(days=max(older_than_days, 0))).date().isoformat()
        results: list[WriteResult] = []
        for partition in self.partitions(dataset):
            if partition >= cutoff:
                continue
            files = sorted(self.partition_dir(dataset, partition).glob("*.parquet"))
            if len(files) < 2:
                continue
            frames = [pd.read_parquet(path) for path in files]
            merged = pd.concat(frames, ignore_index=True)
            rows = len(merged)
            if dedupe:
                subset = [key for key in (dedupe_keys or []) if key in merged.columns]
                if not subset:
                    subset = [column for column in merged.columns if column != "ingested_at"]
                merged = merged.drop_duplicates(subset=subset, keep="last").reset_index(drop=True)
            target = (
                self.partition_dir(dataset, partition)
                / f"part-{partition}-{int(pd.Timestamp(utcnow()).timestamp())}-{uuid.uuid4().hex[:8]}.parquet"
            )
            size = self._write_table(pa.Table.from_pandas(merged, preserve_index=False), target)
            uploaded = False
            if self.auto_upload:
                # Upload the replacement *before* deleting anything, then drop the
                # remote parts so a later restore does not contain both copies.
                uploaded = self.upload_file(dataset, partition, target)
                if uploaded:
                    self._delete_remote_files(dataset, partition, files)
            for path in files:
                path.unlink(missing_ok=True)
            results.append(WriteResult(dataset, partition, target, len(merged), size, uploaded, rows - len(merged)))
            log.info(
                "parquet_compacted",
                extra={
                    "dataset": dataset,
                    "partition": partition,
                    "files_in": len(files),
                    "rows": len(merged),
                    "deduplicated": rows - len(merged),
                    "bytes": size,
                    "uploaded": uploaded,
                },
            )
        return results

    def _delete_remote_files(self, dataset: str, partition: str, files: Sequence[Path]) -> None:
        """Best-effort removal of the pre-compaction objects from R2."""
        if not self.r2_ready:
            return
        client = self._client()
        for path in files:
            key = self._remote_key(dataset, partition, path.name)
            try:
                client.delete_object(Bucket=self.r2.bucket, Key=key)
            except Exception as exc:  # noqa: BLE001 - the compacted file is already safe
                log.warning("r2_compact_cleanup_failed", extra={"key": key, "error": type(exc).__name__})

    def delete_partition(self, dataset: str, partition: str) -> bool:
        directory = self.partition_dir(dataset, partition)
        if not directory.exists():
            return False
        for path in directory.glob("*"):
            path.unlink(missing_ok=True)
        directory.rmdir()
        return True

    def retention_days(self, dataset: str, *, remote: bool = False) -> int:
        """Retention window for one dataset, in days (``<= 0`` keeps it forever).

        A per-dataset entry wins over the operational default and governs both
        the local and the remote pass, so a mirrored lake cannot outlive - or be
        outlived by - the copy the dashboard reads.
        """
        override = self.dataset_retention_days.get(dataset)
        if override is not None:
            return int(override)
        return self.remote_retention_days if remote else self.local_retention_days

    def apply_local_retention(self, *, now: datetime | None = None) -> int:
        """Drop local partitions older than each dataset's retention window.

        ``local_retention_days <= 0`` disables the pass outright, overrides
        included: "keep everything" has to mean everything.
        """
        if self.local_retention_days <= 0:
            return 0
        moment = now or utcnow()
        removed = 0
        for dataset in self.list_datasets():
            limit = _retention_cutoff(self.retention_days(dataset), moment)
            if limit is None:
                continue
            cutoff = limit.date().isoformat()
            for partition in self.partitions(dataset):
                if partition < cutoff and self.delete_partition(dataset, partition):
                    removed += 1
        if removed:
            log.info("local_retention_applied", extra={"partitions_removed": removed})
        return removed

    # -- Cloudflare R2 (any S3-compatible endpoint) -------------------------
    def _client(self) -> Any:
        if self.r2 is None or not self.r2.is_configured():
            raise StorageError(
                "R2 is not configured: set R2_ENABLED=true plus R2_ENDPOINT_URL, R2_BUCKET, "
                "R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY"
            )
        if self._s3 is not None:
            return self._s3
        with self._s3_lock:
            if self._s3 is None:
                import boto3
                from botocore.config import Config

                self._s3 = boto3.client(
                    "s3",
                    **self.r2.client_kwargs(),
                    config=Config(retries={"max_attempts": 5, "mode": "standard"}, max_pool_connections=16),
                )
        return self._s3

    @property
    def r2_ready(self) -> bool:
        return bool(self.r2 and self.r2.is_configured())

    def _remote_key(self, dataset: str, partition: str, filename: str) -> str:
        assert self.r2 is not None
        prefix = (self.r2.prefix or "").strip("/")
        parts = [part for part in (prefix, dataset, f"{PARTITION_COLUMN}={partition}", filename) if part]
        return "/".join(parts)

    def upload_file(self, dataset: str, partition: str, path: Path, *, overwrite: bool = True) -> bool:
        if not self.r2_ready:
            return False
        client = self._client()
        key = self._remote_key(dataset, partition, path.name)
        if not overwrite:
            # ``head_object`` raising is the *expected* path for a new key: a 404
            # means "upload it", not "skip the upload". Only a successful head
            # means the object already exists.
            try:
                client.head_object(Bucket=self.r2.bucket, Key=key)
                return False
            except Exception as exc:  # noqa: BLE001 - boto3 raises ClientError
                if not _is_not_found(exc):
                    log.warning(
                        "r2_head_failed",
                        extra={"key": key, "error": f"{type(exc).__name__}: {str(exc)[:200]}"},
                    )
                    return False
        try:
            client.upload_file(
                str(path),
                self.r2.bucket,
                key,
                ExtraArgs={"ContentType": "application/vnd.apache.parquet"},
            )
        except Exception as exc:  # noqa: BLE001 - boto3 raises ClientError / NoCredentialsError
            log.warning(
                "r2_upload_failed",
                extra={"key": key, "error": f"{type(exc).__name__}: {str(exc)[:300]}"},
            )
            return False
        return True

    def upload_partition(self, dataset: str, partition: str, *, overwrite: bool = False) -> int:
        if not self.r2_ready:
            return 0
        uploaded = 0
        for path in sorted(self.partition_dir(dataset, partition).glob("*.parquet")):
            if self.upload_file(dataset, partition, path, overwrite=overwrite):
                uploaded += 1
        return uploaded

    def sync(self, dataset: str | None = None, *, overwrite: bool = False) -> dict[str, int]:
        """Mirror local partitions to R2. Returns counts per action."""
        if not self.r2_ready:
            log.info("r2_sync_skipped", extra={"reason": "not_configured"})
            return {"uploaded": 0, "partitions": 0}
        uploaded = 0
        partitions = 0
        for name in [dataset] if dataset else self.list_datasets():
            for partition in self.partitions(name):
                count = self.upload_partition(name, partition, overwrite=overwrite)
                if count:
                    partitions += 1
                    uploaded += count
        log.info("r2_sync_complete", extra={"dataset": dataset or "*", "uploaded": uploaded, "partitions": partitions})
        return {"uploaded": uploaded, "partitions": partitions}

    def remote_listing(self, dataset: str | None = None) -> list[dict[str, Any]]:
        if not self.r2_ready:
            return []
        prefix_parts = [(self.r2.prefix or "").strip("/")]
        if dataset:
            prefix_parts.append(self._validate_dataset(dataset))
        prefix = "/".join(part for part in prefix_parts if part)
        if prefix:
            # The trailing slash matters: ``alpha-sentinel`` must not match
            # ``alpha-sentinel-staging/...`` in the same bucket.
            prefix += "/"
        client = self._client()
        listing: list[dict[str, Any]] = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.r2.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                listing.append(
                    {
                        "key": item["Key"],
                        "size": int(item.get("Size", 0)),
                        "last_modified": item.get("LastModified"),
                    }
                )
        return listing

    def download(self, dataset: str, partition: str | None = None, *, overwrite: bool = False) -> int:
        """Pull remote partitions back into the local lake (disaster recovery)."""
        if not self.r2_ready:
            return 0
        client = self._client()
        prefix_parts = [(self.r2.prefix or "").strip("/"), self._validate_dataset(dataset)]
        if partition:
            prefix_parts.append(f"{PARTITION_COLUMN}={partition}")
        prefix = "/".join(part for part in prefix_parts if part)
        if prefix:
            prefix += "/"
        downloaded = 0
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.r2.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = item["Key"]
                if not key.endswith(".parquet"):
                    continue
                partition_key = next(
                    (part.split("=", 1)[1] for part in key.split("/") if part.startswith(f"{PARTITION_COLUMN}=")),
                    None,
                )
                if partition_key is None:
                    continue
                target = self.partition_dir(dataset, partition_key) / key.rsplit("/", 1)[-1]
                if target.exists() and not overwrite:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(self.r2.bucket, key, str(target))
                downloaded += 1
        log.info("r2_download_complete", extra={"dataset": dataset, "files": downloaded})
        return downloaded

    def apply_remote_retention(self, *, now: datetime | None = None) -> int:
        """Delete R2 objects older than ``remote_retention_days``.

        A per-dataset override governs this pass too, and ``0`` keeps that
        dataset forever - the same rule the local pass applies.

        Retention is refused without an explicit prefix: an empty prefix lists the
        whole bucket and would delete objects this project does not own. Only
        Parquet files inside a ``dt=`` partition of *this* lake are eligible.

        ``remote_retention_days <= 0`` disables the pass outright, overrides
        included: "keep everything" has to mean everything here as well.
        """
        if not self.r2_ready or self.remote_retention_days <= 0:
            return 0
        prefix = (self.r2.prefix or "").strip("/")
        if not prefix:
            log.error("remote_retention_refused", extra={"reason": "R2_PREFIX is empty"})
            return 0
        client = self._client()
        moment = now or utcnow()
        cutoffs: dict[str, datetime | None] = {}
        removed = 0
        for item in self.remote_listing():
            key = str(item.get("key") or "")
            if not key.startswith(f"{prefix}/"):
                continue
            layout = re.fullmatch(
                rf"([^/]+)/{PARTITION_COLUMN}=(\d{{4}}-\d{{2}}-\d{{2}})/[^/]+\.parquet",
                key[len(prefix) + 1:],
            )
            if layout is None or not _DATASET_RE.fullmatch(layout[1]):
                continue
            try:
                date.fromisoformat(layout[2])
            except ValueError:
                continue
            dataset = layout[1]
            if dataset not in cutoffs:
                cutoffs[dataset] = _retention_cutoff(self.retention_days(dataset, remote=True), moment)
            limit = cutoffs[dataset]
            if limit is None:
                continue
            modified = item.get("last_modified")
            if modified is None or modified >= limit:
                continue
            client.delete_object(Bucket=self.r2.bucket, Key=key)
            removed += 1
        if removed:
            log.info("remote_retention_applied", extra={"objects_removed": removed})
        return removed

    # -- health -------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        stats = self.dataset_stats()
        return {
            "root": str(self.root),
            "writable": os.access(self.root, os.W_OK),
            "datasets": len({stat.dataset for stat in stats}),
            "partitions": len(stats),
            "rows": sum(stat.rows for stat in stats),
            "bytes": sum(stat.bytes for stat in stats),
            "r2_configured": self.r2_ready,
            "compression": self.compression,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ParquetStore(root={self.root!s}, compression={self.compression!r}, r2={self.r2_ready})"
