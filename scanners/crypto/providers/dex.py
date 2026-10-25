"""DEX microstructure: pair discovery, liquidity, turnover and order flow.

Two free providers are used and cross-checked:

* **DexScreener** - best pair coverage, plus buy/sell counts per window.
* **GeckoTerminal** - independent liquidity/volume read and the OHLCV source used
  by the technicals layer.

Disagreement between them is itself a signal: if one reports 10x the liquidity of
the other, the layer is returned as ``DEGRADED`` instead of pretending to be precise.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

import aiohttp

from core.http import request_json, to_float, to_int
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import Layer, SignalStatus, LayerSignal, TokenSnapshot, clamp, get_chain_profile
from scanners.base import BaseScanner, ScanContext, linear, log_scale, safe_div

log = get_logger(__name__)

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
GECKO_TOKEN_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}/pools"

MIN_PLAUSIBLE_LIQUIDITY = 20_000.0
MAX_PLAUSIBLE_LIQUIDITY = 5_000_000.0


@dataclass(slots=True)
class DexMarket:
    """Best-known market snapshot for a token pair."""

    pair_address: str = ""
    dex_id: str = ""
    base_symbol: str = ""
    quote_symbol: str = "USD"
    price_usd: float | None = None
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    volume_1h_usd: float | None = None
    volume_6h_usd: float | None = None
    fdv_usd: float | None = None
    market_cap_usd: float | None = None
    price_change_1h_pct: float | None = None
    price_change_24h_pct: float | None = None
    buys_24h: int | None = None
    sells_24h: int | None = None
    buys_1h: int | None = None
    sells_1h: int | None = None
    pair_created_at: datetime | None = None
    sources: list[str] = field(default_factory=list)
    gecko_liquidity_usd: float | None = None

    @property
    def turnover(self) -> float | None:
        return safe_div(self.volume_24h_usd, self.liquidity_usd)

    @property
    def buy_ratio(self) -> float | None:
        total = (self.buys_24h or 0) + (self.sells_24h or 0)
        if not total:
            return None
        return (self.buys_24h or 0) / total

    @property
    def age_hours(self) -> float | None:
        if self.pair_created_at is None:
            return None
        delta = datetime.now(timezone.utc) - self.pair_created_at
        return max(0.0, delta.total_seconds() / 3600.0)

    @property
    def source_count(self) -> int:
        return len(set(self.sources))

    def to_metrics(self) -> dict[str, Any]:
        return {
            "pair_address": self.pair_address,
            "dex": self.dex_id,
            "price_usd": self.price_usd,
            "liquidity_usd": self.liquidity_usd,
            "gecko_liquidity_usd": self.gecko_liquidity_usd,
            "volume_24h_usd": self.volume_24h_usd,
            "volume_1h_usd": self.volume_1h_usd,
            "turnover": round(self.turnover, 3) if self.turnover is not None else None,
            "buy_ratio": round(self.buy_ratio, 3) if self.buy_ratio is not None else None,
            "price_change_1h_pct": self.price_change_1h_pct,
            "price_change_24h_pct": self.price_change_24h_pct,
            "fdv_usd": self.fdv_usd,
            "market_cap_usd": self.market_cap_usd,
            "age_hours": round(self.age_hours, 1) if self.age_hours is not None else None,
            "sources": sorted(set(self.sources)),
        }


def _pair_created_at(raw: Any) -> datetime | None:
    millis = to_int(raw)
    if millis is None or millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)


def market_from_dexscreener(pair: dict[str, Any]) -> DexMarket:
    """Normalise one DexScreener pair object."""
    txns = pair.get("txns") or {}
    h24 = txns.get("h24") or {}
    h1 = txns.get("h1") or {}
    volumes = pair.get("volume") or {}
    changes = pair.get("priceChange") or {}
    liquidity = pair.get("liquidity") or {}
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    return DexMarket(
        pair_address=str(pair.get("pairAddress") or ""),
        dex_id=str(pair.get("dexId") or ""),
        base_symbol=str(base.get("symbol") or ""),
        quote_symbol=str(quote.get("symbol") or "USD"),
        price_usd=to_float(pair.get("priceUsd")),
        liquidity_usd=to_float(liquidity.get("usd")),
        volume_24h_usd=to_float(volumes.get("h24")),
        volume_1h_usd=to_float(volumes.get("h1")),
        volume_6h_usd=to_float(volumes.get("h6")),
        fdv_usd=to_float(pair.get("fdv")),
        market_cap_usd=to_float(pair.get("marketCap")),
        price_change_1h_pct=to_float(changes.get("h1")),
        price_change_24h_pct=to_float(changes.get("h24")),
        buys_24h=to_int(h24.get("buys")),
        sells_24h=to_int(h24.get("sells")),
        buys_1h=to_int(h1.get("buys")),
        sells_1h=to_int(h1.get("sells")),
        pair_created_at=_pair_created_at(pair.get("pairCreatedAt")),
        sources=["dexscreener"],
    )


def _addresses_match(left: Any, right: str) -> bool:
    return str(left or "").strip().lower() == str(right or "").strip().lower()


def _pick_best_pair(pairs: list[dict[str, Any]], chain: str, token_address: str) -> dict[str, Any] | None:
    """Prefer base-token pairs on the requested chain, then the deepest liquidity.

    DexScreener returns pairs where the scanned token is *either* side. Only the
    base side's ``priceUsd``/``baseToken``/buy-sell counts describe our token, so a
    quote-side pool would silently substitute another asset's price.
    """
    if not pairs:
        return None
    base_pairs = [
        pair for pair in pairs if _addresses_match((pair.get("baseToken") or {}).get("address"), token_address)
    ]
    if not base_pairs:
        return None
    chain_id = chain.lower()
    same_chain = [pair for pair in base_pairs if str(pair.get("chainId", "")).lower() == chain_id]
    candidates = same_chain or base_pairs
    return max(
        candidates,
        key=lambda pair: to_float((pair.get("liquidity") or {}).get("usd")) or 0.0,
    )


async def fetch_dexscreener_market(
    session: aiohttp.ClientSession,
    token: TokenSnapshot,
) -> DexMarket | None:
    payload = await request_json(session, DEXSCREENER_TOKEN_URL.format(address=token.token_address))
    pairs = payload.get("pairs") if isinstance(payload, dict) else None
    if not isinstance(pairs, list):
        return None
    best = _pick_best_pair(pairs, token.chain, token.token_address)
    return market_from_dexscreener(best) if best else None


def _gecko_base_address(row: Mapping[str, Any]) -> str:
    """Extract the base-token address from a GeckoTerminal pool row (``net_0x...``)."""
    relationships = row.get("relationships") or {}
    data = (relationships.get("base_token") or {}).get("data") or {}
    identifier = str(data.get("id") or "")
    return identifier.split("_", 1)[1] if "_" in identifier else ""


def _gecko_row_is_base(row: Any, token_address: str) -> bool:
    if not isinstance(row, Mapping):
        return False
    base = _gecko_base_address(row)
    # Older payloads omit the relationship; keep the row only when we cannot tell.
    return not base or _addresses_match(base, token_address)


async def fetch_gecko_market(
    session: aiohttp.ClientSession,
    token: TokenSnapshot,
    *,
    headers: dict[str, str] | None = None,
) -> DexMarket | None:
    network = get_chain_profile(token.chain).geckoterminal
    payload = await request_json(
        session,
        GECKO_TOKEN_POOLS_URL.format(network=network, address=token.token_address),
        headers=headers,
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    rows = [row for row in rows if _gecko_row_is_base(row, token.token_address)]
    if not rows:
        return None
    best = max(rows, key=lambda row: to_float((row.get("attributes") or {}).get("reserve_in_usd")) or 0.0)
    attrs = best.get("attributes") or {}
    volume = attrs.get("volume_usd") or {}
    txns = (attrs.get("transactions") or {}).get("h24") or {}
    changes = attrs.get("price_change_percentage") or {}
    return DexMarket(
        pair_address=str(attrs.get("address") or ""),
        dex_id=str(attrs.get("dex_id") or best.get("relationships", {}).get("dex", {}).get("data", {}).get("id", "")),
        base_symbol=str(attrs.get("base_token_symbol") or ""),
        quote_symbol=str(attrs.get("quote_token_symbol") or "USD"),
        price_usd=to_float(attrs.get("base_token_price_usd")),
        liquidity_usd=to_float(attrs.get("reserve_in_usd")),
        gecko_liquidity_usd=to_float(attrs.get("reserve_in_usd")),
        volume_24h_usd=to_float(volume.get("h24")),
        volume_1h_usd=to_float(volume.get("h1")),
        price_change_1h_pct=to_float(changes.get("h1")),
        price_change_24h_pct=to_float(changes.get("h24")),
        buys_24h=to_int(txns.get("buys")),
        sells_24h=to_int(txns.get("sells")),
        fdv_usd=to_float(attrs.get("fdv_usd")),
        market_cap_usd=to_float(attrs.get("market_cap_usd")),
        pair_created_at=_pair_created_at(attrs.get("pool_created_at")),
        sources=["geckoterminal"],
    )


def merge_markets(primary: DexMarket | None, secondary: DexMarket | None) -> DexMarket | None:
    """Keep the richer DexScreener read, fill gaps from GeckoTerminal, flag conflict."""
    if primary is None:
        return secondary
    if secondary is None:
        return primary
    merged = primary
    merged.sources = sorted(set(primary.sources + secondary.sources))
    merged.gecko_liquidity_usd = secondary.liquidity_usd
    for attr in (
        "volume_24h_usd",
        "volume_1h_usd",
        "price_change_1h_pct",
        "price_change_24h_pct",
        "fdv_usd",
        "market_cap_usd",
        "buys_24h",
        "sells_24h",
        "pair_created_at",
    ):
        if getattr(merged, attr) in (None, 0, "") and getattr(secondary, attr) not in (None, 0, ""):
            setattr(merged, attr, getattr(secondary, attr))
    if not merged.pair_address:
        merged.pair_address = secondary.pair_address
    return merged


async def load_market(token: TokenSnapshot, ctx: ScanContext) -> DexMarket | None:
    """Fetch both providers once per token and cache the merged view in the context."""
    cached = ctx.get_shared("market")
    if isinstance(cached, DexMarket):
        return cached

    results = await asyncio.gather(
        fetch_dexscreener_market(ctx.session, token),
        fetch_gecko_market(ctx.session, token),
        return_exceptions=True,
    )
    markets: list[DexMarket] = []
    for result in results:
        if isinstance(result, DexMarket):
            markets.append(result)
        elif isinstance(result, Exception):
            log.debug("dex_provider_failed", extra={"token": token.token_address, "error": str(result)[:200]})
    primary = next((market for market in markets if "dexscreener" in market.sources), None)
    secondary = next((market for market in markets if "geckoterminal" in market.sources), None)
    merged = merge_markets(primary, secondary)
    if merged is not None:
        ctx.set_shared("market", merged)
    return merged


def _liquidity_conflict(market: DexMarket) -> bool:
    if not market.liquidity_usd or not market.gecko_liquidity_usd:
        return False
    high = max(market.liquidity_usd, market.gecko_liquidity_usd)
    low = min(market.liquidity_usd, market.gecko_liquidity_usd)
    return low > 0 and high / low > 3.0


class DexScanner(BaseScanner):
    """Scores market quality: depth, turnover sanity, order-flow skew, momentum."""

    layer = Layer.DEX
    weight = 1.2
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        market = await load_market(token, ctx)
        if market is None or not market.price_usd:
            return self.unavailable(token, "no DEX pair found")

        flags: list[str] = []
        liquidity = market.liquidity_usd or 0.0
        if liquidity < MIN_PLAUSIBLE_LIQUIDITY:
            flags.append("liquidity_below_20k")
        turnover = market.turnover
        if turnover is not None and turnover > 10:
            flags.append("turnover_above_10x_liquidity")
        if market.buy_ratio is not None and market.buy_ratio > 0.75:
            flags.append("buy_ratio_extreme_high")
        if market.buy_ratio is not None and market.buy_ratio < 0.25:
            flags.append("buy_ratio_extreme_low")
        age_hours = market.age_hours
        if age_hours is not None and age_hours < 48:
            flags.append("pair_younger_than_48h")
        conflict = _liquidity_conflict(market)
        if conflict:
            flags.append("provider_liquidity_conflict")

        depth_score = log_scale(liquidity, MIN_PLAUSIBLE_LIQUIDITY, MAX_PLAUSIBLE_LIQUIDITY)
        if turnover is None:
            turnover_score = 50.0
        else:
            # Healthy turnover is ~0.2x-3x of liquidity; both extremes are suspicious.
            deviation = abs(math.log10(max(turnover, 1e-4)) - math.log10(0.8))
            turnover_score = clamp(100.0 - deviation * 45.0)
        flow_score = linear(market.buy_ratio, 0.30, 0.70) if market.buy_ratio is not None else 50.0
        momentum_score = linear(market.price_change_24h_pct, -40.0, 40.0)

        score = (
            0.40 * depth_score
            + 0.25 * turnover_score
            + 0.20 * flow_score
            + 0.15 * momentum_score
        )
        if "liquidity_below_20k" in flags:
            score = min(score, 35.0)
        if conflict:
            score *= 0.9

        confidence = 0.85 if market.source_count >= 2 else 0.6
        if liquidity < MIN_PLAUSIBLE_LIQUIDITY:
            confidence *= 0.7
        if market.volume_24h_usd is None:
            confidence *= 0.6

        status = SignalStatus.DEGRADED if (conflict or market.volume_24h_usd is None) else SignalStatus.OK
        volume_text = f"${market.volume_24h_usd:,.0f}" if market.volume_24h_usd is not None else "n/a"
        turnover_text = f"{turnover:.2f}x" if turnover is not None else "n/a"
        buy_text = f"{market.buy_ratio:.0%}" if market.buy_ratio is not None else "n/a"
        summary = (
            f"{market.dex_id or 'dex'} pair {market.pair_address[:10]}..: "
            f"liq ${liquidity:,.0f}, 24h vol {volume_text}, "
            f"turnover {turnover_text}, buy ratio {buy_text}"
        )
        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=summary,
            status=status,
            metrics=market.to_metrics(),
            flags=flags,
        )


__all__ = [
    "DexMarket",
    "DexScanner",
    "fetch_dexscreener_market",
    "fetch_gecko_market",
    "load_market",
    "market_from_dexscreener",
    "merge_markets",
]
