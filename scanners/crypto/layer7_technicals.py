"""Layer 7 - technical engine on M15 and H1 bars.

Indicators are computed with the repo's own audited implementations
(:mod:`scanners.technicals`) rather than ``pandas-ta``: the dependency set is
pinned deliberately (see ``requirements.txt``), and these functions are already
unit-tested. Nothing here needs a vendor key - GeckoTerminal provides both the
candles and the trade tape.

Order Flow Imbalance (OFI) is measured from the DEX tape: signed buy minus sell
notional over the recent trades, normalised to ``-1..1``. It is the taker-flow
proxy that matters for a token with no central limit order book.
"""

from __future__ import annotations

import asyncio
from typing import Any

from config.settings import Settings
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import Layer, LayerSignal, SignalStatus, TokenSnapshot, clamp, get_chain_profile
from scanners.base import BaseScanner, ScanContext, linear, log_scale, safe_div
from scanners.dex import load_market
from scanners.series import parse_ohlcv

from scanners.shared.http_client import first_row, request_json, to_float
from scanners.shared.technicals import atr, ema, obv, percent_change, rsi

log = get_logger(__name__)


GECKO_OHLCV_URL = "https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/ohlcv/{frame}"
GECKO_TRADES_URL = "https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/trades"


MIN_CANDLES = 35


def parse_trade_flow(payload: Any, *, window: int = 300) -> dict[str, Any]:
    """Signed taker flow from GeckoTerminal's trade tape."""
    rows = first_row(payload)[-window:]
    buy_usd = sell_usd = 0.0
    counted = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else row
        volume = to_float(attrs.get("volume_in_usd")) or to_float(attrs.get("volume_usd"))
        if volume is None or volume <= 0:
            continue
        kind = str(attrs.get("kind") or attrs.get("side") or "").strip().lower()
        if kind not in {"buy", "sell"}:
            continue
        if kind == "buy":
            buy_usd += volume
        else:
            sell_usd += volume
        counted += 1
    total = buy_usd + sell_usd
    if counted == 0 or total <= 0:
        return {}
    return {
        "trades_sampled": counted,
        "buy_volume_usd": round(buy_usd, 2),
        "sell_volume_usd": round(sell_usd, 2),
        "order_flow_imbalance": round((buy_usd - sell_usd) / total, 4),
    }


def indicator_snapshot(candles: Any, *, label: str) -> dict[str, Any]:
    """RSI/EMA/ATR/OBV for one timeframe; ``{}`` when there is not enough data."""
    closes = (candles or {}).get("close") or []
    highs = (candles or {}).get("high") or []
    lows = (candles or {}).get("low") or []
    volumes = (candles or {}).get("volume") or []
    if len(closes) < MIN_CANDLES or len(highs) < MIN_CANDLES or len(lows) < MIN_CANDLES:
        return {}
    price = closes[-1]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50) if len(closes) >= 50 else None
    rsi14 = rsi(closes, 14)
    atr14 = atr(highs, lows, closes, 14)
    atr_pct = safe_div(atr14, price)
    obv_series = obv(closes, volumes) if len(volumes) == len(closes) else []
    obv_slope = percent_change(obv_series[-1], obv_series[-13]) if len(obv_series) >= 13 else None
    return {
        f"{label}_price": price,
        f"{label}_ema20": ema20,
        f"{label}_ema50": ema50,
        f"{label}_rsi14": round(rsi14, 2) if rsi14 is not None else None,
        f"{label}_atr_pct": round(atr_pct * 100.0, 3) if atr_pct is not None else None,
        f"{label}_obv_slope_pct": round(obv_slope, 2) if obv_slope is not None else None,
        f"{label}_change_12_pct": percent_change(price, closes[-13]) if len(closes) >= 13 else None,
        f"{label}_candles": len(closes),
    }


def trend_score(price: float | None, ema20: float | None, ema50: float | None) -> float:
    """Price above rising EMAs is a trend; below them is distribution."""
    score = 50.0
    if price is not None and ema20:
        score += 20.0 if price > ema20 else -20.0
    if ema20 and ema50:
        score += 20.0 if ema20 > ema50 else -20.0
    return clamp(score)


def rsi_score(value: float | None) -> float:
    """Reward constructive momentum; penalise exhaustion at both extremes."""
    if value is None:
        return 50.0
    if value >= 78.0:
        return 25.0
    if value <= 25.0:
        return 35.0
    return linear(value, 30.0, 70.0)


def order_flow_score(imbalance: float | None) -> float:
    if imbalance is None:
        return 50.0
    return linear(imbalance, -0.5, 0.5)


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
            self._candles(network, pool, frame="minute", aggregate=15, ctx=ctx),
            self._candles(network, pool, frame="hour", aggregate=1, ctx=ctx),
            self._trades(network, pool, ctx),
            return_exceptions=True,
        )
        if isinstance(m15, Exception) and isinstance(h1, Exception) and isinstance(trades, Exception):
            raise RuntimeError("every technicals provider failed")

        m15_metrics = indicator_snapshot({} if isinstance(m15, Exception) else parse_ohlcv(m15), label="m15")
        h1_metrics = indicator_snapshot({} if isinstance(h1, Exception) else parse_ohlcv(h1), label="h1")
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

        flags: list[str] = []
        if h1_rsi is not None and h1_rsi >= 78.0:
            flags.append("rsi_overbought")
        if h1_rsi is not None and h1_rsi <= 25.0:
            flags.append("rsi_oversold")
        price = to_float(metrics.get("h1_price"))
        ema20 = to_float(metrics.get("h1_ema20"))
        ema50 = to_float(metrics.get("h1_ema50"))
        if price and ema20 and ema50 and price < ema20 < ema50:
            flags.append("downtrend_structure")
        if ofi is not None and ofi <= -0.35:
            flags.append("ofi_sell_pressure")
        if ofi is not None and ofi >= 0.35:
            flags.append("ofi_buy_pressure")
        if "downtrend_structure" in flags:
            score = min(score, 42.0)
        if "rsi_overbought" in flags:
            score = min(score, 60.0)

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
        if "h1_atr_pct" in metrics:
            parts.append(f"ATR {metrics['h1_atr_pct']:.1f}%")
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

    # -- providers -----------------------------------------------------------
    @staticmethod
    async def _candles(network: str, pool: str, *, frame: str, aggregate: int, ctx: ScanContext) -> Any:
        payload = await request_json(
            ctx.session,
            GECKO_OHLCV_URL.format(network=network, pool=pool, frame=frame),
            params={"aggregate": aggregate, "limit": 200},
        )
        if not isinstance(payload, dict):
            raise ValueError("unexpected GeckoTerminal OHLCV payload")
        return payload

    @staticmethod
    async def _trades(network: str, pool: str, ctx: ScanContext) -> Any:
        return await request_json(
            ctx.session,
            GECKO_TRADES_URL.format(network=network, pool=pool),
            params={"trade_volume_in_usd_greater_than": 100},
        )


def build_scanner(settings: Settings) -> CryptoTechnicalsScanner:
    return CryptoTechnicalsScanner(settings)


__all__ = [
    "GECKO_OHLCV_URL",
    "GECKO_TRADES_URL",
    "MIN_CANDLES",
    "CryptoTechnicalsScanner",
    "build_scanner",
    "indicator_snapshot",
    "order_flow_score",
    "parse_trade_flow",
    "rsi_score",
    "trend_score",
]
