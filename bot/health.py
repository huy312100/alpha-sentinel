"""Cycle-freshness check for the daemon.

``--status`` answers "what does the bot believe right now" and is built for a
human reading a terminal. A container healthcheck needs the other question: *is
the loop still turning?* Those differ in the one case that matters. A bot whose
scan loop died, whose store went read-only, or whose cycle is wedged behind a
hung provider stays perfectly able to print a status summary and exit ``0`` - so
a healthcheck built on status can never fail. The container looks healthy while
nothing is being audited, which is the worst possible failure mode: silent.

This module answers the operational question and stays cheap on purpose: one
``runs`` row and one ``kv`` row, no watchlist, no network, no LLM.

Verdicts:

* **healthy** - a cycle finished inside the window; or a cycle is currently
  running and started inside it; or the process booted inside the window (no
  cycle can have finished yet); or nothing has ever run, because a fresh volume
  must not fail its first healthcheck;
* **unhealthy** - a cycle has been running longer than the window (wedged), or
  the newest finished cycle is older than the window (the loop stopped), or the
  newest cycle that *wrote a verdict* is older than the verdict window (the loop
  is turning but producing nothing).

The *outcome* of a cycle is deliberately not a liveness signal: a provider
outage is reported by the cycle's own error count and logs, and restarting the
container would not fix it - it would only lose in-memory state and re-race the
provider. A failed-but-fresh cycle is logged here, not failed on.

The verdict window is the one exception, and it is deliberately slow: two days by
default, so an afternoon of provider failures stays a log line while a pipeline
that has quietly stopped writing anything becomes a restart. It exists because
empty cycles are normal on a long per-instrument cooldown, and cycle freshness
alone would keep reporting a bot that produces nothing as healthy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bot.engine import BOOT_STATE_KEY
from config.settings import Settings
from core.logging import get_logger
from core.models import utcnow
from storage.hot_store import HotStore

log = get_logger(__name__)

#: A cycle legitimately outlives its interval (retries, payload adaptation, a
#: slow layer), so the window is a multiple of the interval - and never below
#: this floor, or a slow-but-correct provider flaps the container.
MIN_STALE_SECONDS = 1_800
#: Multiple of ``SCAN_INTERVAL_SECONDS`` a cycle may take before it counts as hung.
STALE_INTERVAL_FACTOR = 3


def stale_window_seconds(settings: Settings) -> int:
    """How long a cycle may be missing before the bot is called stalled."""
    configured = int(settings.scanners.health_max_stale_seconds or 0)
    if configured > 0:
        return configured
    interval = max(int(settings.scanners.scan_interval_seconds or 0), 1)
    return max(interval * STALE_INTERVAL_FACTOR, MIN_STALE_SECONDS)


def _parse(value: Any) -> datetime | None:
    """Parse a stored timestamp; ``None`` means "cannot tell", never "old"."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age(reference: datetime | None, now: datetime) -> float | None:
    if reference is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max((now - reference).total_seconds(), 0.0)


