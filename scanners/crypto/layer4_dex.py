"""Layer 4 - DEX microstructure: pool discovery, depth, turnover and volume spikes.

Layer 4 owns the token's DEX pair. Two free, keyless providers are queried once
by :func:`load_market` and cross-checked:

* **DexScreener** - best pair coverage, plus buy/sell counts per window.
* **GeckoTerminal** - an independent liquidity/volume read, and the pool the
  technicals layer fetches candles from.

Disagreement between the two is itself a signal: if one reports 10x the liquidity
of the other the layer returns ``DEGRADED`` instead of pretending to be precise.

The layer also owns trade-tape integrity: :class:`WashTradingFilter` reads
GeckoTerminal's recent trades and decides whether the volume behind a spike is
real. Every other consumer (layers 2, 7, the pipeline) reads the merged pair
through :func:`load_market`, which caches on the scan context, so a token costs
one pair lookup per scan - not one per layer.
"""

from __future__ import annotations

import asyncio
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import aiohttp

from config.settings import EndpointSettings, Settings
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import Layer, LayerSignal, SignalStatus, TokenSnapshot, WashTradingReport, get_chain_profile
from scanners.base import BaseScanner, ScanContext, clamp, linear, log_scale, safe_div
from scanners.shared.http_client import request_json, to_float, to_int

log = get_logger(__name__)


MIN_LIQUIDITY_USD = 20_000.0
MAX_LIQUIDITY_USD = 5_000_000.0


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


