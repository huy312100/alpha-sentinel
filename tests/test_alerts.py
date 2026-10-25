"""Alert policy, message rendering and duplicate suppression."""

from __future__ import annotations

import pytest

from bot.alerts import (
    ALERT_KIND,
    TELEGRAM_LIMIT,
    AlertResult,
    TelegramAlerter,
    chart_url,
    format_verdict,
    format_verdict_text,
    should_alert,
)
from config.settings import SecretStr, TelegramSettings
from core.http import HttpError
from core.models import VerdictStatus
from tests.conftest import TOKEN

CONFIGURED = TelegramSettings(bot_token=SecretStr("123:abc"), chat_id="-100123", alert_min_score=70.0)


class TestPolicy:
    @pytest.mark.parametrize("score,expected", [(70.0, True), (69.9, False), (95.0, True)])
    def test_approved_threshold(self, make_verdict, score, expected):
        verdict = make_verdict(final_score=score)
        assert should_alert(verdict, CONFIGURED) is expected

    @pytest.mark.parametrize("status", [VerdictStatus.WATCH, VerdictStatus.REJECTED])
    def test_non_actionable_never_alerts(self, make_verdict, status):
        assert should_alert(make_verdict(status=status, final_score=99.0), CONFIGURED) is False

    def test_veto_is_configurable(self, make_verdict):
        verdict = make_verdict(status=VerdictStatus.VETOED)
        assert should_alert(verdict, CONFIGURED) is True
        assert should_alert(verdict, TelegramSettings(alert_on_veto=False)) is False

    def test_custom_threshold(self, make_verdict):
        quiet = TelegramSettings(bot_token=SecretStr("1:x"), chat_id="1", alert_min_score=90.0)
        assert should_alert(make_verdict(final_score=85.0), quiet) is False


class TestRendering:
    def test_chart_url_targets_dexscreener(self, make_verdict):
        assert chart_url(make_verdict()) == f"https://dexscreener.com/ethereum/{TOKEN}"
        assert chart_url(make_verdict(chain="solana")) == f"https://dexscreener.com/solana/{TOKEN}"

    def test_html_escapes_model_output(self, make_verdict):
        verdict = make_verdict()
        text = format_verdict(verdict)
        assert "<b>APPROVED \u00b7 TEST</b>" in text
        assert f"<code>{TOKEN}</code>" in text
        assert "Momentum building &lt;b&gt;fast&lt;/b&gt; &amp; float is thin" in text
        assert "\u2192" in text  # prior -> alpha
        assert "dexscreener.com/ethereum/" + TOKEN in text

    def test_markup_in_symbol_is_escaped(self, make_verdict):
        verdict = make_verdict(symbol="<script>")
        assert "<script>" not in format_verdict(verdict)
        assert "&lt;script&gt;" in format_verdict(verdict)

    def test_veto_reasons_are_rendered(self, make_verdict):
        verdict = make_verdict(status=VerdictStatus.VETOED, final_score=12.0)
        text = format_verdict(verdict, reasons=["hard security flags: honeypot", "risk auditor veto"])
        assert "<b>Veto</b>: hard security flags: honeypot; risk auditor veto" in text

    def test_empty_optional_sections_are_skipped(self, make_verdict):
        verdict = make_verdict()
        verdict.alpha.thesis = ""
        verdict.alpha.catalysts = []
        verdict.risk.reasoning_summary = ""
        text = format_verdict(verdict)
        assert "<b>Thesis</b>" not in text
        assert "<b>Catalysts</b>" not in text
        assert "<b>Risk</b>" not in text

    def test_message_is_clipped_to_telegram_limit(self, make_verdict):
        verdict = make_verdict()
        verdict.alpha.catalysts = ["x" * 500 for _ in range(40)]
        verdict.alpha.thesis = "long " * 5000
        text = format_verdict(verdict)
        assert len(text) <= TELEGRAM_LIMIT

    def test_plain_text_twin(self, make_verdict):
        text = format_verdict_text(make_verdict(), reasons=["owner can mint"])
        assert text.startswith("[APPROVED] TEST on ethereum \u00b7 crypto - score 81.5")
        assert "size 0.75x" in text
        assert text.endswith("| veto: owner can mint")

    def test_plain_text_market_line_carries_asset_class(self, make_verdict):
        verdict = make_verdict(asset_class="equity", chain="us_equity", token_address="AAPL", symbol="AAPL")
        text = format_verdict_text(verdict)
        assert "on us_equity \u00b7 equity" in text


