"""Restart containers whose healthcheck has been failing.

``restart: unless-stopped`` restarts a container that *exits*. It does nothing
for a container that keeps running but has stopped working, which is exactly the
failure the healthcheck was written to detect: Docker flips the status to
``unhealthy`` and, with no orchestrator watching it, that status is only a label
in ``docker ps``. A bot wedged behind a hung provider therefore sits there
looking up while producing no cycles, and the dashboard keeps showing last
week's verdicts.

Why this runs on the host rather than as a container: restarting containers from
inside one needs ``/var/run/docker.sock``, and anything holding that socket can
start a privileged container and take the host. A supervisor that guards the
stack should not be able to rewrite it, so this stays a host process with the
same Docker access an operator already has.

Restart budget, not restart loops: a container that is broken in a way a restart
cannot fix - a missing key, a corrupt volume - would otherwise be bounced
forever, which hides the cause and makes the logs useless. After
``--max-restarts`` attempts inside ``--window`` seconds the watchdog stops
restarting, logs ``docker_watchdog_halted`` and keeps observing. It deliberately
does not exit: a systemd ``Restart=always`` would bring it back with a clean
budget and resume bouncing the container.

Portability: this file is stdlib-only and sticks to syntax Python 3.8 accepts,
because the unit that runs it calls ``/usr/bin/python3`` on the host - Debian 11
ships 3.9 and Ubuntu 20.04 ships 3.8. ``@dataclass(slots=True)`` alone would make
the supervisor fail to import on either, which is a poor property for the one
process meant to outlive everything else.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

WAIT = "wait"
RESTART = "restart"
HALT = "halt"

#: ``docker inspect`` template: ``none`` when the container declares no healthcheck,
#: which must not be read as "unhealthy" - an unmonitored container is not a sick one.
HEALTH_TEMPLATE = "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"

log = logging.getLogger("alphasentinel.watchdog")


@dataclass(frozen=True)
class Policy:
    """How much evidence is required before acting, and how often it may act."""

    threshold: int = 3
    max_restarts: int = 3
    window_seconds: float = 3600.0


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str


def next_decision(
    *,
    health: str,
    consecutive_failures: int,
    restarts_within_window: int,
    policy: Policy,
) -> Decision:
    """Decide what to do about one observation.

    ``starting`` is not a failure: it is what a container reports during its
    ``start_period``, and treating it as one would restart every fresh boot
    before the bot had a chance to finish its first cycle.
    """
    if health == "missing":
        return Decision(WAIT, "container_not_found")
    if health != "unhealthy":
        return Decision(WAIT, "healthy" if health == "healthy" else f"not_unhealthy:{health}")
    if consecutive_failures < max(policy.threshold, 1):
        return Decision(WAIT, "confirming")
    if restarts_within_window >= policy.max_restarts:
        return Decision(HALT, "restart_budget_exhausted")
    return Decision(RESTART, "unhealthy")


@dataclass
class WatchState:
    """What has been observed so far; the only state the loop carries."""

    failures: dict[str, int] = field(default_factory=dict)
    restarts: dict[str, list[float]] = field(default_factory=dict)

    def restarts_within(self, name: str, moment: float, window: float) -> int:
        return sum(1 for stamp in self.restarts.get(name, []) if moment - stamp < window)

    def record_restart(self, name: str, moment: float, window: float) -> None:
        stamps = [stamp for stamp in self.restarts.get(name, []) if moment - stamp < window]
        stamps.append(moment)
        self.restarts[name] = stamps


def emit(event: str, **fields: Any) -> None:
    """One JSON object per line on stdout, which is what journald collects."""
    payload = {"event": event, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
    print(json.dumps(payload, default=str), flush=True)


def container_health(name: str, *, runner: Callable[..., Any] = subprocess.run) -> str:
    """``healthy`` / ``unhealthy`` / ``starting`` / ``none`` / ``missing``."""
    try:
        completed = runner(
            ["docker", "inspect", "--format", HEALTH_TEMPLATE, name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        # No docker binary, or it did not answer: not evidence about the container.
        return "missing"
    if getattr(completed, "returncode", 1) != 0:
        return "missing"
    return (getattr(completed, "stdout", "") or "").strip() or "missing"


def restart_container(name: str, *, runner: Callable[..., Any] = subprocess.run) -> bool:
    """``docker restart``, with a timeout that outlives ``stop_grace_period``."""
    try:
        completed = runner(
            ["docker", "restart", "--time", "30", name],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(completed, "returncode", 1) == 0


def tick(
    containers: Sequence[str],
    state: WatchState,
    policy: Policy,
    moment: float,
    *,
    probe: Callable[[str], str] = container_health,
    restart: Callable[[str], bool] = restart_container,
    note: Callable[..., None] = emit,
) -> list[str]:
    """One observation pass; returns the containers that were restarted."""
    restarted: list[str] = []
    for name in containers:
        health = probe(name)
        if health == "unhealthy":
            state.failures[name] = state.failures.get(name, 0) + 1
        else:
            # Any non-unhealthy reading clears the count: a container that
            # recovered on its own must not restart later on stale evidence.
            state.failures[name] = 0

        decision = next_decision(
            health=health,
            consecutive_failures=state.failures.get(name, 0),
            restarts_within_window=state.restarts_within(name, moment, policy.window_seconds),
            policy=policy,
        )

        if decision.action == RESTART:
            ok = restart(name)
            # The attempt is recorded whether or not it worked, so a container
            # that cannot be restarted exhausts its budget and stops being
            # hammered instead of retrying forever.
            state.record_restart(name, moment, policy.window_seconds)
            # A restart that did not cure the problem must not restart again on
            # the next pass: the container gets a full ``threshold`` of fresh
            # observations, which is also how long its start_period needs.
            state.failures[name] = 0
            restarted.append(name)
            note("docker_watchdog_restart", container=name, ok=ok, restarts=len(state.restarts.get(name, [])))
            continue

        note(
            "docker_watchdog_observe",
            container=name,
            health=health,
            failures=state.failures.get(name, 0),
            action=decision.action,
            reason=decision.reason,
        )
        if decision.action == HALT:
            log.error(
                "docker_watchdog_halted: %s has used %s restarts in %ss; leaving it alone",
                name,
                policy.max_restarts,
                int(policy.window_seconds),
            )

    return restarted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchdog",
        description="Restart containers whose Docker healthcheck stays unhealthy.",
    )
    parser.add_argument(
        "--container",
        action="append",
        dest="containers",
        metavar="NAME",
        required=True,
        help="container to watch (repeatable)",
    )
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between passes (default 60)")
    parser.add_argument("--threshold", type=int, default=3, help="consecutive unhealthy reads before acting")
    parser.add_argument("--max-restarts", type=int, default=3, help="restart budget inside --window")
    parser.add_argument("--window", type=float, default=3600.0, help="budget window in seconds (default 3600)")
    parser.add_argument("--once", action="store_true", help="run one pass and exit (cron/CI)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        stream=sys.stderr,
    )
    policy = Policy(
        threshold=args.threshold,
        max_restarts=args.max_restarts,
        window_seconds=args.window,
    )
    state = WatchState()
    stopping = False

    def _stop(*_: Any) -> None:
        nonlocal stopping
        stopping = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):  # pragma: no cover - not the main thread
            pass

    emit("docker_watchdog_started", containers=list(args.containers), policy=asdict(policy))
    while True:
        try:
            tick(args.containers, state, policy, time.monotonic())
        except Exception as exc:  # noqa: BLE001 - the watchdog must outlive a bad pass
            log.warning("watchdog_pass_failed: %s: %s", type(exc).__name__, str(exc)[:300])
        if args.once or stopping:
            break
        # Sleep in slices so SIGTERM is honoured promptly rather than after a
        # whole interval; systemd sends it on every stop and reload.
        deadline = time.monotonic() + max(args.interval, 1.0)
        while not stopping and time.monotonic() < deadline:
            time.sleep(min(1.0, max(deadline - time.monotonic(), 0.0)))
    emit("docker_watchdog_stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())


__all__ = [
    "HALT",
    "Policy",
    "RESTART",
    "WAIT",
    "WatchState",
    "container_health",
    "emit",
    "next_decision",
    "restart_container",
    "tick",
]
