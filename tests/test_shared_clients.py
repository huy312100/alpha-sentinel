"""Shared clients: one HTTP policy and one indicator library for every market.

The point of ``scanners/shared`` is that these behaviours are implemented once.
The tests therefore cover the *contract* - containment, rate limiting, maths
edge cases - rather than any single vendor payload.
"""

from __future__ import annotations

import pytest

from core.logging import REDACTED, clear_secrets, register_secret, scrub_url_params
from scanners.shared import http_client
from scanners.shared.http_client import (
    ProviderResult,
    fetch_provider,
    first_float,
    first_int,
    first_row,
    limiter_for,
)
from scanners.shared.listed import technicals as technicals_layer
from scanners.shared.technicals import (
    atr,
    bollinger,
    compute_indicators,
    ema,
    macd,
    obv,
    order_flow_imbalance,
    percent_change,
    rsi,
    sma,
    zscore,
)


class TestProviderContainment:
    async def test_success_keeps_the_metrics(self):
        async def answered():
            return {"holders": 1200}

        result = await fetch_provider("vendor", answered())
        assert isinstance(result, ProviderResult)
        assert result.ok
        assert result.metrics == {"holders": 1200}

    async def test_failure_becomes_an_error_instead_of_an_exception(self):
        async def denied():
            raise RuntimeError("429 too many requests")

        result = await fetch_provider("vendor", denied())
        assert not result.ok
        assert result.error is not None and "429" in result.error

    async def test_an_empty_payload_is_not_a_success(self):
        async def empty():
            return {}

        result = await fetch_provider("vendor", empty())
        assert not result.ok
        assert result.error == "no usable data"

    async def test_a_query_string_credential_never_reaches_the_signal(self):
        """The error is stored on the layer, not only logged, so scrub it here."""
        clear_secrets()
        register_secret("news-token-123")

        async def denied():
            raise RuntimeError(
                "404 from https://cryptopanic.com/api/v1/posts/?auth_token=news-token-123&public=true"
            )

        result = await fetch_provider("cryptopanic", denied())
        clear_secrets()

        assert not result.ok
        assert "news-token-123" not in (result.error or "")
        assert "https://cryptopanic.com/api/v1/posts/" in (result.error or "")


class TestPayloadPolling:
    def test_first_float_skips_nulls_and_booleans(self):
        payload = {"a": None, "b": True, "c": "12.5"}
        assert first_float(payload, "a", "b", "c") == 12.5
        assert first_float(payload, "a", "b") is None
        assert first_float(None, "a") is None

    def test_first_int_truncates(self):
        assert first_int({"count": "42.9"}, "count") == 42

    def test_first_row_walks_the_common_envelopes(self):
        assert first_row([{"x": 1}]) == [{"x": 1}]
        assert first_row({"data": [{"x": 1}]}) == [{"x": 1}]
        assert first_row({"result": {"rows": [{"x": 1}]}}) == [{"x": 1}]
        assert first_row({"unexpected": 1}) == []


class TestRateLimiting:
    def test_one_bucket_per_host(self):
        first = limiter_for("https://alpha.example.com/one")
        second = limiter_for("https://alpha.example.com/two")
        other = limiter_for("https://beta.example.com/one")
        assert first is second
        assert other is not first

    def test_a_later_stricter_rate_replaces_the_cached_bucket(self):
        url = "https://strict.example.com/v1/thing"
        fast = limiter_for(url, rate_per_second=20.0)
        assert limiter_for(url, rate_per_second=20.0) is fast

        slow = limiter_for(url, rate_per_second=1.0)
        assert slow is not fast
        assert slow._interval == pytest.approx(1.0)
        # A later, faster request must not loosen the strictest caller's rate.
        assert limiter_for(url, rate_per_second=20.0) is slow

    async def test_request_json_goes_through_the_shared_transport(self, monkeypatch):
        calls: list[tuple[str, dict]] = []

        async def fake_transport(session, url, **kwargs):
            calls.append((url, kwargs))
            return {"ok": True}

        monkeypatch.setattr(http_client, "_core_request_json", fake_transport)
        payload = await http_client.request_json(
            object(),
            "https://api.example.com/v1/thing",
            params={"q": "1"},
            rate_per_second=1000.0,
        )
        assert payload == {"ok": True}
        assert len(calls) == 1
        url, kwargs = calls[0]
        assert url == "https://api.example.com/v1/thing"
        assert kwargs["params"] == {"q": "1"}
        assert kwargs["method"] == "GET"


