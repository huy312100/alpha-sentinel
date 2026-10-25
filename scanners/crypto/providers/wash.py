"""Wash-trading filter: is this volume real?

Volume is the cheapest thing to fake on a DEX: two wallets, one bot, and a token
looks liquid. We pull the recent trade tape from GeckoTerminal and look for the
fingerprints of churn - a handful of wallets producing most of the volume, repeated
identical trade sizes, and rapid self-trading that nets to nothing.

The output is a :class:`WashTradingReport` whose ``suspicion_score`` (0-100) the
pipeline compares against ``WASH_SUSPICION_THRESHOLD``.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any, Sequence

from core.http import request_json, to_float
from core.logging import get_logger
from core.models import TokenSnapshot, WashTradingReport, get_chain_profile
from scanners.base import ScanContext, clamp

log = get_logger(__name__)

GECKO_TRADES_URL = "https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/trades"
WASH_SUSPICION_THRESHOLD = 85.0
MIN_TRADES_FOR_VERDICT = 30

# Wallets trading within this window, in alternating directions, look like churn.
SELF_TRADE_WINDOW_SECONDS = 120


def _epoch_seconds(raw: Any) -> float | None:
    """Parse GeckoTerminal's ISO-8601 ``block_timestamp`` (or an epoch number).

    ``to_float`` returns ``None`` for ``2026-10-25T12:00:00Z``; treating that as
    ``0.0`` made every trade look simultaneous and inflated the self-trading score.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return None
    numeric = to_float(text)
    if numeric is not None:
        return numeric
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _parse_trades(payload: Any) -> list[dict[str, Any]]:
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    trades: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        attrs = row.get("attributes") or {}
        volume = to_float(attrs.get("volume_in_usd"))
        if volume is None or volume <= 0:
            continue
        trades.append(
            {
                "wallet": str(attrs.get("tx_from_address") or "unknown").lower(),
                "kind": str(attrs.get("kind") or "").lower(),
                "volume_usd": volume,
                "timestamp": _epoch_seconds(attrs.get("block_timestamp")),
                "tx_hash": str(attrs.get("tx_hash") or ""),
            }
        )
    return trades


def is_round_amount(volume: float) -> bool:
    """Heuristic: trade sizes that land on clean multiples are usually scripts."""
    if volume <= 0:
        return False
    for step in (1_000.0, 500.0, 100.0, 50.0, 10.0):
        if volume >= step and abs(volume % step) < 0.01:
            return True
    return False


def wallet_concentration(trades: Sequence[dict[str, Any]]) -> float:
    """Share of total volume produced by the top 3 wallets (0-1)."""
    total = sum(trade["volume_usd"] for trade in trades)
    if total <= 0:
        return 0.0
    by_wallet: dict[str, float] = {}
    for trade in trades:
        by_wallet[trade["wallet"]] = by_wallet.get(trade["wallet"], 0.0) + trade["volume_usd"]
    top = sorted(by_wallet.values(), reverse=True)[:3]
    return min(1.0, sum(top) / total)


def self_trade_share(trades: Sequence[dict[str, Any]], window: int = SELF_TRADE_WINDOW_SECONDS) -> float:
    """Fraction of timestamped trades that alternate direction on one wallet."""
    timed = [trade for trade in trades if trade.get("timestamp") is not None]
    if len(timed) < 2:
        return 0.0
    ordered = sorted(timed, key=lambda trade: trade["timestamp"])
    last_seen: dict[str, tuple[str, float]] = {}
    flagged = 0
    for trade in ordered:
        wallet = trade["wallet"]
        previous = last_seen.get(wallet)
        if previous is not None:
            previous_kind, previous_ts = previous
            if previous_kind and previous_kind != trade["kind"] and abs(trade["timestamp"] - previous_ts) <= window:
                flagged += 1
        last_seen[wallet] = (trade["kind"], trade["timestamp"])
    return flagged / len(ordered)


def buy_sell_imbalance(trades: Sequence[dict[str, Any]]) -> float:
    """``|buy - sell| / total`` - near zero despite heavy volume is suspicious churn."""
    buys = sum(trade["volume_usd"] for trade in trades if trade["kind"] == "buy")
    sells = sum(trade["volume_usd"] for trade in trades if trade["kind"] == "sell")
    total = buys + sells
    return abs(buys - sells) / total if total > 0 else 0.0


