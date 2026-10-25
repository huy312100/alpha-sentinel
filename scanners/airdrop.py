"""Airdrop / catalyst layer (CoinGecko, keyless).

The question this layer answers is not "is there a free token?" but "is there a
*reason* this token gets attention next week?" - an indexed listing with quest or
points-programme categories, watchlist growth and live development. Tokens that are
not indexed by CoinGecko are skipped rather than scored neutral.
"""

from __future__ import annotations

from typing import Any

from core.http import request_json, to_float
from core.instruments import AssetClass
from core.models import Layer, SignalStatus, LayerSignal, TokenSnapshot
from scanners.base import BaseScanner, ScanContext, linear, log_scale

COINGECKO_CONTRACT_URL = "https://api.coingecko.com/api/v3/coins/{platform}/contract/{address}"

# Our chain slug -> CoinGecko asset-platform id.
COINGECKO_PLATFORMS: dict[str, str] = {
    "ethereum": "ethereum",
    "bsc": "binance-smart-chain",
    "polygon": "polygon-pos",
    "arbitrum": "arbitrum-one",
    "base": "base",
}

CATALYST_KEYWORDS = ("airdrop", "points", "quest", "incentive", "farming", "retroactive", "launchpad")


def catalyst_categories(categories: Any) -> list[str]:
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


class AirdropScanner(BaseScanner):
    layer = Layer.AIRDROP
    weight = 0.6
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        platform = COINGECKO_PLATFORMS.get(token.chain)
        if platform is None:
            return self.skipped(token, f"CoinGecko platform mapping missing for {token.chain}")

        payload = await request_json(
            ctx.session,
            COINGECKO_CONTRACT_URL.format(platform=platform, address=token.token_address),
        )
        data = payload if isinstance(payload, dict) else {}
        if not data or not data.get("id"):
            return self.skipped(token, "token not indexed by CoinGecko")

        categories = data.get("categories") or []
        hits = catalyst_categories(categories)
        watchlist = to_float(data.get("watchlist_portfolio_users"))
        sentiment_up = to_float(data.get("sentiment_votes_up_percentage"))
        dev = data.get("developer_data") or {}
        commits_4w = to_float(dev.get("commit_count_4_weeks")) if isinstance(dev, dict) else None
        community = data.get("community_data") or {}
        followers = to_float(community.get("twitter_followers")) if isinstance(community, dict) else None

        if watchlist is None and sentiment_up is None and commits_4w is None:
            return self.unavailable(token, f"CoinGecko data incomplete for {data.get('id')}")

        flags: list[str] = []
        if commits_4w is not None and commits_4w == 0:
            flags.append("no_commits_4w")

        category_score = 88.0 if hits else 45.0
        watch_score = log_scale(watchlist, 1_000.0, 250_000.0)
        sentiment_score = linear(sentiment_up, 40.0, 90.0)
        dev_score = linear(commits_4w, 0.0, 60.0) if commits_4w is not None else 50.0

        score = 0.32 * category_score + 0.28 * watch_score + 0.20 * sentiment_score + 0.20 * dev_score
        confidence = 0.7 if hits else 0.55
        if commits_4w is None:
            confidence *= 0.8

        status = SignalStatus.OK if hits else SignalStatus.DEGRADED
        watchlist_text = f"{watchlist:,.0f}" if watchlist is not None else "n/a"
        summary = (
            f"{data.get('symbol', '').upper() or 'token'}: "
            f"{'catalyst in ' + ', '.join(hits[:2]) if hits else 'no explicit airdrop/points category'}; "
            f"watchlist {watchlist_text}, commits 4w {commits_4w if commits_4w is not None else 'n/a'}"
        )
        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=summary,
            status=status,
            metrics={
                "coingecko_id": data.get("id"),
                "categories": categories if isinstance(categories, list) else [],
                "catalyst_categories": hits,
                "watchlist_users": watchlist,
                "sentiment_up_pct": sentiment_up,
                "commits_4w": commits_4w,
                "twitter_followers": followers,
                "market_cap_rank": data.get("market_cap_rank"),
            },
            flags=flags,
        )


__all__ = ["AirdropScanner", "COINGECKO_PLATFORMS", "catalyst_categories"]
