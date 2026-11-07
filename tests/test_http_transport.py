"""Transport policy: what a retry costs, and what a 429 teaches.

These are the two behaviours the scanner layers inherit and never implement
themselves, so they are tested against the seam rather than against a vendor:

* ``Retry-After`` is read in both of its standard forms and clamped;
* a throttle response closes the *host* for every caller, while an ordinary 500 does
  not - which is the difference between "we are over our quota" and "the server hiccuped".

Nothing here touches the network, and no test sleeps for a real vendor window: the
recorded ``asyncio.sleep`` delays are the assertion.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from core.http import (
    DEFAULT_THROTTLE_WAIT_SECONDS,
    MAX_THROTTLE_WAIT_SECONDS,
    HttpError,
    RateLimiter,
    parse_retry_after,
    request_json,
    throttle_penalty,
    utcnow,
)

URL = "https://api.geckoterminal.com/api/v2/networks/eth/pools/0xdead/ohlcv/hour"


class FakeResponse:
    def __init__(self, status: int, *, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}

    async def text(self) -> str:
        return '{"errors":[{"title":"Rate limited"}]}'

    async def json(self, content_type: Any = None) -> dict[str, Any]:
        return {"data": {"attributes": {}}}

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeSession:
    """Serves a scripted sequence of statuses, one per request made."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.requests = 0

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.requests += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome[0], headers=outcome[1])


class RecordingLimiter:
    """A bucket that reports what was asked of it, for the pacing-contract tests."""

    def __init__(self) -> None:
        self.acquires = 0
        self.penalties: list[float] = []

    async def acquire(self) -> None:
        self.acquires += 1

    def penalize(self, seconds: float) -> None:
        self.penalties.append(seconds)


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Record every backoff the transport asks for, without waiting for it."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


