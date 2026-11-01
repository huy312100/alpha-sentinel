"""The watchdog's decision rules, which are the part that can be wrong.

The Docker calls are two thin subprocess wrappers; everything worth testing is
the arithmetic between an observation and an action. The cases that matter are
the ones that would do damage: restarting on the first blip, restarting during
``start_period``, and bouncing a container forever because a restart cannot fix
what is broken.
"""

from __future__ import annotations

import ast
import shlex
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from ops.watchdog import (
    DEFAULT_HEALTH_COMMAND,
    HALT,
    RESTART,
    WAIT,
    Policy,
    WatchState,
    bound_log,
    container_health,
    default_health_command,
    health_argv,
    launchd_health,
    launchd_restart,
    log_caps,
    next_decision,
    resolve_backend,
    resolve_targets,
    restart_container,
    tick,
)

POLICY = Policy(threshold=3, max_restarts=2, window_seconds=3600.0)


def decide(health: str, failures: int, restarts: int = 0, policy: Policy = POLICY):
    return next_decision(
        health=health,
        consecutive_failures=failures,
        restarts_within_window=restarts,
        policy=policy,
    )


def quiet(*_: Any, **__: Any) -> None:
    """Swallow the JSON log line; a passing test should not print."""


class Completed:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


# -- the decision table ------------------------------------------------------


def test_a_healthy_container_is_left_alone():
    assert decide("healthy", 0).action == WAIT


def test_starting_is_not_a_failure():
    """Restarting during start_period would kill every fresh boot."""
    assert decide("starting", 0).action == WAIT
    assert decide("starting", 9).action == WAIT


def test_a_container_without_a_healthcheck_is_not_unhealthy():
    assert decide("none", 99).action == WAIT


def test_a_missing_container_is_reported_and_not_restarted():
    decision = decide("missing", 99)
    assert decision.action == WAIT
    assert decision.reason == "container_not_found"


def test_one_unhealthy_read_only_confirms():
    """A single failed probe is usually a provider blip, not a dead loop."""
    assert decide("unhealthy", 1).action == WAIT
    assert decide("unhealthy", 2).action == WAIT
    assert decide("unhealthy", 2).reason == "confirming"


def test_the_threshold_is_what_triggers_a_restart():
    assert decide("unhealthy", 3).action == RESTART


def test_an_exhausted_budget_halts_instead_of_restarting():
    decision = decide("unhealthy", 3, restarts=2)
    assert decision.action == HALT
    assert decision.reason == "restart_budget_exhausted"


def test_a_zero_threshold_still_needs_one_observation():
    """``--threshold 0`` must mean "act on the first bad read", not "act blindly"."""
    policy = Policy(threshold=0, max_restarts=2, window_seconds=3600.0)

    _, restarted = run_tick(["unhealthy"], policy=policy)

    assert restarted == ["bot"]


# -- state -------------------------------------------------------------------


def test_restart_budget_forgets_attempts_outside_the_window():
    state = WatchState()
    state.record_restart("bot", moment=0.0, window=100.0)

    assert state.restarts_within("bot", 50.0, 100.0) == 1
    assert state.restarts_within("bot", 150.0, 100.0) == 0


def test_recording_forgets_stale_attempts_instead_of_growing_forever():
    state = WatchState()
    for moment in (0.0, 10.0, 20.0):
        state.record_restart("bot", moment=moment, window=15.0)

    assert state.restarts_within("bot", 20.0, 15.0) == 2


# -- one pass ----------------------------------------------------------------


def run_tick(sequence, state=None, policy=POLICY, restarted=None):
    """Drive ``tick`` over a fixed sequence of health readings."""
    readings = iter(sequence)
    restarted = restarted if restarted is not None else []
    state = state if state is not None else WatchState()
    for moment, _ in enumerate(sequence):
        tick(
            ["bot"],
            state,
            policy,
            float(moment),
            probe=lambda _name: next(readings),
            restart=lambda name: restarted.append(name) or True,
            note=quiet,
        )
    return state, restarted


def test_two_unhealthy_passes_do_not_restart():
    _, restarted = run_tick(["unhealthy", "unhealthy"])

    assert restarted == []


def test_three_consecutive_unhealthy_passes_restart_exactly_once():
    _, restarted = run_tick(["unhealthy", "unhealthy", "unhealthy", "unhealthy"])

    assert restarted == ["bot"]


def test_a_recovery_clears_the_evidence():
    """Two failures either side of a healthy read are not three in a row."""
    _, restarted = run_tick(["unhealthy", "unhealthy", "healthy", "unhealthy", "unhealthy"])

    assert restarted == []


def test_the_budget_stops_the_bouncing():
    policy = Policy(threshold=1, max_restarts=2, window_seconds=3600.0)
    _, restarted = run_tick(["unhealthy"] * 5, policy=policy)

    assert restarted == ["bot", "bot"]