@dataclass(slots=True)
class HealthReport:
    """The answer ``--health`` prints, and the reason behind it."""

    healthy: bool
    reason: str
    window_seconds: int
    age_seconds: float | None = None
    status: str = ""
    last_run_at: str | None = None
    booted_at: str | None = None
    #: Which measurement ``window_seconds`` / ``age_seconds`` describe: ``cycle`` for
    #: the scheduler bound, ``verdict`` for the production bound. The two windows age
    #: against different runs, so printing them without naming the one that fired lets
    #: an operator read a verdict window next to a cycle age and see nothing wrong.
    bound: str = "cycle"
    verdict_run_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "reason": self.reason,
            "window_seconds": self.window_seconds,
            "age_seconds": None if self.age_seconds is None else round(self.age_seconds, 1),
            "last_run_status": self.status or None,
            "last_run_at": self.last_run_at,
            "booted_at": self.booted_at,
            "bound": self.bound,
            "verdict_run_at": self.verdict_run_at,
        }

    def format(self) -> str:
        head = "AlphaSentinel health: OK" if self.healthy else "AlphaSentinel health: UNHEALTHY"
        last = f"{self.status or 'unknown'} at {self.last_run_at}" if self.last_run_at else "none"
        age = f"{self.age_seconds:.0f}s" if self.age_seconds is not None else "n/a"
        lines = [head, f"  reason: {self.reason}", f"  window: {self.window_seconds}s ({self.bound})"]
        if self.bound == "verdict":
            # State the cycle without its age: the number that tripped the check
            # belongs to another run, and pairing a verdict window with a cycle age is
            # how a stalled pipeline prints as if it were fine.
            lines.append(f"  last run: {last}")
            lines.append(f"  last verdict: {self.verdict_run_at or 'never'} (age {age})")
        else:
            lines.append(f"  last run: {last} (age {age})")
        lines.append(f"  booted: {self.booted_at or 'unknown'}")
        return "\n".join(lines)


def _booted_at(store: HotStore) -> str | None:
    try:
        value = store.get_state(BOOT_STATE_KEY)
    except Exception as exc:  # noqa: BLE001 - a marker is diagnostic, not load-bearing
        log.warning("health_boot_marker_read_failed", extra={"error": str(exc)[:200]})
        return None
    return str(value) if value else None


def _cycle_report(store: HotStore, settings: Settings, moment: datetime) -> HealthReport:
    """The scheduler's side of health: is a cycle running, or recently finished?"""
    window = stale_window_seconds(settings)

    try:
        latest = store.latest_run()
    except Exception as exc:  # noqa: BLE001 - a locked store is not proof of a dead loop
        # Fail open: SQLite can hold a write lock for a moment, and restarting
        # the container over that would turn a blip into an outage.
        log.warning("health_store_read_failed", extra={"error": str(exc)[:200]})
        return HealthReport(healthy=True, reason="store_unreadable", window_seconds=window)

    booted = _booted_at(store)
    boot_age = _age(_parse(booted), moment)

    if latest is None:
        if boot_age is not None and boot_age > window:
            log.warning("health_no_cycle_since_boot", extra={"booted_at": booted, "window": window})
            return HealthReport(
                healthy=False,
                reason="no_cycle_finished_since_boot",
                window_seconds=window,
                age_seconds=boot_age,
                booted_at=booted,
            )
        return HealthReport(
            healthy=True, reason="no_runs_yet", window_seconds=window, age_seconds=boot_age, booted_at=booted
        )

    status = str(latest.get("status") or "")
    started_at = latest.get("started_at")
    finished_at = latest.get("finished_at")

    if status == "running":
        age = _age(_parse(started_at), moment)
        if age is None:
            # An unreadable timestamp must not flap the container.
            return HealthReport(
                healthy=True, reason="running_start_unparseable", window_seconds=window,
                status=status, last_run_at=started_at, booted_at=booted,
            )
        if age > window:
            log.warning("health_cycle_hung", extra={"started_at": started_at, "age_seconds": round(age, 1)})
            return HealthReport(
                healthy=False, reason="cycle_hung", window_seconds=window, age_seconds=age,
                status=status, last_run_at=started_at, booted_at=booted,
            )
        return HealthReport(
            healthy=True, reason="cycle_running", window_seconds=window, age_seconds=age,
            status=status, last_run_at=started_at, booted_at=booted,
        )

    age = _age(_parse(finished_at or started_at), moment)
    if age is None:
        return HealthReport(
            healthy=True, reason="timestamp_unparseable", window_seconds=window,
            status=status, last_run_at=finished_at or started_at, booted_at=booted,
        )
    if age <= window:
        if status == "failed":
            log.warning("health_last_cycle_failed", extra={"run_id": latest.get("id"), "age_seconds": round(age, 1)})
        return HealthReport(
            healthy=True, reason="cycle_fresh", window_seconds=window, age_seconds=age,
            status=status, last_run_at=finished_at or started_at, booted_at=booted,
        )
    # A daemon that restarted a moment ago has no finished cycle yet; only call
    # it stalled once the boot itself is older than the window.
    if boot_age is not None and boot_age <= window:
        return HealthReport(
            healthy=True, reason="booting", window_seconds=window, age_seconds=boot_age,
            status=status, last_run_at=finished_at or started_at, booted_at=booted,
        )
    log.warning(
        "health_cycle_stale",
        extra={"last_run_at": finished_at or started_at, "age_seconds": round(age, 1), "window": window},
    )
    return HealthReport(
        healthy=False, reason="cycle_stale", window_seconds=window, age_seconds=age,
        status=status, last_run_at=finished_at or started_at, booted_at=booted,
    )


