"""Layer 6 - tokenomics, dilution overhang and dated catalysts.

Three reads:

* **CoinGecko** (keyless) - circulating/total/max supply, float ratio and FDV
  versus market cap. This is the dilution overhang that price must absorb.
* **Catalyst tagging** (same CoinGecko payload) - a category naming an airdrop,
  points, quest or farming programme, plus watchlist growth and commit activity.
  This is the "why would anyone care next week" half of the thesis, so it is
  scored only when present instead of being averaged in as a neutral 50.
* **Event calendar** (optional) - a JSON feed of unlock/vesting cliffs and
  mainnet upgrades. Configure ``TOKENOMICS_CALENDAR_URL`` to point at
  TokenUnlocks, an internal sheet export or your own service; the parser accepts
  a list, ``{"events": [...]}`` or ``{"data": [...]}`` with
  ``symbol``/``type``/``date``/``pct_of_supply`` fields.

Without a calendar the layer still scores the float; it reports ``DEGRADED``
because the unlock half of the thesis is unknown, never a fake neutral 50.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from config.settings import Settings
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import Layer, LayerSignal, SignalStatus, TokenSnapshot, clamp, utcnow
from scanners.base import BaseScanner, ScanContext, linear, log_scale
from scanners.shared.http_client import (
    ProviderResult,
    fetch_provider,
    first_float,
    request_json,
    to_float,
    to_int,
)

log = get_logger(__name__)


#: Our chain slug -> CoinGecko asset-platform id.
COINGECKO_PLATFORMS: dict[str, str] = {
    "ethereum": "ethereum",
    "bsc": "binance-smart-chain",
    "polygon": "polygon-pos",
    "arbitrum": "arbitrum-one",
    "base": "base",
}

#: Category keywords that name a live incentive programme - a reason for attention.
CATALYST_KEYWORDS = ("airdrop", "points", "quest", "incentive", "farming", "retroactive", "launchpad")


def catalyst_categories(categories: Any) -> list[str]:
    """Keep only the CoinGecko categories that promise a reward programme."""
    if not isinstance(categories, list):
        return []
    hits: list[str] = []
    for category in categories:
        if not isinstance(category, str):
            continue
        lowered = category.lower()
        if any(keyword in lowered for keyword in CATALYST_KEYWORDS):
            hits.append(category)
    return hits


def parse_catalysts(payload: Any) -> dict[str, Any]:
    """Read the attention half of CoinGecko's contract payload.

    A category is only a catalyst when it names an incentive programme; a token
    filed under "DeFi" has none. Watchlist growth and live commits are the
    supporting evidence that the programme is still running, so an unindexed
    token yields nothing rather than a neutral score.
    """
    if not isinstance(payload, dict) or not payload.get("id"):
        return {}
    metrics: dict[str, Any] = {
        "coingecko_id": payload.get("id"),
        "catalyst_categories": catalyst_categories(payload.get("categories")),
        "watchlist_users": to_float(payload.get("watchlist_portfolio_users")),
        "sentiment_up_pct": to_float(payload.get("sentiment_votes_up_percentage")),
        "market_cap_rank": to_int(payload.get("market_cap_rank")),
    }
    developer = payload.get("developer_data")
    if isinstance(developer, dict):
        metrics["commits_4w"] = to_float(developer.get("commit_count_4_weeks"))
    community = payload.get("community_data")
    if isinstance(community, dict):
        metrics["twitter_followers"] = to_float(community.get("twitter_followers"))
    return metrics


UNLOCK_TYPES = {"unlock", "vesting", "cliff", "token_unlock", "emission"}
UPGRADE_TYPES = {"upgrade", "mainnet", "hard_fork", "fork", "release"}


def parse_supply(payload: Any) -> dict[str, Any]:
    """Normalise CoinGecko's supply block into float and valuation metrics."""
    if not isinstance(payload, dict):
        return {}
    market = payload.get("market_data") or {}
    if not isinstance(market, dict):
        return {}
    circulating = first_float(market, "circulating_supply")
    total = first_float(market, "total_supply")
    max_supply = first_float(market, "max_supply")
    mcap = first_float(market.get("market_cap") or {}, "usd")
    fdv = first_float(market.get("fully_diluted_valuation") or {}, "usd")
    if circulating is None and total is None:
        return {}
    denominator = total or max_supply or circulating
    float_ratio = circulating / denominator if circulating and denominator else None
    return {
        "coingecko_id": payload.get("id"),
        "circulating_supply": circulating,
        "total_supply": total,
        "max_supply": max_supply,
        "float_ratio": round(float_ratio, 4) if float_ratio is not None else None,
        "market_cap_usd": mcap,
        "fdv_usd": fdv,
        "fdv_over_mcap": round(fdv / mcap, 3) if fdv and mcap and mcap > 0 else None,
    }