class TestParseRetryAfter:
    def test_delta_seconds(self):
        assert parse_retry_after("30") == 30.0

    def test_zero_is_an_answer_not_a_missing_header(self):
        # The parser's job is to report what the header said, distinguishing "0" from
        # absent. What to *do* about a 0 is the retry policy's decision, and it is not
        # "go now" - see TestThrottleAwareRetry below.
        assert parse_retry_after("0") == 0.0
        assert parse_retry_after(None) is None
        assert parse_retry_after("") is None

    def test_http_date_deadline(self):
        future = (utcnow() + timedelta(seconds=45)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert 5.0 <= parse_retry_after(future) <= 46.0

    def test_garbage_is_ignored_rather_than_trusted(self):
        assert parse_retry_after("soon") is None
        assert parse_retry_after("-12") == 0.0

    def test_a_hostile_window_is_clamped(self):
        assert parse_retry_after("86400") == MAX_THROTTLE_WAIT_SECONDS


class TestRateLimiterPenalty:
    def test_penalty_is_the_longest_wait_asked_for(self):
        limiter = RateLimiter(1.0, burst=1)
        limiter.penalize(5.0)
        first = limiter.blocked_for
        limiter.penalize(2.0)
        assert limiter.blocked_for == pytest.approx(first, abs=0.5)
        limiter.penalize(30.0)
        assert limiter.blocked_for > first

    def test_penalty_is_clamped_to_the_same_ceiling_as_the_header(self):
        limiter = RateLimiter(1.0, burst=1)
        limiter.penalize(3600.0)
        assert limiter.blocked_for <= MAX_THROTTLE_WAIT_SECONDS

    async def test_acquire_waits_out_the_window_before_taking_a_token(self, sleeps):
        limiter = RateLimiter(1.0, burst=1)
        limiter.penalize(12.0)
        await limiter.acquire()
        assert any(wait >= 11.0 for wait in sleeps), sleeps


class TestThrottlePenaltyPolicy:
    """Which response closes the host, and for how long.

    The bug this pins is the one a status check invites: treating every retryable
    status as quota, so a single 500 from a busy vendor stalls every concurrent
    caller of that host for a window nobody declared.
    """

    def test_429_always_costs_at_least_one_estimated_window(self):
        assert throttle_penalty(429, None) == DEFAULT_THROTTLE_WAIT_SECONDS
        assert throttle_penalty(429, 0.0) == DEFAULT_THROTTLE_WAIT_SECONDS
        assert throttle_penalty(429, 45.0) == 45.0

    def test_a_named_window_is_honoured_on_any_status(self):
        assert throttle_penalty(503, 20.0) == 20.0
        assert throttle_penalty(404, 5.0) == 5.0

    def test_a_bare_server_fault_is_not_quota(self):
        assert throttle_penalty(500, None) is None
        assert throttle_penalty(503, None) is None
        assert throttle_penalty(502, None) is None
        assert throttle_penalty(None, None) is None

    def test_a_named_window_is_still_clamped(self):
        assert throttle_penalty(503, 3600.0) == MAX_THROTTLE_WAIT_SECONDS


class TestThrottleAwareRetry:
    async def test_retry_after_header_sizes_the_retry_and_closes_the_host(self, sleeps):
        session = FakeSession([(429, {"Retry-After": "7"}), (200, {})])
        limiter = RateLimiter(1.0, burst=1)

        payload = await request_json(session, URL, retries=2, limiter=limiter)

        assert payload == {"data": {"attributes": {}}}
        assert session.requests == 2
        # The header, not the exponential default (1.5**0 + jitter < 2), is what the
        # retry waited for.
        assert sleeps[0] >= 7.0
        assert limiter.blocked_for > 0

    async def test_a_429_without_a_header_uses_the_blind_window(self, sleeps):
        session = FakeSession([(429, {}), (200, {})])
        limiter = RateLimiter(1.0, burst=1)

        await request_json(session, URL, retries=2, limiter=limiter)

        assert sleeps[0] >= DEFAULT_THROTTLE_WAIT_SECONDS
        assert limiter.blocked_for > 0

    async def test_a_zero_retry_after_does_not_disable_the_throttle(self, sleeps):
        """Measured, not hypothetical.

        A burst of 40 requests at this host drew 19 rejections and *every one* of them
        carried ``Retry-After: 0``. That is self-consistent for a vendor metering a
        window: an individual request can be retriable immediately while the window is
        still shut. Honouring the 0 literally would restart the storm the instant the
        throttle is supposed to end it, so the header may only lengthen the wait.
        """
        session = FakeSession([(429, {"Retry-After": "0"}), (200, {})])
        limiter = RateLimiter(1.0, burst=1)

        await request_json(session, URL, retries=2, limiter=limiter)

        assert sleeps[0] >= DEFAULT_THROTTLE_WAIT_SECONDS
        assert limiter.blocked_for > 0

    async def test_a_plain_500_is_not_treated_as_quota(self, sleeps):
        """The distinction the whole design rests on.

        A hiccup must not close a host for everyone: if 500s were throttled like 429s,
        one transient server fault would serialize an entire cycle behind a penalty
        nobody asked for.
        """
        session = FakeSession([(500, {}), (200, {})])
        limiter = RateLimiter(1.0, burst=1)

        await request_json(session, URL, retries=2, limiter=limiter)

        assert sleeps[0] < 5.0
        assert limiter.blocked_for == 0.0

    async def test_exhausted_retries_surface_the_status_and_the_window(self, sleeps):
        """The header survives all the way out, because a layer that reports the
        failure should be able to say whether it was quota or a fault."""
        session = FakeSession([(429, {"Retry-After": "3"}), (429, {"Retry-After": "3"}), (429, {"Retry-After": "3"})])

        with pytest.raises(HttpError) as raised:
            await request_json(session, URL, retries=2)

        assert raised.value.status == 429
        assert raised.value.retry_after == 3.0
        assert len(sleeps) == 2, sleeps

    async def test_no_limiter_means_no_shared_state_to_penalize(self, sleeps):
        """Callers without a bucket still honour the header; they just cannot teach anyone."""
        session = FakeSession([(429, {"Retry-After": "9"}), (200, {})])

        await request_json(session, URL, retries=2, limiter=None)

        assert sleeps[0] >= 9.0

    async def test_the_attempt_that_gives_up_still_closes_the_host(self, sleeps):
        """Penalising inside the retry path only would be the interesting half-bug.

        The request that exhausts its retries is the one holding the proof the window
        is shut. If it teaches nothing, every later caller rediscovers it with its own
        budget - which is the storm this change exists to stop, in a new costume.
        """
        session = FakeSession([(429, {"Retry-After": "20"}), (429, {"Retry-After": "20"}), (429, {"Retry-After": "20"})])
        limiter = RateLimiter(1.0, burst=1)

        with pytest.raises(HttpError):
            await request_json(session, URL, retries=2, limiter=limiter)

        assert limiter.blocked_for >= 19.0

    async def test_a_retries_zero_throttle_is_recorded_too(self, sleeps):
        session = FakeSession([(429, {})])
        limiter = RateLimiter(1.0, burst=1)

        with pytest.raises(HttpError):
            await request_json(session, URL, retries=0, limiter=limiter)

        assert limiter.blocked_for > 0
        assert sleeps == [], sleeps

    async def test_a_bare_503_does_not_close_the_host_for_everyone(self, sleeps):
        """The distinction a status set alone gets wrong.

        A 503 is "server hiccuped" unless it names a window. Letting it penalise the
        shared bucket would let one transient fault serialize a whole cycle.
        """
        session = FakeSession([(503, {}), (200, {})])
        limiter = RateLimiter(1.0, burst=1)

        await request_json(session, URL, retries=2, limiter=limiter)

        assert sleeps[0] < 5.0
        assert limiter.blocked_for == 0.0

    async def test_a_503_that_names_a_window_is_honoured(self, sleeps):
        session = FakeSession([(503, {"Retry-After": "25"}), (200, {})])
        limiter = RateLimiter(1.0, burst=1)

        await request_json(session, URL, retries=2, limiter=limiter)

        assert sleeps[0] >= 25.0
        assert limiter.blocked_for >= 24.0

    async def test_a_retry_is_another_request_and_paces_itself(self, sleeps):
        """The half of a rate limit that spacing only the first attempt is not.

        Measured on this head before the fix: a bucket set to 9 requests/minute put
        14/minute on the vendor at 2.00 HTTP attempts per logical request, because
        retries bypassed the bucket. ``penalize`` was still recorded, so nothing in the
        logs looked wrong - the pacing simply was not applied to the traffic.
        """
        session = FakeSession([(429, {"Retry-After": "2"}), (429, {"Retry-After": "2"}), (200, {})])
        limiter = RecordingLimiter()

        await request_json(session, URL, retries=2, limiter=limiter)

        # The caller acquires for attempt 1 outside this function; attempts 2 and 3
        # must acquire here, so 2 is the whole of the transport's responsibility.
        assert limiter.acquires == 2, limiter.acquires
        assert all(penalty >= DEFAULT_THROTTLE_WAIT_SECONDS for penalty in limiter.penalties), limiter.penalties

    async def test_a_clean_first_attempt_acquires_nothing_extra(self, sleeps):
        """The retry pacing must not become a second tax on requests that worked."""
        session = FakeSession([(200, {})])
        limiter = RecordingLimiter()

        await request_json(session, URL, retries=2, limiter=limiter)

        assert limiter.acquires == 0
        assert limiter.penalties == []
