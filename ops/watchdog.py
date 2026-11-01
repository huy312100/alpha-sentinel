"""Restart whatever has stopped working, and keep its logs off the disk.

``restart: unless-stopped`` restarts a container that *exits*. It does nothing
for a container that keeps running but has stopped working, which is exactly the
failure the healthcheck was written to detect: Docker flips the status to
``unhealthy`` and, with no orchestrator watching it, that status is only a label
in ``docker ps``. A bot wedged behind a hung provider therefore sits there
looking up while producing no cycles, and the dashboard keeps showing last
week's verdicts.

macOS has the same hole with different plumbing: a LaunchAgent with ``KeepAlive``
relaunches a process that dies, and a wedged ``run_forever`` loop never dies. So
this is one decision core behind two backends - ``docker`` (inspect + restart) and
``launchd`` (ask the app itself, then ``kickstart -k``). The threshold, the
per-target state and the restart budget are shared; only the probe and the action
differ.

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
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

WAIT = "wait"
RESTART = "restart"
HALT = "halt"

#: ``docker inspect`` template: ``none`` when the container declares no healthcheck,
#: which must not be read as "unhealthy" - an unmonitored container is not a sick one.
HEALTH_TEMPLATE = "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"

#: The repository root, so the launchd health probe works whatever the cwd is.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Launch the app's own freshness check. ``--health`` exits 0 when a cycle finished
#: inside the window, 1 when the loop stopped or hung, and 2 on bad usage/config.
#: Overridable by ``--health-command``, which is an operator argument at exactly the
#: trust level of the unit file's own ExecStart: this script's job is to run a
#: command and act on its exit code, so a flag that changes the command is the
#: feature, not an input to be sanitised. Nothing untrusted writes a plist.
DEFAULT_HEALTH_COMMAND = f"{sys.executable} -m bot.main --health"

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


def launchd_health(
    name: str,
    *,
    command: Sequence[str] = (),
    app_dir: Path = PROJECT_ROOT,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = 90.0,
) -> str:
    """Translate ``bot.main --health`` into a status word.

    The app already answers the only question worth asking - did a cycle finish
    recently? - and answers it with its exit code, so this adds no new judgement.
    ``name`` is ignored: freshness is a property of the one scan loop, not of the
    label being restarted, and it is accepted so both backends share one signature.

    Anything outside 0 and 1 is ``missing``, never ``unhealthy``. Exit 2 means the
    environment itself is refused - a missing key, a broken catalogue - and
    restarting cannot fix that, so treating it as a sick process would bounce the
    job forever while the real cause waits to be read in the log.
    """
    try:
        completed = runner(list(command), capture_output=True, text=True, cwd=str(app_dir), timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return "missing"
    code = getattr(completed, "returncode", 2)
    if code == 0:
        return "healthy"
    if code == 1:
        return "unhealthy"
    return "missing"


def launchd_restart(
    label: str,
    *,
    uid: int | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> bool:
    """``launchctl kickstart -k`` - kill the job and let launchd bring it back.

    ``-k`` is the whole point: a wedged loop is not going to exit on its own, and
    without it kickstart would only start a second instance. The ``gui`` domain is
    used because these are LaunchAgents in the user session, not system daemons.
    """
    domain = f"gui/{uid if uid is not None else os.getuid()}"
    try:
        completed = runner(
            ["launchctl", "kickstart", "-k", f"{domain}/{label}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(completed, "returncode", 1) == 0


def bound_log(path: str | Path, max_bytes: int) -> int:
    """Keep a launchd-written log inside ``max_bytes`` by retaining its tail.

    launchd appends to ``StandardOutPath`` forever, so the hole that compose's
    ``max-size``/``max-file`` closed is open again on macOS - and it opens on
    exactly the run whose whole purpose is being left alone.

    The file is rewritten **in place**, never replaced: launchd holds the inode
    open, so swapping the file would leave it appending to a deleted inode while
    the replacement stopped growing. Losing a few lines to that race is acceptable
    for a log; silently losing every future line is not.
    """
    target = Path(path).expanduser()
    try:
        size = target.stat().st_size
    except OSError:
        return 0
    if max_bytes <= 0 or size <= max_bytes:
        return 0
    keep = max(max_bytes // 2, 1)
    offset = max(size - keep, 0)
    with target.open("rb") as handle:
        handle.seek(offset)
        tail = handle.read()
    # Drop the leading partial line, or the file would open mid-sentence forever -
    # but only when it *is* partial. The byte before the offset decides: a newline
    # there means the read began on a line boundary and that first line is whole,
    # which is the newest-but-one record an operator would go looking for.
    if offset > 0:
        with target.open("rb") as handle:
            handle.seek(offset - 1)
            boundary = handle.read(1)
        if boundary != b"\n":
            newline = tail.find(b"\n")
            tail = tail[newline + 1:] if newline != -1 else b""
    notice = f"\n... watchdog dropped {size - len(tail)} earlier bytes ...\n".encode()
    with target.open("wb") as handle:
        handle.write(notice + tail)
    return size - len(tail)


def resolve_backend(
    backend: str, args: argparse.Namespace
) -> tuple[Callable[[str], str], Callable[[str], bool]]:
    """The (probe, restart) pair for one backend, wired from the parsed arguments."""
    if backend == "launchd":
        command = shlex.split(args.health_command) or list(DEFAULT_HEALTH_COMMAND.split())
        app_dir = Path(args.app_dir).expanduser() if args.app_dir else PROJECT_ROOT
        probe = lambda target: launchd_health(target, command=command, app_dir=app_dir)  # noqa: E731
        return probe, launchd_restart
    return container_health, restart_container


BACKENDS: tuple[str, ...] = ("docker", "launchd")


def tick(
    targets: Sequence[str],
    state: WatchState,
    policy: Policy,
    moment: float,
    *,
    probe: Callable[[str], str] = container_health,
    restart: Callable[[str], bool] = restart_container,
    note: Callable[..., None] = emit,
) -> list[str]:
    """One observation pass; returns the targets that were restarted."""
    restarted: list[str] = []
    for name in targets:
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
            note("watchdog_restart", target=name, ok=ok, restarts=len(state.restarts.get(name, [])))
            continue

        note(
            "watchdog_observe",
            target=name,
            health=health,
            failures=state.failures.get(name, 0),
            action=decision.action,
            reason=decision.reason,
        )
        if decision.action == HALT:
            log.error(
                "watchdog_halted: %s has used %s restarts in %ss; leaving it alone",
                name,
                policy.max_restarts,
                int(policy.window_seconds),
            )

    return restarted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchdog",
        description="Restart a monitored process that stays up but stops working.",
    )
    parser.add_argument(
        "--backend",
        choices=BACKENDS,
        default="docker",
        help="how to probe and restart: docker (container healthcheck) or "
        "launchd (the app's own --health, then kickstart)",
    )
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        metavar="NAME",
        help="container name, or launchd label with --backend launchd (repeatable)",
    )
    parser.add_argument(
        "--container",
        action="append",
        dest="containers",
        metavar="NAME",
        help="deprecated alias for --target",
    )
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between passes (default 60)")
    parser.add_argument("--threshold", type=int, default=3, help="consecutive unhealthy reads before acting")
    parser.add_argument("--max-restarts", type=int, default=3, help="restart budget inside --window")
    parser.add_argument("--window", type=float, default=3600.0, help="budget window in seconds (default 3600)")
    parser.add_argument(
        "--health-command",
        default=DEFAULT_HEALTH_COMMAND,
        help="command for --backend launchd to ask the app if its loop is turning "
        "(default: this interpreter, -m bot.main --health)",
    )
    parser.add_argument(
        "--app-dir",
        default=None,
        help="directory the health command runs in (default: this repository)",
    )
    parser.add_argument(
        "--log-file",
        action="append",
        dest="log_files",
        metavar="PATH",
        help="bound this launchd-written log to --max-log-mb, keeping its tail (repeatable)",
    )
    parser.add_argument(
        "--max-log-mb",
        type=float,
        default=20.0,
        help="size cap per --log-file; 0 disables bounding (default 20)",
    )
    parser.add_argument("--once", action="store_true", help="run one pass and exit (cron/CI)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def resolve_targets(args: argparse.Namespace) -> list[str]:
    """The watch list, merging the deprecated alias.

    An empty list is refused rather than accepted: a supervisor that watches
    nothing starts, stays up, logs nothing, and reports success - which is worse
    than a crash, because a crash would be noticed.
    """
    targets = list(args.targets or []) + list(args.containers or [])
    if not targets:
        raise ValueError("no --target given")
    return targets


def log_caps(args: argparse.Namespace) -> list[tuple[str, int]]:
    """`(path, byte ceiling)` for each ``--log-file``; a zero cap means leave it."""
    ceiling = int(max(args.max_log_mb, 0.0) * 1024 * 1024)
    return [(path, ceiling) for path in (args.log_files or [])]


def _install_stop_handler(on_stop: Callable[[], None]) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: on_stop())
        except (ValueError, OSError):  # pragma: no cover - not the main thread
            log.warning("watchdog_signal_handler_unavailable: %s", sig)


def _wait(interval: float, stopping: Callable[[], bool]) -> None:
    """Sleep in slices so a stop signal is honoured promptly, not after a whole
    interval - systemd and launchd both send one on every stop and reload."""
    deadline = time.monotonic() + max(interval, 1.0)
    while not stopping() and time.monotonic() < deadline:
        time.sleep(min(1.0, max(deadline - time.monotonic(), 0.0)))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        targets = resolve_targets(args)
    except ValueError as exc:
        print(f"watchdog: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        stream=sys.stderr,
    )
    probe, restart = resolve_backend(args.backend, args)
    bounded_logs = log_caps(args)
    policy = Policy(
        threshold=args.threshold,
        max_restarts=args.max_restarts,
        window_seconds=args.window,
    )
    state = WatchState()
    flags = {"stopping": False}
    _install_stop_handler(lambda: flags.__setitem__("stopping", True))

    emit("watchdog_started", backend=args.backend, targets=targets, policy=asdict(policy))
    while True:
        try:
            tick(targets, state, policy, time.monotonic(), probe=probe, restart=restart)
            for path, cap in bounded_logs:
                dropped = bound_log(path, cap)
                if dropped:
                    emit("watchdog_log_truncated", path=str(path), bytes_dropped=dropped, cap_bytes=cap)
        except Exception as exc:  # noqa: BLE001 - the watchdog must outlive a bad pass
            log.warning("watchdog_pass_failed: %s: %s", type(exc).__name__, str(exc)[:300])
        if args.once or flags["stopping"]:
            break
        _wait(args.interval, lambda: flags["stopping"])
    emit("watchdog_stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())


__all__ = [
    "BACKENDS",
    "HALT",
    "Policy",
    "RESTART",
    "WAIT",
    "WatchState",
    "bound_log",
    "container_health",
    "emit",
    "launchd_health",
    "launchd_restart",
    "log_caps",
    "next_decision",
    "resolve_backend",
    "resolve_targets",
    "restart_container",
    "tick",
]