def _calendar_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("events", "data", "items", "calendar"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return rows
    return []


def _event_date(row: dict[str, Any]) -> datetime | None:
    raw = row.get("date") or row.get("unlock_date") or row.get("timestamp") or row.get("at")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        seconds = float(raw) / 1000.0 if float(raw) > 1e11 else float(raw)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_event_calendar(payload: Any, symbol: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Extract the next unlock/upgrade for one symbol from a dated event feed."""
    reference = now or utcnow()
    wanted = symbol.strip().upper()
    unlocks: list[tuple[float, float | None, bool]] = []
    upgrades: list[float] = []
    considered = 0
    for row in _calendar_rows(payload):
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol") or row.get("token") or row.get("ticker") or "").upper()
        if wanted and row_symbol and row_symbol != wanted:
            continue
        when = _event_date(row)
        if when is None:
            continue
        days = (when - reference).total_seconds() / 86_400.0
        if days < -0.5:
            continue
        kinds = str(row.get("type") or row.get("event") or row.get("category") or "").lower()
        considered += 1
        if any(kind in kinds for kind in UNLOCK_TYPES):
            percent = first_float(row, "pct_of_supply", "percent_of_supply", "unlock_pct", "percent")
            unlocks.append((days, percent, bool(row.get("cliff"))))
        elif any(kind in kinds for kind in UPGRADE_TYPES):
            upgrades.append(days)
    if considered == 0:
        return {}

    metrics: dict[str, Any] = {"events_considered": considered}
    if unlocks:
        unlocks.sort(key=lambda item: item[0])
        next_days, next_pct, next_cliff = unlocks[0]
        within_30 = [item for item in unlocks if item[0] <= 30.0]
        metrics.update(
            {
                "next_unlock_days": round(next_days, 1),
                "unlock_pct_next": next_pct,
                "next_unlock_is_cliff": next_cliff,
                "unlocks_within_30d": len(within_30),
                "cliff_unlocks_within_30d": sum(1 for item in within_30 if item[2]),
                "unlock_pct_30d": (
                    round(sum(item[1] for item in within_30 if item[1] is not None), 3)
                    if any(item[1] is not None for item in within_30)
                    else None
                ),
            }
        )
    if upgrades:
        upgrades.sort()
        metrics.update(
            {
                "next_upgrade_days": round(upgrades[0], 1),
                "upgrades_within_30d": sum(1 for days in upgrades if days <= 30.0),
            }
        )
    return metrics


class CryptoTokenomicsScanner(BaseScanner):
    """Layer 6: float, dilution overhang, unlock cliffs and upgrade catalysts."""

    layer = Layer.TOKENOMICS
    weight = 0.7
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        symbol = (token.symbol or "").strip().upper()
        metrics: dict[str, Any] = {}
        sources: list[str] = []

        calls: dict[str, Any] = {}
        supply_call = self._supply(token, ctx)
        if supply_call is not None:
            calls["coingecko"] = supply_call
        if symbol:
            calendar_call = self._calendar(symbol, ctx)
            if calendar_call is not None:
                calls["calendar"] = calendar_call
        if not calls:
            return self.skipped(token, "no tokenomics provider available for this token")

        results: list[ProviderResult] = await asyncio.gather(
            *(fetch_provider(name, coro) for name, coro in calls.items())
        )
        for result in results:
            if result.ok:
                metrics.update(result.metrics)
                sources.append(result.source)
        if not metrics:
            return self.skipped(token, "tokenomics providers returned no data")

        float_ratio = to_float(metrics.get("float_ratio"))
        unlock_pct_30d = to_float(metrics.get("unlock_pct_30d"))
        next_unlock_days = to_float(metrics.get("next_unlock_days"))
        next_upgrade_days = to_float(metrics.get("next_upgrade_days"))
        catalyst_hits = metrics.get("catalyst_categories") or []
        watchlist_users = to_float(metrics.get("watchlist_users"))
        commits_4w = to_float(metrics.get("commits_4w"))

        components: list[tuple[float, float]] = []
        if float_ratio is not None:
            components.append((0.45, linear(float_ratio * 100.0, 20.0, 90.0)))
        if unlock_pct_30d is not None:
            components.append((0.35, clamp(100.0 - unlock_pct_30d * 8.0)))
        if next_upgrade_days is not None:
            components.append((0.20, 65.0 if next_upgrade_days <= 30.0 else 50.0))
        # Only positive catalysts are added: "no airdrop" is not a tokenomics flaw.
        if catalyst_hits:
            components.append((0.20, 88.0))
        if watchlist_users is not None:
            components.append((0.10, log_scale(watchlist_users, 1_000.0, 250_000.0)))

        total_weight = sum(weight for weight, _ in components) or 1.0
        score = sum(weight * value for weight, value in components) / total_weight

        flags: list[str] = []
        if float_ratio is not None and float_ratio < 0.35:
            flags.append("low_float_high_fdv")
        if unlock_pct_30d is not None and unlock_pct_30d >= 5.0:
            flags.append("large_unlock_above_5pct")
        if next_unlock_days is not None and next_unlock_days <= 14.0:
            flags.append("unlock_within_14d")
        if metrics.get("next_unlock_is_cliff"):
            flags.append("next_unlock_is_cliff")
        if next_upgrade_days is not None and next_upgrade_days <= 30.0:
            flags.append("mainnet_upgrade_within_30d")
        if commits_4w is not None and commits_4w <= 0:
            flags.append("no_commits_4w")

        confidence = 0.75 if "calendar" in sources and "coingecko" in sources else 0.55
        if "calendar" not in sources:
            confidence *= 0.8
        status = SignalStatus.OK if "calendar" in sources and float_ratio is not None else SignalStatus.DEGRADED

        float_text = f"{float_ratio:.1%}" if float_ratio is not None else "n/a"
        unlock_text = f"{unlock_pct_30d:.2f}% in 30d" if unlock_pct_30d is not None else "no unlock calendar"
        upgrade_text = f"{next_upgrade_days:.0f}d" if next_upgrade_days is not None else "n/a"
        catalyst_text = f", catalyst {', '.join(catalyst_hits[:2])}" if catalyst_hits else ""
        summary = (
            f"{symbol or 'token'}: float {float_text}, {unlock_text}, "
            f"next upgrade {upgrade_text}{catalyst_text} [{'+'.join(sources)}]"
        )
        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=summary,
            status=status,
            metrics={**metrics, "sources": sources, "flags": flags},
            flags=flags,
        )

    # -- providers -----------------------------------------------------------
    def _supply(self, token: TokenSnapshot, ctx: ScanContext) -> Any:
        platform = COINGECKO_PLATFORMS.get(token.chain)
        if platform is None:
            return None

        async def fetch() -> dict[str, Any]:
            payload = await request_json(
                ctx.session,
                ctx.settings.endpoints.url(
                    "coingecko.contract", platform=platform, address=token.token_address
                ),
            )
            return {**parse_supply(payload), **parse_catalysts(payload)}

        return fetch()

    def _calendar(self, symbol: str, ctx: ScanContext) -> Any:
        url = self.settings.providers.tokenomics_calendar_url.strip()
        if not url:
            return None

        async def fetch() -> dict[str, Any]:
            payload = await request_json(ctx.session, url)
            return parse_event_calendar(payload, symbol)

        return fetch()


def build_scanner(settings: Settings) -> CryptoTokenomicsScanner:
    return CryptoTokenomicsScanner(settings)


__all__ = [
    "CryptoTokenomicsScanner",
    "UNLOCK_TYPES",
    "UPGRADE_TYPES",
    "build_scanner",
    "parse_event_calendar",
    "parse_supply",
]