class TestIndicatorMaths:
    def test_sma_and_ema_need_a_full_window(self):
        values = [float(value) for value in range(1, 31)]
        assert sma(values, 5) == pytest.approx(28.0)
        assert sma(values[:3], 5) is None
        assert ema(values[:3], 5) is None
        # A monotonically rising series keeps the EMA below the spot price.
        assert 0.0 < ema(values, 10) < values[-1]

    def test_rsi_extremes_and_insufficient_history(self):
        rising = [float(value) for value in range(1, 40)]
        falling = [float(value) for value in range(40, 1, -1)]
        assert rsi(rising, 14) == 100.0
        assert rsi(falling, 14) == 0.0
        assert rsi(rising[:10], 14) is None

    def test_macd_reports_histogram_acceleration(self):
        values = [100.0 + value for value in range(60)]
        stats = macd(values)
        assert stats is not None
        assert stats["histogram"] == pytest.approx(stats["macd"] - stats["signal"])
        assert macd(values[:20]) is None

    def test_atr_and_bollinger(self):
        highs = [10.0 + value for value in range(30)]
        lows = [9.0 + value for value in range(30)]
        closes = [9.5 + value for value in range(30)]
        assert atr(highs, lows, closes, 14) is not None
        assert atr(highs[:10], lows[:10], closes[:10], 14) is None

        bands = bollinger(closes, 20)
        assert bands is not None
        assert bands["lower"] < bands["middle"] < bands["upper"]

    def test_zscore_flags_displacement(self):
        flat = [100.0] * 20
        assert zscore(flat) == 0.0
        assert zscore([1.0, 2.0]) is None
        spiked = [100.0] * 19 + [140.0]
        assert zscore(spiked) is not None and zscore(spiked) > 2.0

    def test_obv_and_percent_change(self):
        closes = [1.0, 2.0, 1.5, 1.5]
        volumes = [10.0, 20.0, 30.0, 40.0]
        assert obv(closes, volumes) == [0.0, 20.0, -10.0, -10.0]
        assert percent_change(120.0, 100.0) == pytest.approx(20.0)
        assert percent_change(120.0, 0.0) is None

    def test_percent_change_reads_a_signed_base_as_growth(self):
        """OBV goes negative in a downtrend; improving flow must not look like a fall."""
        assert percent_change(-50.0, -100.0) == pytest.approx(50.0)
        assert percent_change(-150.0, -100.0) == pytest.approx(-50.0)


class TestUrlScrubbing:
    def test_the_query_is_dropped_and_the_path_kept(self):
        text = "GET https://api.vendor.com/v1/thing?auth_token=abc123&x=1 failed"
        assert scrub_url_params(text) == f"GET https://api.vendor.com/v1/thing?{REDACTED} failed"

    def test_a_wrapped_url_keeps_its_punctuation(self):
        text = "(see https://api.vendor.com/v1/thing?token=abc)"
        assert scrub_url_params(text) == f"(see https://api.vendor.com/v1/thing?{REDACTED})"

    def test_text_without_a_url_is_untouched(self):
        assert scrub_url_params("connection reset by peer") == "connection reset by peer"
        assert scrub_url_params("https://api.vendor.com/v1/thing") == "https://api.vendor.com/v1/thing"

    def test_order_flow_imbalance_is_bounded(self):
        assert order_flow_imbalance(100.0, 0.0) == 1.0
        assert order_flow_imbalance(0.0, 100.0) == -1.0
        assert order_flow_imbalance(30.0, 10.0) == pytest.approx(0.5)
        assert order_flow_imbalance(0.0, 0.0) is None
        assert order_flow_imbalance(None, 10.0) is None


class TestSingleImplementation:
    def test_the_technicals_layer_does_not_re_export_the_maths(self):
        """The layer wires in the shared package instead of duplicating it.

        Re-exporting ``rsi``/``macd``/``atr`` from a layer module made the layer
        look like a second technicals implementation; the maths now has exactly one
        home (``scanners.shared.technicals``) and the layer's public surface is its
        scanner.
        """
        assert technicals_layer.compute_indicators is compute_indicators
        for duplicated in ("rsi", "macd", "atr", "bollinger", "obv", "sma", "ema"):
            assert not hasattr(technicals_layer, duplicated)