def _epoch_seconds(raw: Any) -> float | None:
    """Parse an epoch number or an ISO-8601 timestamp into epoch seconds.

    ``to_float`` returns ``None`` for ``2026-10-25T12:00:00Z``; treating that as
    ``0.0`` made every trade look simultaneous and inflated the self-trading score.
    GeckoTerminal sends ISO-8601, DexScreener sends epoch milliseconds.
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


def _pair_created_at(raw: Any) -> datetime | None:
    """Creation time from epoch milliseconds (DexScreener) or ISO (GeckoTerminal)."""
    seconds = _epoch_seconds(raw)
    if seconds is None or seconds <= 0:
        return None
    if seconds > 1e11:  # milliseconds, not seconds
        seconds /= 1000.0
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


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
    """Deepest base-token pair *on the requested chain*.

    DexScreener returns pairs where the scanned token is *either* side, so a
    quote-side pool would silently substitute another asset's price. It also
    returns pairs from every chain the token is bridged to: a same-ticker pool on
    another chain has its own price, liquidity and address, and handing its address
    to GeckoTerminal with this token's network is invalid. ``None`` means "no market
    for this token on this chain", which the caller reports as not listed.
    """
    if not pairs:
        return None
    base_pairs = [
        pair for pair in pairs if _addresses_match((pair.get("baseToken") or {}).get("address"), token_address)
    ]
    if not base_pairs:
        return None
    chain_id = (chain or "").strip().lower()
    if chain_id:
        base_pairs = [pair for pair in base_pairs if str(pair.get("chainId", "")).strip().lower() == chain_id]
        if not base_pairs:
            return None
    return max(
        base_pairs,
        key=lambda pair: to_float((pair.get("liquidity") or {}).get("usd")) or 0.0,
    )


async def fetch_dexscreener_market(
    session: aiohttp.ClientSession,
    token: TokenSnapshot,
    *,
    endpoints: EndpointSettings | None = None,
) -> DexMarket | None:
    endpoints = endpoints or EndpointSettings()
    payload = await request_json(
        session,
        endpoints.url("dexscreener.token", address=token.token_address),
    )
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
    endpoints: EndpointSettings | None = None,
    headers: dict[str, str] | None = None,
) -> DexMarket | None:
    network = get_chain_profile(token.chain).geckoterminal
    payload = await request_json(
        session,
        (endpoints or EndpointSettings()).url(
            "geckoterminal.token_pools", network=network, address=token.token_address
        ),
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
        fetch_dexscreener_market(ctx.session, token, endpoints=ctx.settings.endpoints),
        fetch_gecko_market(ctx.session, token, endpoints=ctx.settings.endpoints),
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


def volume_spike_ratio(market: DexMarket) -> float | None:
    """Last hour's volume versus the 24h average hour.

    A spike above ~3x is either a genuine catalyst or manufactured volume; the
    wash-trading filter decides which. Below 0.3x, the market is going quiet.
    """
    volume_1h = to_float(market.volume_1h_usd)
    volume_24h = to_float(market.volume_24h_usd)
    if volume_1h is None or not volume_24h:
        return None
    hourly_average = volume_24h / 24.0
    return round(volume_1h / hourly_average, 3) if hourly_average > 0 else None


def liquidity_conflict(market: DexMarket) -> bool:
    """Two providers disagreeing by >3x means neither liquidity figure is trusted."""
    primary = to_float(market.liquidity_usd)
    secondary = to_float(market.gecko_liquidity_usd)
    if not primary or not secondary:
        return False
    high, low = max(primary, secondary), min(primary, secondary)
    return low > 0 and high / low > 3.0


class CryptoDexScanner(BaseScanner):
    """Layer 4: pool liquidity, turnover, volume spikes and order-flow skew."""

    layer = Layer.DEX
    weight = 1.2
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        market = await load_market(token, ctx)
        if market is None or not market.price_usd:
            return self.unavailable(token, "no DEX pair found")

        liquidity = to_float(market.liquidity_usd) or 0.0
        turnover = to_float(market.turnover)
        spike = volume_spike_ratio(market)
        buy_ratio = market.buy_ratio
        conflict = liquidity_conflict(market)

        flags: list[str] = []
        if liquidity < MIN_LIQUIDITY_USD:
            flags.append("liquidity_below_20k")
        if turnover is not None and turnover > 10.0:
            flags.append("turnover_above_10x_liquidity")
        if spike is not None and spike >= 5.0:
            flags.append("volume_spike_above_5x")
        if spike is not None and spike < 0.3:
            flags.append("volume_collapse")
        if buy_ratio is not None and buy_ratio > 0.75:
            flags.append("buy_ratio_extreme_high")
        if buy_ratio is not None and buy_ratio < 0.25:
            flags.append("buy_ratio_extreme_low")
        if conflict:
            flags.append("provider_liquidity_conflict")
        age_hours = market.age_hours
        if age_hours is not None and age_hours < 48.0:
            flags.append("pair_younger_than_48h")

        depth = log_scale(liquidity, MIN_LIQUIDITY_USD, MAX_LIQUIDITY_USD)
        if turnover is None:
            turnover_score = 50.0
        else:
            # Healthy turnover is roughly 0.2x-3x of pool liquidity.
            deviation = abs(math.log10(max(turnover, 1e-4)) - math.log10(0.8))
            turnover_score = clamp(100.0 - deviation * 45.0)
        spike_score = log_scale(spike, 0.3, 5.0) if spike is not None else 50.0
        flow_score = linear(buy_ratio, 0.30, 0.70) if buy_ratio is not None else 50.0
        momentum = linear(market.price_change_24h_pct, -40.0, 40.0)

        components: list[tuple[float, float]] = [
            (0.35, depth),
            (0.20, turnover_score),
            (0.20, spike_score),
            (0.15, flow_score),
            (0.10, momentum),
        ]
        total_weight = sum(weight for weight, _ in components)
        score = sum(weight * value for weight, value in components) / total_weight
        if "liquidity_below_20k" in flags:
            score = min(score, 35.0)
        if conflict:
            score *= 0.9

        confidence = 0.85 if len(market.sources) >= 2 else 0.6
        if liquidity < MIN_LIQUIDITY_USD:
            confidence *= 0.7
        if market.volume_24h_usd is None:
            confidence *= 0.6
        status = SignalStatus.DEGRADED if (conflict or market.volume_24h_usd is None) else SignalStatus.OK

        volume_text = f"${market.volume_24h_usd:,.0f}" if market.volume_24h_usd is not None else "n/a"
        spike_text = f"{spike:.2f}x" if spike is not None else "n/a"
        buy_text = f"{buy_ratio:.0%}" if buy_ratio is not None else "n/a"
        summary = (
            f"{market.dex_id or 'dex'} pair {market.pair_address[:10]}..: liq ${liquidity:,.0f}, "
            f"24h vol {volume_text}, volume spike {spike_text}, buy ratio {buy_text}"
        )
        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=summary,
            status=status,
            metrics={
                **market.to_metrics(),
                "volume_spike_ratio": spike,
                "liquidity_conflict": conflict,
                "flags": flags,
            },
            flags=flags,
        )


# -- trade-tape integrity ----------------------------------------------------


WASH_SUSPICION_THRESHOLD = 85.0
MIN_TRADES_FOR_VERDICT = 30

# Wallets trading within this window, in alternating directions, look like churn.
SELF_TRADE_WINDOW_SECONDS = 120


def parse_trades(payload: Any) -> list[dict[str, Any]]:
    """Normalise GeckoTerminal's trade tape into wallet/side/notional rows.

    One parser for both consumers: the wash-trading filter (layer 4) and the
    order-flow imbalance read (layer 7).
    """
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


async def fetch_pool_trades(
    token: TokenSnapshot,
    ctx: ScanContext,
    pool: str,
    *,
    min_volume_usd: float = 0.0,
) -> list[dict[str, Any]]:
    """One GeckoTerminal trade-tape read, normalised through :func:`parse_trades`.

    Both consumers go through here: the wash filter wants every print
    (``min_volume_usd=0``), layer 7 wants the meaningful ones. That keeps the
    endpoint, its parameters and the payload shape in one place.
    """
    network = get_chain_profile(token.chain).geckoterminal
    payload = await request_json(
        ctx.session,
        ctx.settings.endpoints.url("geckoterminal.pool_trades", network=network, pool=pool),
        params={"trade_volume_in_usd_greater_than": min_volume_usd},
    )
    return parse_trades(payload)


async def fetch_wash_report(token: TokenSnapshot, ctx: ScanContext, pool: str) -> WashTradingReport:
    return analyse_trades(await fetch_pool_trades(token, ctx, pool))


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


def build_scanner(settings: Settings) -> CryptoDexScanner:
    return CryptoDexScanner(settings)


__all__ = [
    "DexMarket",
    "MAX_LIQUIDITY_USD",
    "MIN_LIQUIDITY_USD",
    "MIN_TRADES_FOR_VERDICT",
    "SELF_TRADE_WINDOW_SECONDS",
    "WASH_SUSPICION_THRESHOLD",
    "WashTradingFilter",
    "CryptoDexScanner",
    "analyse_trades",
    "build_scanner",
    "buy_sell_imbalance",
    "fetch_dexscreener_market",
    "fetch_gecko_market",
    "fetch_pool_trades",
    "fetch_wash_report",
    "is_round_amount",
    "liquidity_conflict",
    "load_market",
    "market_from_dexscreener",
    "merge_markets",
    "parse_trades",
    "self_trade_share",
    "volume_spike_ratio",
    "wallet_concentration",
]