def analyse_trades(trades: Sequence[dict[str, Any]]) -> WashTradingReport:
    """Turn a trade tape into a scored report. Pure function - easy to unit test."""
    if not trades:
        return WashTradingReport(evaluated=False, flags=["no_trade_tape"])

    volumes = [trade["volume_usd"] for trade in trades]
    wallets = {trade["wallet"] for trade in trades}
    unique_ratio = len(wallets) / len(trades)
    round_ratio = sum(1 for volume in volumes if is_round_amount(volume)) / len(volumes)
    self_ratio = self_trade_share(trades)
    imbalance = buy_sell_imbalance(trades)
    concentration = wallet_concentration(trades)

    flags: list[str] = []
    score = 0.0
    # Each signal is bounded; none of them alone can condemn a token.
    if len(trades) >= MIN_TRADES_FOR_VERDICT:
        if unique_ratio < 0.2:
            flags.append("few_wallets_many_trades")
            score += 30.0
        elif unique_ratio < 0.35:
            flags.append("low_wallet_diversity")
            score += 15.0
        if concentration > 0.7:
            flags.append("top3_wallets_dominate_volume")
            score += 25.0
        elif concentration > 0.5:
            flags.append("top3_wallets_majority_volume")
            score += 12.0
        if round_ratio > 0.5:
            flags.append("repeated_round_trade_sizes")
            score += 20.0
        elif round_ratio > 0.3:
            flags.append("many_round_trade_sizes")
            score += 10.0
        if self_ratio > 0.25:
            flags.append("rapid_self_trading")
            score += 25.0
        elif self_ratio > 0.12:
            flags.append("some_self_trading")
            score += 12.0
        if imbalance < 0.05 and len(trades) > 100:
            flags.append("balanced_churn_no_net_flow")
            score += 10.0
    else:
        flags.append("thin_trade_tape")
        score += 10.0

    return WashTradingReport(
        suspicion_score=round(clamp(score), 2),
        trade_count=len(trades),
        unique_wallets=len(wallets),
        avg_trade_usd=round(sum(volumes) / len(volumes), 2),
        median_trade_usd=round(statistics.median(volumes), 2),
        unique_wallet_ratio=round(unique_ratio, 4),
        round_amount_ratio=round(round_ratio, 4),
        self_trade_ratio=round(self_ratio, 4),
        buy_sell_imbalance=round(imbalance, 4),
        flags=flags,
        metrics={
            "top3_wallet_volume_share": round(concentration, 4),
            "tape_volume_usd": round(sum(volumes), 2),
        },
        evaluated=True,
    )


async def fetch_wash_report(token: TokenSnapshot, ctx: ScanContext, pool: str) -> WashTradingReport:
    network = get_chain_profile(token.chain).geckoterminal
    payload = await request_json(
        ctx.session,
        GECKO_TRADES_URL.format(network=network, pool=pool),
        params={"trade_volume_in_usd_greater_than": 0},
    )
    return analyse_trades(_parse_trades(payload))


class WashTradingFilter:
    """Thin adapter so the pipeline can call ``await wash.evaluate(...)``."""

    def __init__(self, threshold: float = WASH_SUSPICION_THRESHOLD) -> None:
        self.threshold = threshold

    def is_compromised(self, report: WashTradingReport) -> bool:
        return report.evaluated and report.suspicion_score >= self.threshold

    async def evaluate(self, token: TokenSnapshot, ctx: ScanContext, pool: str | None) -> WashTradingReport:
        if not pool:
            return WashTradingReport(evaluated=False, flags=["no_pool_address"])
        try:
            report = await fetch_wash_report(token, ctx, pool)
        except Exception as exc:  # noqa: BLE001 - an unreachable tape is not evidence
            log.debug("wash_tape_failed", extra={"token": token.token_address, "error": str(exc)[:200]})
            return WashTradingReport(evaluated=False, flags=["trade_tape_unavailable"])
        if self.is_compromised(report):
            log.warning(
                "wash_trading_suspected",
                extra={
                    "token": token.token_address,
                    "suspicion": report.suspicion_score,
                    "flags": report.flags,
                },
            )
        return report


__all__ = [
    "GECKO_TRADES_URL",
    "MIN_TRADES_FOR_VERDICT",
    "WASH_SUSPICION_THRESHOLD",
    "WashTradingFilter",
    "analyse_trades",
    "buy_sell_imbalance",
    "fetch_wash_report",
    "is_round_amount",
    "self_trade_share",
    "wallet_concentration",
]