def test_a_restart_that_fails_still_consumes_the_budget():
    """Otherwise a container that cannot be restarted is retried forever."""
    policy = Policy(threshold=1, max_restarts=2, window_seconds=3600.0)
    state = WatchState()
    attempts = []

    for moment in range(5):
        tick(
            ["bot"],
            state,
            policy,
            float(moment),
            probe=lambda _name: "unhealthy",
            restart=lambda name: attempts.append(name) and False,
            note=quiet,
        )

    assert attempts == ["bot", "bot"]


def test_each_container_is_judged_on_its_own_evidence():
    state = WatchState()
    readings = {"bot": "unhealthy", "dashboard": "healthy"}
    restarted = []

    for moment in range(3):
        tick(
            ["bot", "dashboard"],
            state,
            POLICY,
            float(moment),
            probe=lambda name: readings[name],
            restart=lambda name: restarted.append(name) or True,
            note=quiet,
        )

    assert restarted == ["bot"]


# -- the docker wrappers -----------------------------------------------------


def test_health_is_read_through_the_conditional_template():
    assert container_health("bot", runner=lambda *a, **k: Completed(0, "unhealthy\n")) == "unhealthy"
    assert container_health("bot", runner=lambda *a, **k: Completed(0, "none\n")) == "none"


def test_a_failed_inspect_reads_as_missing_not_unhealthy():
    """A transient docker error must not be mistaken for a sick container."""
    assert container_health("bot", runner=lambda *a, **k: Completed(1, "")) == "missing"


