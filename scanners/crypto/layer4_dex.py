"""Layer 4 - DEX microstructure: pool depth, turnover and volume spikes.

DexScreener and GeckoTerminal are free and keyless. Both are queried once by
:func:`scanners.crypto.providers.dex.load_market`; this layer adds a volume-spike read on top of
that merged view instead of issuing duplicate requests.
"""

from __future__ import annotations

import math

from config.settings import Settings
from scanners.shared.http_client import to_float
from core.instruments import AssetClass
from core.models import Layer, LayerSignal, SignalStatus, TokenSnapshot
from scanners.base import BaseScanner, ScanContext, clamp, linear, log_scale
from scanners.crypto.providers.dex import DexMarket, load_market


MIN_LIQUIDITY_USD = 20_000.0
MAX_LIQUIDITY_USD = 5_000_000.0


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


def build_scanner(settings: Settings) -> CryptoDexScanner:
    return CryptoDexScanner(settings)


__all__ = [
    "CryptoDexScanner",
    "MIN_LIQUIDITY_USD",
    "build_scanner",
    "liquidity_conflict",
    "volume_spike_ratio",
]
