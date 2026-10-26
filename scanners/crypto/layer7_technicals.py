"""Layer 7 - technical engine on M15 and H1 bars.

Indicators, score curves and flags all come from :mod:`scanners.shared.technicals`
(``primitives`` / ``snapshot`` / ``scoring`` / ``flags``), the same package
``scanners/shared/listed/technicals.py`` uses - one audited set of implementations
rather than ``pandas-ta`` (the dependency set is pinned deliberately, see
``requirements.txt``). This layer only adds what is crypto-specific: the two
timeframes, the DEX pool resolution and the order-flow read.

Order Flow Imbalance (OFI) is measured from the DEX tape: signed buy minus sell
notional over the recent trades, normalised to ``-1..1``. It is the taker-flow proxy
that matters for a token with no central limit order book. The tape is parsed once by
:func:`scanners.crypto.layer4_dex.parse_trades`, so the wash filter and this layer
never disagree about what a trade is.

Nothing here needs a vendor key - GeckoTerminal provides both the candles and the
trade tape, at the host configured in ``GECKOTERMINAL_BASE_URL``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

from config.settings import Settings
from core.instruments import AssetClass
from core.models import Layer, LayerSignal, SignalStatus, TokenSnapshot, get_chain_profile
from scanners.base import BaseScanner, ScanContext, log_scale
from scanners.crypto.layer4_dex import fetch_pool_trades, load_market
from scanners.shared.http_client import to_float
from scanners.shared.series import fetch_ohlcv
from scanners.shared.technicals import (
    MIN_CANDLES,
    apply_flag_caps,
    compute_indicators,
    order_flow_imbalance,
    order_flow_score,
    rsi_flags,
    rsi_score,
    structure_flags,
    trend_score,
)


def parse_trade_flow(trades: Sequence[Mapping[str, Any]], *, window: int = 300) -> dict[str, Any]:
    """Signed taker flow from the normalised GeckoTerminal trade tape.

    ``trades`` is the output of :func:`scanners.crypto.layer4_dex.parse_trades`; the
    volume aggregation and the imbalance maths are shared with the wash filter.
    """
    rows = list(trades)[-window:]
    buy_usd = sum(float(row.get("volume_usd") or 0.0) for row in rows if row.get("kind") == "buy")
    sell_usd = sum(float(row.get("volume_usd") or 0.0) for row in rows if row.get("kind") == "sell")
    counted = sum(1 for row in rows if row.get("kind") in {"buy", "sell"})
    total = buy_usd + sell_usd
    if counted == 0 or total <= 0:
        return {}
    imbalance = order_flow_imbalance(buy_usd, sell_usd)
    return {
        "trades_sampled": counted,
        "buy_volume_usd": round(buy_usd, 2),
        "sell_volume_usd": round(sell_usd, 2),
        "order_flow_imbalance": round(imbalance, 4) if imbalance is not None else 0.0,
    }


def indicator_snapshot(candles: Any, *, label: str) -> dict[str, Any]:
    """RSI/EMA/ATR/OBV for one timeframe; ``{}`` when there is not enough data."""
    indicators = compute_indicators(candles, minimum=MIN_CANDLES)
    return indicators.metrics(label) if indicators is not None else {}


class CryptoTechnicalsScanner(BaseScanner):
    """Layer 7: multi-timeframe trend, RSI and order-flow imbalance."""

    layer = Layer.TECHNICALS
    weight = 1.0
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        market = await load_market(token, ctx)
        pool = (market.pair_address if market else None) or token.pair_address or ""
        if not pool:
            return self.skipped(token, "no pool address available for OHLCV")

        network = get_chain_profile(token.chain).geckoterminal
        m15, h1, trades = await asyncio.gather(
            fetch_ohlcv(ctx, network=network, pool=pool, frame="minute", aggregate=15),
            fetch_ohlcv(ctx, network=network, pool=pool, frame="hour"),
            fetch_pool_trades(token, ctx, pool, min_volume_usd=100.0),
            return_exceptions=True,
        )
        if isinstance(m15, Exception) and isinstance(h1, Exception) and isinstance(trades, Exception):
            raise RuntimeError("every technicals provider failed")

        m15_metrics = indicator_snapshot({} if isinstance(m15, Exception) else m15, label="m15")
        h1_metrics = indicator_snapshot({} if isinstance(h1, Exception) else h1, label="h1")
        flow_metrics = {} if isinstance(trades, Exception) else parse_trade_flow(trades)
        metrics = {**m15_metrics, **h1_metrics, **flow_metrics}
        if not m15_metrics and not h1_metrics:
            return self.unavailable(token, "GeckoTerminal returned no usable OHLCV")

        h1_rsi = to_float(metrics.get("h1_rsi14"))
        m15_rsi = to_float(metrics.get("m15_rsi14"))
        ofi = to_float(metrics.get("order_flow_imbalance"))
        h1_obv = to_float(metrics.get("h1_obv_slope_pct"))

        components: list[tuple[float, float]] = []
        trend_value = trend_score(
            to_float(metrics.get("h1_price")), to_float(metrics.get("h1_ema20")), to_float(metrics.get("h1_ema50"))
        )
        components.append((0.35, trend_value))
        if h1_rsi is not None:
            components.append((0.20, rsi_score(h1_rsi)))
        if ofi is not None:
            components.append((0.25, order_flow_score(ofi)))
        if m15_rsi is not None:
            components.append((0.10, rsi_score(m15_rsi)))
        if h1_obv is not None:
            components.append((0.10, log_scale(abs(h1_obv) + 1.0, 1.0, 60.0) if h1_obv > 0 else 40.0))

        total_weight = sum(weight for weight, _ in components) or 1.0
        score = sum(weight * value for weight, value in components) / total_weight

        price = to_float(metrics.get("h1_price"))
        ema20 = to_float(metrics.get("h1_ema20"))
        ema50 = to_float(metrics.get("h1_ema50"))
        flags: list[str] = rsi_flags(h1_rsi) + structure_flags(price, ema20, ema50)
        if ofi is not None and ofi <= -0.35:
            flags.append("ofi_sell_pressure")
        if ofi is not None and ofi >= 0.35:
            flags.append("ofi_buy_pressure")
        score = apply_flag_caps(score, flags)

        confidence = 0.8 if m15_metrics and h1_metrics else 0.6
        if ofi is None:
            confidence *= 0.85
        status = SignalStatus.OK if m15_metrics and h1_metrics and flow_metrics else SignalStatus.DEGRADED

        parts = []
        if "h1_candles" in metrics:
            parts.append(f"{metrics['h1_candles']}x1h candles")
        if h1_rsi is not None:
            parts.append(f"H1 RSI {h1_rsi:.1f}")
        if m15_rsi is not None:
            parts.append(f"M15 RSI {m15_rsi:.1f}")
        if ofi is not None:
            parts.append(f"OFI {ofi:+.2f}")
        # ``compute_indicators`` keeps unset indicators as ``None``; the key alone
        # does not guarantee a number.
        atr_pct = to_float(metrics.get("h1_atr_pct"))
        if atr_pct is not None:
            parts.append(f"ATR {atr_pct:.1f}%")
        summary = f"{token.symbol or 'token'}: " + ", ".join(parts)
        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=summary,
            status=status,
            metrics={**metrics, "pool": pool, "flags": flags},
            flags=flags,
        )


def build_scanner(settings: Settings) -> CryptoTechnicalsScanner:
    return CryptoTechnicalsScanner(settings)


__all__ = [
    "MIN_CANDLES",
    "CryptoTechnicalsScanner",
    "build_scanner",
    "indicator_snapshot",
    "order_flow_score",
    "parse_trade_flow",
    "rsi_score",
    "trend_score",
]