def _productivity_check(
    report: HealthReport,
    store: HotStore,
    settings: Settings,
    moment: datetime,
) -> HealthReport:
    """Downgrade a live-but-unproductive daemon to unhealthy.

    ``cycle_fresh`` is a fact about the scheduler. With a 6-hour cooldown and a
    5-minute interval, almost every cycle legitimately scans nothing, so a daemon
    can keep finishing empty cycles for a week and stay green while producing no
    verdict at all - the exact failure a soak exists to catch, and the one a
    cycle-freshness check cannot see.

    Fails open on a read error, like the cycle check does, and never fires on a
    fresh volume: nothing has had a chance to be produced yet.
    """
    window = int(settings.scanners.health_max_stale_verdict_seconds or 0)
    if window <= 0:
        return report

    try:
        last = store.last_verdict_run()
    except Exception as exc:  # noqa: BLE001 - an unreadable store is not proof of an idle one
        log.warning("health_verdict_read_failed", extra={"error": str(exc)[:200]})
        return report

    if last is None:
        # No run has ever written a verdict. That is normal only while the process is
        # young: once the boot itself has outlasted the window, the same stall is
        # visible from the other side and nothing but the boot marker can say so.
        boot_age = _age(_parse(report.booted_at), moment)
        if boot_age is not None and boot_age > window:
            log.warning(
                "health_no_verdict_since_boot",
                extra={"booted_at": report.booted_at, "age_seconds": round(boot_age, 1), "window": window},
            )
            return HealthReport(
                healthy=False, reason="no_verdict_since_boot", window_seconds=window, age_seconds=boot_age,
                status=report.status, last_run_at=report.last_run_at, booted_at=report.booted_at,
                bound="verdict",
            )
        return report

    age = _age(_parse(last.get("finished_at") or last.get("started_at")), moment)
    if age is not None and age > window:
        log.warning(
            "health_verdicts_stale",
            extra={"last_verdict_run_at": last.get("finished_at") or last.get("started_at"),
                   "age_seconds": round(age, 1), "window": window},
        )
        return HealthReport(
            healthy=False, reason="verdicts_stale", window_seconds=window, age_seconds=age,
            status=report.status, last_run_at=report.last_run_at, booted_at=report.booted_at,
            bound="verdict", verdict_run_at=last.get("finished_at") or last.get("started_at"),
        )
    return report


def evaluate(store: HotStore, settings: Settings, *, now: datetime | None = None) -> HealthReport:
    """Decide whether the daemon is still *producing*, not merely still running.

    Two independent bounds: the cycle window says the loop is turning, the verdict
    window says the loop is accomplishing something. Reporting only the first is how
    a bot that has written nothing for a week reports itself healthy.
    """
    moment = now or utcnow()
    report = _cycle_report(store, settings, moment)
    if not report.healthy:
        return report
    return _productivity_check(report, store, settings, moment)


__all__ = ["HealthReport", "MIN_STALE_SECONDS", "STALE_INTERVAL_FACTOR", "evaluate", "stale_window_seconds"]
