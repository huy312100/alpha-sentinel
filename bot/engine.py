"""The scan cycle: collect -> audit -> persist -> alert.

This is the only place where the deterministic layers, the LLMs, the two stores
and the alert channel meet. The ordering matters and is intentional:

1. **collect** - :class:`scanners.pipeline.ScanPipeline` fans out the layers and
   attaches the security screen + wash-trading read (no LLM tokens spent yet);
2. **audit** - :class:`ai_engine.dual_audit_flow.DualAuditFlow` applies the
   deterministic risk floor and the two-agent audit; the verdict is final here;
3. **persist** - SQLite for "what do I look at right now", Parquet for history;
4. **alert** - only approved/vetoed verdicts that clear the policy threshold.

One token failing can never abort a cycle: errors are captured per token, counted
in the run record and surfaced in the dashboard.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

import pandas as pd

from ai_engine.dual_audit_flow import AuditOutcome, DualAuditFlow
from bot.alerts import AlertResult, TelegramAlerter
from bot.watchlist import WatchToken, WatchlistBundle, WatchlistLoader
from config.settings import Settings
from core.logging import get_logger
from core.models import FinalVerdict, VerdictStatus, iso, json_safe, utcnow
from scanners.pipeline import CollectedSnapshot, ScanPipeline
from storage.hot_store import HotStore
from storage.parquet_store import ParquetStore, StorageError

log = get_logger(__name__)

VERDICTS_DATASET = "verdicts"
SIGNALS_DATASET = "layer_signals"
RUNS_DATASET = "runs"


@dataclass(slots=True)
class TokenResult:
    """Everything that happened to one watchlist entry in this cycle."""

    token: WatchToken
    outcome: AuditOutcome | None = None
    collected: CollectedSnapshot | None = None
    alert: AlertResult | None = None
    skipped: bool = False
    skip_reason: str = ""
    error: str | None = None

    @property
    def verdict(self) -> FinalVerdict | None:
        return self.outcome.verdict if self.outcome else None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.skipped

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token.token_address,
            "chain": self.token.chain,
            "asset_class": self.token.asset_class.value,
            "symbol": self.token.symbol,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "error": self.error,
            "alert": self.alert.to_dict() if self.alert else None,
            "verdict": json_safe(self.verdict.to_dict()) if self.verdict else None,
        }


@dataclass(slots=True)
class ScanReport:
    """Cycle summary - the object the CLI prints and the bot logs."""

    run_id: str
    started_at: Any = field(default_factory=utcnow)
    finished_at: Any = None
    duration_ms: float = 0.0
    requested: int = 0
    skipped: int = 0
    collected: int = 0
    approved: int = 0
    watch: int = 0
    rejected: int = 0
    vetoed: int = 0
    errors: int = 0
    alerts_sent: int = 0
    results: list[TokenResult] = field(default_factory=list)
    health: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def verdicts(self) -> int:
        return self.approved + self.watch + self.rejected + self.vetoed

    def verdicts_by_status(self) -> dict[str, int]:
        return {
            VerdictStatus.APPROVED.value: self.approved,
            VerdictStatus.WATCH.value: self.watch,
            VerdictStatus.REJECTED.value: self.rejected,
            VerdictStatus.VETOED.value: self.vetoed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at) if self.finished_at else None,
            "duration_ms": round(self.duration_ms, 1),
            "requested": self.requested,
            "skipped": self.skipped,
            "collected": self.collected,
            "verdicts": self.verdicts,
            "by_status": self.verdicts_by_status(),
            "errors": self.errors,
            "alerts_sent": self.alerts_sent,
            "dry_run": self.dry_run,
            "health": self.health,
            "results": [result.to_dict() for result in self.results],
        }


class ScanEngine:
    """Owns the pipeline, the audit flow and the stores for the life of a process."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: HotStore | None = None,
        cold: ParquetStore | None = None,
        pipeline: ScanPipeline | None = None,
        audit: DualAuditFlow | None = None,
        alerter: TelegramAlerter | None = None,
        loader: WatchlistLoader | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or HotStore(settings.storage.hot_db_path)
        self.cold = cold or ParquetStore(
            settings.storage.parquet_root,
            r2=settings.storage.r2,
            compression=settings.storage.compression,
            row_group_size=settings.storage.row_group_size,
            local_retention_days=settings.storage.local_retention_days,
            remote_retention_days=settings.storage.remote_retention_days,
        )
        self.pipeline = pipeline
        self.audit = audit
        self.alerter = alerter or TelegramAlerter(
            settings.telegram, endpoints=settings.endpoints, store=self.store
        )
        self.loader = loader or WatchlistLoader(
            settings.scanners.watchlist_path, default_chain=settings.runtime.default_chain
        )
        self._owns_pipeline = pipeline is None
        self._owns_audit = audit is None
        self._started = False
        self._stop = asyncio.Event()
        self._last_maintenance: float = 0.0

    # -- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> "ScanEngine":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._started:
            return
        self.store.initialize()
        if self.pipeline is None:
            self.pipeline = ScanPipeline(self.settings)
            self._owns_pipeline = True
        await self.pipeline.__aenter__()
        if self.audit is None:
            self.audit = DualAuditFlow(self.settings)
            self._owns_audit = True
        await self.audit.__aenter__()
        self._started = True
        log.info("scan_engine_started", extra=self.settings.redacted_summary())

    async def close(self) -> None:
        self._stop.set()
        if self.audit is not None and self._owns_audit:
            await self.audit.close()
        if self.pipeline is not None and self._owns_pipeline:
            await self.pipeline.close()
        if self.alerter is not None:
            await self.alerter.close()
        self.store.close()
        self.audit = None if self._owns_audit else self.audit
        self.pipeline = None if self._owns_pipeline else self.pipeline
        self._started = False
        log.info("scan_engine_closed")

    def stop(self) -> None:
        """Ask :meth:`run_forever` to finish the current cycle and exit."""
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # -- target selection ---------------------------------------------------
    def select_targets(
        self,
        *,
        watchlists: Sequence[str] | None = None,
        limit: int | None = None,
        force: bool = False,
    ) -> tuple[list[WatchToken], list[TokenResult]]:
        """Apply the per-cycle cap and the cooldown filter."""
        bundle: WatchlistBundle = self.loader.load()
        targets = bundle.capped(watchlists, limit)
        due: list[WatchToken] = []
        skipped: list[TokenResult] = []
        for token in targets:
            if force or self.store.is_due(
                token.token_address,
                token.chain,
                token.cooldown_hours,
                asset_class=token.asset_class.value,
            ):
                due.append(token)
            else:
                skipped.append(TokenResult(token=token, skipped=True, skip_reason="cooldown"))
        log.info(
            "targets_selected",
            extra={
                "configured": len(bundle),
                "capped": len(targets),
                "due": len(due),
                "cooling_down": len(skipped),
                "watchlists": list(watchlists or bundle.names()),
            },
        )
        return due, skipped

    # -- cycle --------------------------------------------------------------
    async def run_cycle(  # noqa: C901 - one explicit, auditable sequence
        self,
        *,
        watchlists: Sequence[str] | None = None,
        limit: int | None = None,
        force: bool = False,
        dry_run: bool = False,
        tokens: Sequence[WatchToken] | None = None,
    ) -> ScanReport:
        """Run one full cycle. Always returns a report; never raises on token errors."""
        if not self._started:
            raise RuntimeError("ScanEngine.start() must be awaited before run_cycle()")
        assert self.pipeline is not None and self.audit is not None

        started = time.perf_counter()
        # Dry runs persist nothing - not even the run row.
        report = ScanReport(
            run_id=uuid.uuid4().hex if dry_run else self.store.start_run("scan"),
            dry_run=dry_run,
        )

        try:
            skipped: list[TokenResult] = []
            if tokens is not None:
                targets = list(tokens)
            else:
                try:
                    targets, skipped = self.select_targets(watchlists=watchlists, limit=limit, force=force)
                except Exception as exc:  # noqa: BLE001 - a bad watchlist must not orphan the run
                    log.error(
                        "target_selection_failed",
                        extra={"error": f"{type(exc).__name__}: {str(exc)[:200]}"},
                    )
                    report.errors += 1
                    targets, skipped = [], []
            report.requested = len(targets) + len(skipped)
            report.skipped = len(skipped)
            report.results.extend(skipped)

            verdicts: list[FinalVerdict] = []
            signals: list[dict[str, Any]] = []

            if targets:
                try:
                    collected = await self.pipeline.collect_many([token.to_snapshot() for token in targets])
                except Exception as exc:  # noqa: BLE001 - report the failure instead of crashing
                    log.error(
                        "batch_collection_failed",
                        extra={"error": f"{type(exc).__name__}: {str(exc)[:200]}"},
                    )
                    report.errors += 1
                    collected = []
                report.collected = len(collected)
                by_key: dict[tuple[str, str, str], CollectedSnapshot] = {
                    (item.snapshot.asset_class, item.snapshot.token_address, item.snapshot.chain): item
                    for item in collected
                }

                for token in targets:
                    snapshot_result = by_key.get(token.key)
                    if snapshot_result is None:
                        report.errors += 1
                        report.results.append(TokenResult(token=token, error="collection_failed"))
                        continue

                    result = TokenResult(token=token, collected=snapshot_result)
                    signals.extend(self._signal_rows(snapshot_result))
                    try:
                        result.outcome = await self.audit.audit_detailed(snapshot_result.snapshot)
                    except Exception as exc:  # noqa: BLE001 - isolate one token
                        result.error = f"{type(exc).__name__}: {str(exc)[:200]}"
                        report.errors += 1
                        report.results.append(result)
                        log.error("audit_failed", extra={"token": token.token_address, "error": result.error})
                        continue

                    verdict = result.outcome.verdict
                    verdicts.append(verdict)
                    self._count(report, verdict)
                    report.results.append(result)

                    if not dry_run:
                        self._persist_verdict(verdict)
                        if result.outcome.degraded:
                            report.errors += 1
                        try:
                            result.alert = await self.alerter.send_verdict(
                                verdict, reasons=result.outcome.veto_reasons
                            )
                        except Exception as exc:  # noqa: BLE001 - delivery is best-effort
                            report.errors += 1
                            log.error(
                                "alert_delivery_failed",
                                extra={
                                    "token": verdict.token_address,
                                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                                },
                            )
                        if result.alert and result.alert.sent:
                            report.alerts_sent += 1

            report.finished_at = utcnow()
            report.duration_ms = (time.perf_counter() - started) * 1000
            report.health = self.pipeline.health_report()

            if not dry_run:
                self._write_cold(verdicts, signals, report)
            log.info("cycle_finished", extra=report.to_dict() | {"results": len(report.results)})
            return report
        finally:
            if not dry_run:
                if report.finished_at is None:
                    report.finished_at = utcnow()
                    report.duration_ms = (time.perf_counter() - started) * 1000
                self._finish_run(report)

    @staticmethod
    def _count(report: ScanReport, verdict: FinalVerdict) -> None:
        if verdict.status is VerdictStatus.APPROVED:
            report.approved += 1
        elif verdict.status is VerdictStatus.WATCH:
            report.watch += 1
        elif verdict.status is VerdictStatus.REJECTED:
            report.rejected += 1
        else:
            report.vetoed += 1

    def _persist_verdict(self, verdict: FinalVerdict) -> None:
        try:
            self.store.record_verdict(verdict)
            self.store.mark_seen(verdict.token_address, verdict.chain, asset_class=verdict.asset_class)
        except Exception as exc:  # noqa: BLE001 - storage hiccups must not kill the cycle
            log.error(
                "verdict_persist_failed",
                extra={"token": verdict.token_address, "error": f"{type(exc).__name__}: {str(exc)[:200]}"},
            )

    @staticmethod
    def _signal_rows(collected: CollectedSnapshot) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for signal in collected.snapshot.signals:
            rows.append(
                {
                    "token_address": collected.snapshot.token_address,
                    "chain": collected.snapshot.chain,
                    "asset_class": collected.snapshot.asset_class,
                    "symbol": collected.snapshot.symbol,
                    "layer": signal.layer.value,
                    "status": signal.status.value,
                    "score": round(float(signal.score), 2),
                    "confidence": round(float(signal.confidence), 3),
                    "weight": round(float(signal.weight), 3),
                    "summary": signal.summary[:500],
                    "flags": ",".join(signal.flags[:10]),
                    "latency_ms": round(float(signal.latency_ms), 1),
                    "observed_at": signal.observed_at,
                }
            )
        return rows

    def _write_cold(
        self,
        verdicts: list[FinalVerdict],
        signals: list[dict[str, Any]],
        report: ScanReport,
    ) -> None:
        """Mirror the cycle into Parquet. Failure here is loud but never fatal."""
        try:
            if verdicts:
                frame = pd.DataFrame([verdict.to_record() for verdict in verdicts])
                self.cold.write_frame(
                    VERDICTS_DATASET,
                    frame,
                    timestamp_column="decided_at",
                    dedupe_keys=("token_address", "chain", "asset_class", "decided_at"),
                )
            if signals:
                self.cold.write_frame(
                    SIGNALS_DATASET,
                    pd.DataFrame(signals),
                    timestamp_column="observed_at",
                    dedupe_keys=("token_address", "chain", "asset_class", "layer", "observed_at"),
                )
            run_row = pd.DataFrame(
                [
                    {
                        "run_id": report.run_id,
                        "mode": "dry-run" if report.dry_run else "scan",
                        "status": "completed",
                        "started_at": report.started_at,
                        "finished_at": report.finished_at,
                        "requested": report.requested,
                        "collected": report.collected,
                        "verdicts": report.verdicts,
                        "approved": report.approved,
                        "vetoed": report.vetoed,
                        "errors": report.errors,
                        "alerts_sent": report.alerts_sent,
                        "duration_ms": round(report.duration_ms, 1),
                    }
                ]
            )
            self.cold.write_frame(RUNS_DATASET, run_row, timestamp_column="finished_at")
        except (StorageError, ValueError, OSError, ImportError) as exc:
            log.error(
                "cold_store_write_failed",
                extra={"error": f"{type(exc).__name__}: {str(exc)[:300]}", "run_id": report.run_id},
            )

    def _finish_run(self, report: ScanReport) -> None:
        status = "completed"
        if report.errors and not report.verdicts:
            status = "failed"
        elif report.errors or report.skipped:
            status = "partial"
        try:
            self.store.finish_run(
                report.run_id,
                status=status,
                tokens_scanned=report.collected,
                verdicts_written=report.verdicts,
                approved=report.approved,
                vetoed=report.vetoed,
                errors=report.errors,
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping
            log.error("run_finish_failed", extra={"run_id": report.run_id, "error": str(exc)[:200]})

    # -- maintenance --------------------------------------------------------
    def maintenance(self, *, force: bool = False) -> dict[str, Any]:
        """Trim the hot DB and the Parquet lake; safe to call repeatedly."""
        now = time.monotonic()
        interval = self.settings.scanners.maintenance_interval_seconds
        if not force and self._last_maintenance and (now - self._last_maintenance) < interval:
            return {"skipped": True}
        self._last_maintenance = now
        result: dict[str, Any] = {"skipped": False}
        try:
            result["stale_runs"] = self.store.fail_stale_runs()
            result["pruned"] = self.store.prune(verdict_days=30, alert_days=90)
        except Exception as exc:  # noqa: BLE001
            result["hot_error"] = str(exc)[:200]
        try:
            result["partitions_removed"] = self.cold.apply_local_retention()
            if self.cold.r2_ready:
                result["remote_partitions_removed"] = self.cold.apply_remote_retention()
        except (StorageError, OSError, ValueError) as exc:
            result["cold_error"] = str(exc)[:200]
        log.info("maintenance_finished", extra=result)
        return result

    # -- daemon -------------------------------------------------------------
    async def run_forever(
        self,
        *,
        watchlists: Sequence[str] | None = None,
        limit: int | None = None,
        force: bool = False,
        max_cycles: int | None = None,
    ) -> int:
        """Loop until :meth:`stop` is called. Returns the number of cycles run."""
        interval = max(self.settings.scanners.scan_interval_seconds, 1)
        cycles = 0
        while not self._stop.is_set():
            try:
                report = await self.run_cycle(watchlists=watchlists, limit=limit, force=force)
                log.info(
                    "cycle_summary",
                    extra={
                        "run_id": report.run_id,
                        "verdicts": report.verdicts,
                        "approved": report.approved,
                        "vetoed": report.vetoed,
                        "errors": report.errors,
                        "duration_ms": round(report.duration_ms, 1),
                    },
                )
            except asyncio.CancelledError:  # pragma: no cover - graceful shutdown
                raise
            except Exception as exc:  # noqa: BLE001 - a daemon must not die on one bad cycle
                log.exception("cycle_crashed", extra={"error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            try:
                self.maintenance()
            except Exception as exc:  # noqa: BLE001
                log.warning("maintenance_error", extra={"error": str(exc)[:200]})

            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            if self._stop.is_set():
                break
            log.info("cycle_sleeping", extra={"seconds": interval})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
            break
        return cycles


__all__ = [
    "RUNS_DATASET",
    "SIGNALS_DATASET",
    "VERDICTS_DATASET",
    "ScanEngine",
    "ScanReport",
    "TokenResult",
]
