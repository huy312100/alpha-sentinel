"""Telegram alerting for actionable verdicts.

Alerting policy is deliberately narrow:

* **APPROVED** verdicts at or above ``ALERT_MIN_SCORE``;
* **VETOED** verdicts only when ``ALERT_ON_VETO`` is set (a honeypot you already
  track is worth knowing about, but it must not spam every cycle).

Delivery is best-effort by design: a Telegram outage logs a warning and never
propagates into the scan loop, because losing an alert is annoying while losing
a scan cycle is a silent blind spot. Duplicate suppression goes through the hot
store so a restart or a second worker cannot hammer the same chat.
"""

from __future__ import annotations

import asyncio
import html
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp

from config.endpoints import EndpointSettings
from config.settings import TelegramSettings
from core.http import request_json
from core.logging import get_logger
from core.models import FinalVerdict, VerdictStatus
from storage.hot_store import HotStore

log = get_logger(__name__)

ALERT_KIND = "verdict"
TELEGRAM_LIMIT = 4_096
DEDUPE_WINDOW_HOURS = 12.0
_THESIS_LIMIT = 480
_SUMMARY_LIMIT = 320

_STATUS_EMOJI = {
    VerdictStatus.APPROVED: "\U0001F680",  # rocket
    VerdictStatus.WATCH: "\U0001F440",  # eyes
    VerdictStatus.REJECTED: "\U0001F44E",  # thumbs down
    VerdictStatus.VETOED: "\U0001F6D1",  # stop sign
}


def chart_url(verdict: FinalVerdict) -> str:
    """Deep link to the chart - the first thing a human clicks after an alert."""
    if (verdict.asset_class or "crypto") != "crypto":
        # Works for equities, ETFs, indices, FX, futures and yields alike.
        return f"https://www.tradingview.com/symbols/?search={quote(verdict.token_address)}"
    chain = verdict.chain or "ethereum"
    return f"https://dexscreener.com/{chain}/{verdict.token_address}"


def _market_line(verdict: FinalVerdict) -> str:
    """``us_equity · equity`` - venue and market, the identity a human reads."""
    return f"{verdict.chain} · {verdict.asset_class or 'crypto'}"


def _clip(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "\u2026"


def should_alert(verdict: FinalVerdict, telegram: TelegramSettings) -> bool:
    """Pure policy check, unit-testable without a network or a store."""
    if verdict.status is VerdictStatus.APPROVED:
        return verdict.final_score >= telegram.alert_min_score
    if verdict.status is VerdictStatus.VETOED:
        return telegram.alert_on_veto
    return False


def format_verdict(verdict: FinalVerdict, *, reasons: list[str] | None = None) -> str:
    """Render an HTML message for Telegram (all interpolated text is escaped)."""
    symbol = html.escape(verdict.symbol or verdict.token_address[:10])
    market = html.escape(_market_line(verdict))
    address = html.escape(verdict.token_address)
    emoji = _STATUS_EMOJI.get(verdict.status, "\u2139\uFE0F")
    lines = [
        f"{emoji} <b>{verdict.status.value.upper()} \u00b7 {symbol}</b>",
        f"<code>{address}</code> \u00b7 {market}",
        "",
        (
            f"Score <b>{verdict.final_score:.1f}</b> \u00b7 "
            f"prior {verdict.prior_score:.1f} \u2192 alpha {verdict.alpha.alpha_score:.1f} \u00b7 "
            f"risk {verdict.risk.risk_score:.1f} ({html.escape(verdict.risk.severity.value)})"
        ),
        f"Size <b>{verdict.position_multiplier:.2f}x</b> \u00b7 {verdict.latency_ms:.0f} ms",
    ]
    if reasons:
        lines.append("")
        lines.append("<b>Veto</b>: " + html.escape(_clip("; ".join(reasons), _SUMMARY_LIMIT)))
    if verdict.alpha.thesis:
        lines.append("")
        lines.append("<b>Thesis</b>: " + html.escape(_clip(verdict.alpha.thesis, _THESIS_LIMIT)))
    if verdict.alpha.catalysts:
        catalysts = ", ".join(_clip(item, 60) for item in verdict.alpha.catalysts[:4])
        lines.append("<b>Catalysts</b>: " + html.escape(catalysts))
    if verdict.risk.reasoning_summary:
        lines.append("")
        lines.append("<b>Risk</b>: " + html.escape(_clip(verdict.risk.reasoning_summary, _SUMMARY_LIMIT)))
    if verdict.security_flags:
        lines.append("<b>Security</b>: " + html.escape(", ".join(verdict.security_flags[:6])))
    lines.append("")
    lines.append(f'<a href="{chart_url(verdict)}">chart</a>')
    return "\n".join(lines)[:TELEGRAM_LIMIT]


def format_verdict_text(verdict: FinalVerdict, *, reasons: list[str] | None = None) -> str:
    """Plain-text twin used by the CLI and by logs."""
    text = (
        f"[{verdict.status.value.upper()}] {verdict.symbol or verdict.token_address[:10]} "
        f"on {_market_line(verdict)} - score {verdict.final_score:.1f} "
        f"(prior {verdict.prior_score:.1f}, alpha {verdict.alpha.alpha_score:.1f}, "
        f"risk {verdict.risk.risk_score:.1f}, size {verdict.position_multiplier:.2f}x)"
    )
    if reasons:
        text += " | veto: " + "; ".join(reasons)
    return text


@dataclass(slots=True)
class AlertResult:
    """Outcome of one delivery attempt - reported by the engine, never raised."""

    sent: bool
    reason: str = ""
    message_id: int | None = None
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"sent": self.sent, "skipped": self.skipped, "reason": self.reason, "message_id": self.message_id}