def test_no_docker_binary_reads_as_missing():
    def boom(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("docker")

    assert container_health("bot", runner=boom) == "missing"


def test_restart_reports_the_docker_exit_status():
    assert restart_container("bot", runner=lambda *a, **k: Completed(0)) is True
    assert restart_container("bot", runner=lambda *a, **k: Completed(1)) is False


def test_a_restart_that_cannot_be_invoked_is_a_failure_not_a_crash():
    def boom(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("docker")

    assert restart_container("bot", runner=boom) is False


# -- the launchd backend -----------------------------------------------------


def test_the_apps_own_health_exit_code_becomes_the_status_word():
    """``--health`` already answers the question; the watchdog adds no judgement."""
    for code, expected in ((0, "healthy"), (1, "unhealthy")):
        assert launchd_health("bot", command=["x"], runner=lambda *a, **k: Completed(code, "")) == expected


def test_a_refused_boot_is_missing_and_never_unhealthy():
    """Exit 2 means the environment is wrong, which a kickstart cannot repair."""
    assert launchd_health("bot", command=["x"], runner=lambda *a, **k: Completed(2, "")) == "missing"


def test_a_health_probe_that_cannot_run_is_not_evidence_of_a_dead_loop():
    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("no python")

    assert launchd_health("bot", command=["x"], runner=boom) == "missing"


def test_the_health_probe_runs_inside_the_app_directory():
    """``bot.main`` is importable from the repo, not from the watchdog's cwd."""
    seen: dict[str, Any] = {}

    def spy(command, **kwargs):
        seen["command"] = command
        seen["cwd"] = kwargs.get("cwd")
        return Completed(0, "")

    launchd_health("bot", command=["py", "-m", "bot.main", "--health"], app_dir=Path("/srv/app"), runner=spy)

    assert seen["command"] == ["py", "-m", "bot.main", "--health"]
    assert seen["cwd"] == "/srv/app"


def test_restart_kickstarts_the_label_in_the_user_domain():
    """``-k`` is what kills a wedged process; without it launchd starts a second."""
    seen: list[list[str]] = []

    def spy(command, **kwargs):
        seen.append(list(command))
        return Completed(0, "")

    assert launchd_restart("com.alphasentinel.bot", uid=501, runner=spy) is True
    assert seen == [["launchctl", "kickstart", "-k", "gui/501/com.alphasentinel.bot"]]


def test_a_missing_launchctl_is_a_failed_restart_not_an_exception():
    def boom(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("launchctl")

    assert launchd_restart("com.alphasentinel.bot", uid=501, runner=boom) is False


def test_the_backend_pair_is_chosen_by_name():
    class Args:
        backend = "launchd"
        health_command = "python -m bot.main --health"
        app_dir = None

    probe, restart = resolve_backend("launchd", Args())
    assert probe is not container_health
    assert restart is launchd_restart

    docker_probe, docker_restart = resolve_backend("docker", Args())
    assert docker_probe is container_health and docker_restart is restart_container


def test_the_watch_list_merges_the_alias_and_refuses_to_be_empty():
    """A supervisor with nothing to watch starts, stays up and does nothing.

    That is worse than crashing, because a crash gets noticed: the units install
    with a typo in the target and then appear healthy for a week.
    """
    assert resolve_targets(Namespace(targets=["a"], containers=["b"])) == ["a", "b"]
    assert resolve_targets(Namespace(targets=None, containers=["legacy"])) == ["legacy"]

    with pytest.raises(ValueError, match="no --target given"):
        resolve_targets(Namespace(targets=None, containers=None))


def test_an_interpreter_path_containing_a_space_stays_one_argument():
    """A checkout under ``/Users/John Smith/`` is ordinary on macOS.

    Unquoted, the split hands back ``/Users/John`` as the program. That raises
    FileNotFoundError, which maps to ``missing`` rather than ``unhealthy`` - so the
    supervisor starts, loops, reports success, and never once restarts the bot it was
    installed to restart. Nothing in the log points at the cause.
    """
    spaced = "/Users/John Smith/repo/.venv/bin/python"

    argv = health_argv(default_health_command(spaced))

    assert argv[0] == spaced
    assert argv[1:] == ["-m", "bot.main", "--health"]


def test_the_fallback_is_split_the_same_way_as_the_configured_command():
    """An empty flag falls back; a ``str.split`` fallback would undo the quoting."""
    assert health_argv("") == shlex.split(DEFAULT_HEALTH_COMMAND)
    assert " " not in health_argv("")[0] or health_argv("")[0].startswith("/")


def test_the_log_ceiling_is_declared_in_megabytes():
    caps = log_caps(Namespace(log_files=["a.log", "b.log"], max_log_mb=20.0))

    assert caps == [("a.log", 20 * 1024 * 1024), ("b.log", 20 * 1024 * 1024)]
    assert log_caps(Namespace(log_files=["a.log"], max_log_mb=0.0)) == [("a.log", 0)]
    assert log_caps(Namespace(log_files=None, max_log_mb=5.0)) == []


# -- log bounding ------------------------------------------------------------


def test_a_small_log_is_left_alone(tmp_path):
    path = tmp_path / "bot.log"
    path.write_text("line\n", encoding="utf-8")

    assert bound_log(path, 20 * 1024 * 1024) == 0
    assert path.read_text(encoding="utf-8") == "line\n"


def test_bounding_keeps_the_tail_and_reports_what_it_dropped(tmp_path):
    path = tmp_path / "bot.log"
    path.write_bytes(b"OLD\n" + b"keep me\n" * 200)

    dropped = bound_log(path, 256)

    assert dropped > 0
    text = path.read_bytes()
    assert len(text) <= 256
    assert text.endswith(b"keep me\n"), "the newest lines are the ones worth keeping"
    assert b"OLD" not in text
    assert b"dropped" in text


def test_bounding_keeps_a_first_line_that_is_not_actually_partial(tmp_path):
    """Uniform log lines land the read exactly on a line boundary.

    Dropping the first line unconditionally - which the first draft did - throws
    away a whole, intact record every single time, which is the newest-but-one
    line an operator reaches for when asking what happened just before a restart.
    """
    path = tmp_path / "bot.log"
    path.write_bytes(b"".join(f"line {i:03d}\n".encode() for i in range(100)))  # 1000 bytes

    bound_log(path, 200)

    assert b"line 090" in path.read_bytes()


def test_bounding_drops_a_partial_first_line_rather_than_keeping_it_mangled(tmp_path):
    """The other half: a boundary inside a line must not leave half a record.

    The retained window starts 40 bytes into a 120-byte line, so that line is
    truncated and goes; the complete line after it survives untouched.
    """
    path = tmp_path / "bot.log"
    path.write_bytes(b"m" * 120 + b"\n" + b"GOOD LINE\n")  # 130 bytes

    bound_log(path, 100)  # keeps the last 50 bytes, which start mid-line

    kept = path.read_bytes()

    assert b"GOOD LINE" in kept.split(b"\n"), kept
    assert b"mmmm" not in kept, "a truncated line must not masquerade as a record"


def test_bounding_rewrites_in_place_because_launchd_holds_the_inode(tmp_path):
    """Replacing the file would leave launchd appending to a deleted inode."""
    path = tmp_path / "bot.log"
    path.write_bytes(b"x" * 4000)
    before = path.stat().st_ino

    bound_log(path, 512)

    assert path.stat().st_ino == before


def test_bounding_an_absent_log_is_not_an_error(tmp_path):
    assert bound_log(tmp_path / "never-written.log", 100) == 0


def test_a_zero_cap_disables_bounding(tmp_path):
    """``--max-log-mb 0`` means the operator asked for no ceiling, not for deletion."""
    path = tmp_path / "bot.log"
    path.write_bytes(b"y" * 5000)

    assert bound_log(path, 0) == 0
    assert path.stat().st_size == 5000


def test_the_watchdog_stays_loadable_by_the_host_interpreter():
    """The units invoke ``/usr/bin/python3``, which may be 3.8 or 3.9.

    Two halves, because the two failure modes are different: the file must parse as
    3.8 syntax, and it must not reach for dataclass ``slots``, which is 3.10 at
    *runtime* and so invisible to a parser check - it is exactly what broke the
    first draft of this supervisor.
    """
    source = (Path(__file__).resolve().parents[1] / "ops" / "watchdog.py").read_text(encoding="utf-8")

    tree = ast.parse(source, filename="ops/watchdog.py", feature_version=(3, 8))

    # Read the decorators, not the text: this file's own docstring *mentions*
    # slots=True while explaining why it must not be used, and a substring search
    # cannot tell a warning from a violation.
    slotted = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "dataclass"
        and any(keyword.arg == "slots" for keyword in node.keywords)
    ]
    assert slotted == []
