"""The watchdog's decision rules, which are the part that can be wrong.

The Docker calls are two thin subprocess wrappers; everything worth testing is
the arithmetic between an observation and an action. The cases that matter are
the ones that would do damage: restarting on the first blip, restarting during
``start_period``, and bouncing a container forever because a restart cannot fix
what is broken.
"""

from __future__ import annotations

from typing import Any

from ops.watchdog import (
    HALT,
    RESTART,
    WAIT,
    Policy,
    WatchState,
    container_health,
    next_decision,
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
