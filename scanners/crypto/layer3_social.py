"""Layer 3 - social and sentiment intelligence.

Three independent reads are fused:

* **X/Twitter** - recent mention counts (velocity) plus engagement-weighted posts.
* **CryptoPanic** - curated news flow and community bullish/bearish votes.
* **LunarCrush** - social volume, sentiment and Galaxy Score.

All three need credentials, so an unkeyed deployment gets a zero-weight
``SKIPPED`` layer rather than a fabricated neutral reading.
"""

from __future__ import annotations

import asyncio
from typing import Any

from config.settings import Settings
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import Layer, LayerSignal, SignalStatus, TokenSnapshot
from scanners.base import BaseScanner, ScanContext, linear, log_scale
from scanners.social import LUNARCRUSH_COIN_URL

from scanners.shared.http_client import ProviderResult, fetch_provider, first_float, first_row, request_json, to_float

log = get_logger(__name__)


X_COUNTS_URL = "https://api.x.com/2/tweets/counts/recent"
X_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
CRYPTOPANIC_POSTS_URL = "https://cryptopanic.com/api/v1/posts/"


def parse_x_counts(payload: Any, *, buckets_per_window: int = 6) -> dict[str, Any]:
    """Turn hourly mention buckets into a velocity ratio.

    X returns newest-last buckets; the last ``buckets_per_window`` hours are
    compared with the equally sized window before them. A ratio above 1 means
    the conversation is accelerating.
    """
    rows = first_row(payload)
    counts: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = first_float(row, "tweet_count", "count")
        if value is not None:
            counts.append(int(value))
    if not counts:
        return {}
    window = max(1, min(buckets_per_window, len(counts) // 2 or 1))
    recent = sum(counts[-window:])
    prior = sum(counts[-2 * window : -window]) if len(counts) > window else 0
    velocity = (recent / prior) if prior > 0 else (99.0 if recent > 0 else 0.0)
    return {
        "mentions_total": sum(counts),
        "mentions_recent_window": recent,
        "mentions_prior_window": prior,
        "mentions_velocity": round(velocity, 3),
        "mention_buckets": len(counts),
    }


def parse_x_search(payload: Any) -> dict[str, Any]:
    """Aggregate recent posts: unique authors and engagement per tweet."""
    rows = first_row(payload)
    authors: set[str] = set()
    likes = retweets = replies = quotes = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        author = str(row.get("author_id") or "").strip()
        if author:
            authors.add(author)
        metrics = row.get("public_metrics") or {}
        likes += int(to_float(metrics.get("like_count")) or 0)
        retweets += int(to_float(metrics.get("retweet_count")) or 0)
        replies += int(to_float(metrics.get("reply_count")) or 0)
        quotes += int(to_float(metrics.get("quote_count")) or 0)
    if not rows:
        return {}
    posts = len(rows)
    engagement = likes + retweets * 2 + replies + quotes
    return {
        "x_posts": posts,
        "x_unique_authors": len(authors),
        "x_engagement": engagement,
        "x_engagement_per_post": round(engagement / posts, 2),
        "x_author_diversity": round(len(authors) / posts, 3),
    }


def parse_cryptopanic(payload: Any) -> dict[str, Any]:
    """News-flow breadth and community vote skew."""
    rows = first_row(payload)
    bullish = bearish = important = 0
    sources: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        votes = row.get("votes") or {}
        bullish += int(to_float(votes.get("positive")) or 0)
        bearish += int(to_float(votes.get("negative")) or 0)
        important += int(to_float(votes.get("important")) or 0)
        source = row.get("source")
        if isinstance(source, dict) and source.get("title"):
            sources.add(str(source["title"]))
    if not rows:
        return {}
    total_votes = bullish + bearish
    return {
        "news_posts": len(rows),
        "news_bullish_votes": bullish,
        "news_bearish_votes": bearish,
        "news_important": important,
        "news_sources": len(sources),
        "news_sentiment_ratio": round(bullish / total_votes, 3) if total_votes else None,
    }


def parse_lunarcrush(payload: Any) -> dict[str, Any]:
    """LunarCrush v4 wraps the metrics under ``data`` (or ``data.metrics``)."""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    metrics = data.get("metrics") if isinstance(data, dict) and isinstance(data.get("metrics"), dict) else data
    if not isinstance(metrics, dict):
        return {}
    volume = first_float(metrics, "social_volume_24h", "social_volume")
    sentiment = first_float(metrics, "sentiment")
    galaxy = first_float(metrics, "galaxy_score")
    if volume is None and sentiment is None and galaxy is None:
        return {}
    return {
        "lunarcrush_volume_24h": volume,
        "lunarcrush_sentiment": sentiment,
        "lunarcrush_galaxy": galaxy,
        "lunarcrush_contributors": first_float(metrics, "social_contributors", "contributors"),
    }


def velocity_score(velocity: float | None) -> float:
    """0.5x is quiet, 3x+ is a genuine attention spike."""
    if velocity is None:
        return 50.0
    return log_scale(velocity, 0.5, 3.0)


class CryptoSocialScanner(BaseScanner):
    """Layer 3: attention velocity, sentiment and KOL/news amplification."""

    layer = Layer.SOCIAL
    weight = 0.7
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        symbol = self._symbol_for(token, ctx)
        if not symbol:
            return self.skipped(token, "token symbol unknown; social lookup needs a ticker")

        calls = self._provider_calls(symbol, ctx)
        if not calls:
            return self.skipped(token, "no social provider configured (X, CryptoPanic, LunarCrush)")

        results: list[ProviderResult] = await asyncio.gather(
            *(fetch_provider(name, coro) for name, coro in calls.items())
        )
        metrics: dict[str, Any] = {}
        sources: list[str] = []
        for result in results:
            if result.ok:
                metrics.update(result.metrics)
                sources.append(result.source)
        if not metrics:
            raise RuntimeError("every configured social provider failed")

        velocity = to_float(metrics.get("mentions_velocity"))
        news_ratio = to_float(metrics.get("news_sentiment_ratio"))
        engagement = to_float(metrics.get("x_engagement_per_post"))
        galaxy = to_float(metrics.get("lunarcrush_galaxy"))
        sentiment = to_float(metrics.get("lunarcrush_sentiment"))

        components: list[tuple[float, float]] = []
        if velocity is not None:
            components.append((0.35, velocity_score(velocity)))
        if news_ratio is not None:
            components.append((0.25, linear(news_ratio, 0.25, 0.75)))
        if engagement is not None:
            components.append((0.20, log_scale(engagement, 2.0, 500.0)))
        if galaxy is not None:
            components.append((0.20, linear(galaxy, 20.0, 80.0)))
        elif sentiment is not None:
            components.append((0.20, linear(sentiment, 30.0, 75.0)))

        total_weight = sum(weight for weight, _ in components) or 1.0
        score = sum(weight * value for weight, value in components) / total_weight

        flags: list[str] = []
        if velocity is not None and velocity >= 3.0:
            flags.append("social_velocity_spike")
        if velocity is not None and velocity < 0.5:
            flags.append("attention_fading")
        if news_ratio is not None and news_ratio < 0.3:
            flags.append("news_sentiment_negative")
        if sentiment is not None and sentiment < 35.0:
            flags.append("social_sentiment_negative")
        if (to_float(metrics.get("mentions_total")) or 0) < 20:
            flags.append("thin_discussion")
        if (to_float(metrics.get("x_author_diversity")) or 1.0) < 0.5:
            flags.append("few_authors_dominate")

        confidence = 0.8 if len(sources) >= 2 else 0.55
        if "thin_discussion" in flags:
            confidence *= 0.75
        status = SignalStatus.OK if len(components) >= 3 else SignalStatus.DEGRADED

        velocity_text = f"{velocity:.2f}x" if velocity is not None else "n/a"
        summary = (
            f"{symbol}: mention velocity {velocity_text}, "
            f"news ratio {news_ratio if news_ratio is not None else 'n/a'}, "
            f"engagement/post {engagement if engagement is not None else 'n/a'}, "
            f"galaxy {galaxy if galaxy is not None else 'n/a'} [{'+'.join(sources)}]"
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
    @staticmethod
    def _symbol_for(token: TokenSnapshot, ctx: ScanContext) -> str:
        market = ctx.get_shared("market")
        base = getattr(market, "base_symbol", None)
        return (base or token.symbol or "").strip().upper()

    def _provider_calls(self, symbol: str, ctx: ScanContext) -> dict[str, Any]:
        calls: dict[str, Any] = {}
        if self.settings.social.x_bearer_token.is_configured():
            calls["x"] = self._x(symbol, ctx)
        if self.settings.providers.cryptopanic_token.is_configured():
            calls["cryptopanic"] = self._cryptopanic(symbol, ctx)
        if self.settings.social.lunarcrush_api_key.is_configured():
            calls["lunarcrush"] = self._lunarcrush(symbol, ctx)
        return calls

    async def _x(self, symbol: str, ctx: ScanContext) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.settings.social.x_bearer_token.get_secret_value()}"}
        query = f"${symbol} -is:retweet"
        counts, search = await asyncio.gather(
            request_json(
                ctx.session,
                X_COUNTS_URL,
                params={"query": query, "granularity": "hour"},
                headers=headers,
            ),
            request_json(
                ctx.session,
                X_SEARCH_URL,
                params={"query": query, "max_results": 100, "tweet.fields": "public_metrics,author_id"},
                headers=headers,
            ),
            return_exceptions=True,
        )
        metrics: dict[str, Any] = {}
        if not isinstance(counts, Exception):
            metrics.update(parse_x_counts(counts))
        if not isinstance(search, Exception):
            metrics.update(parse_x_search(search))
        return metrics

    async def _cryptopanic(self, symbol: str, ctx: ScanContext) -> dict[str, Any]:
        payload = await request_json(
            ctx.session,
            CRYPTOPANIC_POSTS_URL,
            params={
                "auth_token": self.settings.providers.cryptopanic_token.get_secret_value(),
                "currencies": symbol,
                "public": "true",
            },
        )
        return parse_cryptopanic(payload)

    async def _lunarcrush(self, symbol: str, ctx: ScanContext) -> dict[str, Any]:
        payload = await request_json(
            ctx.session,
            LUNARCRUSH_COIN_URL.format(coin=symbol),
            headers={
                "Authorization": f"Bearer {self.settings.social.lunarcrush_api_key.get_secret_value()}"
            },
        )
        return parse_lunarcrush(payload)


def build_scanner(settings: Settings) -> "CryptoSocialScanner":
    return CryptoSocialScanner(settings)


__all__ = [
    "CRYPTOPANIC_POSTS_URL",
    "CryptoSocialScanner",
    "X_COUNTS_URL",
    "X_SEARCH_URL",
    "build_scanner",
    "parse_cryptopanic",
    "parse_lunarcrush",
    "parse_x_counts",
    "parse_x_search",
    "velocity_score",
]