class TestDelivery:
    def alerter(self, telegram: TelegramSettings, store=None) -> TelegramAlerter:
        return TelegramAlerter(telegram, store=store, dedupe_hours=12.0)

    async def test_unconfigured_alerter_skips(self, make_verdict, store):
        alerter = self.alerter(TelegramSettings(), store=store)
        result = await alerter.send_verdict(make_verdict())
        assert result.sent is False
        assert result.skipped is True
        assert result.reason == "telegram_not_configured"
        assert store.recent_alerts() == []

    async def test_below_threshold_never_renders(self, make_verdict, store, monkeypatch):
        alerter = self.alerter(CONFIGURED, store=store)
        called = False

        async def fake_send(text, **kwargs):
            nonlocal called
            called = True
            return AlertResult(sent=True)

        monkeypatch.setattr(alerter, "send_text", fake_send)
        result = await alerter.send_verdict(make_verdict(final_score=60.0))
        assert result.reason == "below_threshold"
        assert called is False

    async def test_success_records_alert_and_suppresses_duplicates(self, make_verdict, store, monkeypatch):
        alerter = self.alerter(CONFIGURED, store=store)
        captured: dict[str, str] = {}

        async def fake_send(text, **kwargs):
            captured["text"] = text
            return AlertResult(sent=True, message_id=4242)

        monkeypatch.setattr(alerter, "send_text", fake_send)
        verdict = make_verdict()

        first = await alerter.send_verdict(verdict)
        assert first.sent is True
        assert "APPROVED" in captured["text"]
        assert store.alert_was_sent(verdict.token_address, ALERT_KIND) is True

        second = await alerter.send_verdict(verdict)
        assert second.sent is False
        assert second.reason == "duplicate"

        alerts = store.recent_alerts()
        assert len(alerts) == 1
        assert alerts[0]["payload"]["message_id"] == 4242

    async def test_explicit_already_alerted_short_circuits(self, make_verdict, store, monkeypatch):
        alerter = self.alerter(CONFIGURED, store=store)
        monkeypatch.setattr(alerter, "send_text", pytest.fail)  # must not be called
        result = await alerter.send_verdict(make_verdict(), already_alerted=True)
        assert result.reason == "duplicate"

    async def test_failed_send_is_not_recorded(self, make_verdict, store, monkeypatch):
        alerter = self.alerter(CONFIGURED, store=store)

        async def fake_send(text, **kwargs):
            return AlertResult(sent=False, reason="TelegramTimeout")

        monkeypatch.setattr(alerter, "send_text", fake_send)
        result = await alerter.send_verdict(make_verdict())
        assert result.sent is False
        assert store.recent_alerts() == []

    async def test_bookkeeping_failure_does_not_break_delivery(self, make_verdict, store, monkeypatch):
        alerter = self.alerter(CONFIGURED, store=store)

        async def fake_send(text, **kwargs):
            return AlertResult(sent=True, message_id=7)

        async def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(alerter, "send_text", fake_send)
        monkeypatch.setattr(store, "record_alert_async", boom)
        result = await alerter.send_verdict(make_verdict())
        assert result.sent is True

    async def test_dedupe_disabled_always_sends(self, make_verdict, store, monkeypatch):
        alerter = TelegramAlerter(CONFIGURED, store=store, dedupe_hours=0.0)

        async def fake_send(text, **kwargs):
            return AlertResult(sent=True)

        monkeypatch.setattr(alerter, "send_text", fake_send)
        assert (await alerter.send_verdict(make_verdict())).sent is True
        assert (await alerter.send_verdict(make_verdict())).sent is True

    async def test_a_veto_is_not_suppressed_by_an_earlier_approval(self, make_verdict, store, monkeypatch):
        alerter = self.alerter(CONFIGURED, store=store)
        sent: list[str] = []

        async def fake_send(text, **kwargs):
            sent.append(text)
            return AlertResult(sent=True, message_id=len(sent))

        monkeypatch.setattr(alerter, "send_text", fake_send)
        approved = make_verdict(status=VerdictStatus.APPROVED, final_score=90.0)
        vetoed = make_verdict(status=VerdictStatus.VETOED, final_score=10.0)

        assert (await alerter.send_verdict(approved)).sent is True
        assert (await alerter.send_verdict(vetoed)).sent is True
        assert len(sent) == 2
        assert "VETOED" in sent[1]

    async def test_a_dedupe_outage_fails_open(self, make_verdict, store, monkeypatch):
        alerter = self.alerter(CONFIGURED, store=store)

        def boom(*args, **kwargs):
            raise RuntimeError("database is locked")

        async def fake_send(text, **kwargs):
            return AlertResult(sent=True)

        monkeypatch.setattr(store, "alert_was_sent", boom)
        monkeypatch.setattr(alerter, "send_text", fake_send)

        # One duplicate is better than a dropped veto.
        assert (await alerter.send_verdict(make_verdict(status=VerdictStatus.VETOED))).sent is True

    async def test_failure_reason_never_carries_the_telegram_url(self, make_verdict, monkeypatch):
        telegram = TelegramSettings(bot_token=SecretStr("unit-test-token"), chat_id="1")
        alerter = TelegramAlerter(telegram)

        async def boom(session, url, **kwargs):
            raise HttpError(f"HTTP 500 from {url}", status=500, url=url)

        monkeypatch.setattr("bot.alerts.request_json", boom)
        try:
            result = await alerter.send_verdict(make_verdict())
        finally:
            await alerter.close()

        assert result.sent is False
        assert "unit-test-token" not in (result.reason or "")
        assert "api.telegram.org" not in (result.reason or "")
        assert result.reason == "HttpError: telegram request failed"