class TelegramAlerter:
    """Best-effort Telegram sender with on-disk duplicate suppression."""

    def __init__(
        self,
        telegram: TelegramSettings,
        *,
        endpoints: EndpointSettings | None = None,
        session: aiohttp.ClientSession | None = None,
        store: HotStore | None = None,
        dedupe_hours: float = DEDUPE_WINDOW_HOURS,
    ) -> None:
        self.telegram = telegram
        #: Bot API URL template; the token is substituted per send, never stored.
        self.endpoints = endpoints or EndpointSettings()
        self._session = session
        self._owns_session = session is None
        self.store = store
        self.dedupe_hours = max(dedupe_hours, 0.0)

    # -- lifecycle ----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.telegram.is_configured()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20.0),
                headers={"User-Agent": "AlphaSentinel/1.0"},
            )
            self._owns_session = True
        return self._session

    async def __aenter__(self) -> "TelegramAlerter":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()
        self._session = None

    # -- delivery -----------------------------------------------------------
    async def send_text(self, text: str, *, disable_preview: bool = True) -> AlertResult:
        if not self.enabled:
            return AlertResult(sent=False, skipped=True, reason="telegram_not_configured")
        payload = {
            "chat_id": self.telegram.chat_id,
            "text": text[:TELEGRAM_LIMIT],
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        try:
            response = await request_json(
                self.session,
                self.endpoints.url(
                    "telegram.send_message",
                    token=self.telegram.bot_token.get_secret_value(),
                ),
                method="POST",
                json_body=payload,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError, ValueError) as exc:
            # The URL embeds the bot token and HttpError text includes the URL.
            # Only the exception class is safe to store or log.
            log.warning("telegram_send_failed", extra={"error": type(exc).__name__})
            return AlertResult(sent=False, reason=f"{type(exc).__name__}: telegram request failed")
        message_id = None
        if isinstance(response, dict):
            result = response.get("result")
            if isinstance(result, dict):
                message_id = result.get("message_id")
        log.info("telegram_sent", extra={"message_id": message_id})
        return AlertResult(sent=True, message_id=message_id)

    async def send_verdict(
        self,
        verdict: FinalVerdict,
        *,
        reasons: list[str] | None = None,
        already_alerted: bool | None = None,
    ) -> AlertResult:
        """Send one verdict if policy allows and it is not a duplicate."""
        if not should_alert(verdict, self.telegram):
            return AlertResult(sent=False, skipped=True, reason="below_threshold")

        if already_alerted is None and self.store is not None and self.dedupe_hours > 0:
            try:
                already_alerted = await asyncio.to_thread(
                    self.store.alert_was_sent,
                    verdict.token_address,
                    ALERT_KIND,
                    chain=verdict.chain,
                    status=verdict.status.value,
                    within_hours=self.dedupe_hours,
                )
            except Exception as exc:  # noqa: BLE001 - a dedupe outage must not drop a veto
                log.warning("telegram_dedupe_lookup_failed", extra={"error": type(exc).__name__})
                already_alerted = False
        if already_alerted:
            return AlertResult(sent=False, skipped=True, reason="duplicate")

        result = await self.send_text(format_verdict(verdict, reasons=reasons))
        if result.sent and self.store is not None:
            try:
                await self.store.record_alert_async(
                    verdict.token_address,
                    verdict.chain,
                    ALERT_KIND,
                    message=f"{verdict.status.value} {verdict.final_score:.1f}",
                    payload={
                        "status": verdict.status.value,
                        "final_score": verdict.final_score,
                        "risk_score": verdict.risk.risk_score,
                        "message_id": result.message_id,
                    },
                    status=verdict.status.value,
                )
            except Exception as exc:  # noqa: BLE001 - bookkeeping must not break delivery
                log.warning("telegram_alert_bookkeeping_failed", extra={"error": str(exc)[:200]})
        return result


__all__ = [
    "ALERT_KIND",
    "AlertResult",
    "DEDUPE_WINDOW_HOURS",
    "TELEGRAM_LIMIT",
    "TelegramAlerter",
    "chart_url",
    "format_verdict",
    "format_verdict_text",
    "should_alert",
]
