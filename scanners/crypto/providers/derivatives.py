"""Derivatives layer: perpetual funding, open interest and taker flow.

Perp positioning is the cleanest read on *who is already in the trade*. Crowded
longs with rising OI are how good spot setups turn into liquidation cascades, so
this layer is deliberately contrarian at the extremes.

Binance is the primary source; Bybit is a free fallback for funding/OI when Binance
is geo-blocked (HTTP 451) or rate-limited.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.http import request_json, to_float
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import Layer, SignalStatus, LayerSignal, TokenSnapshot
from scanners.base import BaseScanner, ScanContext, linear, safe_div

log = get_logger(__name__)

BINANCE = "https://fapi.binance.com"
BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers"
QUOTE_SUFFIX = "USDT"


def perp_symbol(symbol: str) -> str:
    """``pepe`` -> ``PEPEUSDT``. Empty symbol means "we cannot look this up"."""
    return f"{symbol.strip().upper()}{QUOTE_SUFFIX}" if symbol.strip() else ""


def perp_symbol_candidates(symbol: str) -> list[str]:
    """Contract tickers to try, cheapest first: ``PEPE``, ``1000PEPE``, ``1000000PEPE``."""
    base = symbol.strip().upper()
    if not base:
        return []
    return [f"{prefix}{base}{QUOTE_SUFFIX}" for prefix in ("", "1000", "1000000")]


def funding_score(rate: float | None) -> float:
    """Reward mildly positive funding, punish crowded longs and deep negative carry."""
    if rate is None:
        return 50.0
    healthy = 0.0002  # ~0.02% per 8h
    deviation = abs(rate - healthy) / 0.0008
    return max(0.0, 100.0 - deviation * 100.0)


def position_ratio_score(ratio: float | None) -> float:
    """Contrarian: a lopsided long/short account ratio is fuel, not confirmation."""
    if ratio is None:
        return 50.0
    if ratio <= 1.0:
        return 60.0
    return max(0.0, 100.0 - (ratio - 1.0) * 35.0)


class DerivativesScanner(BaseScanner):
    layer = Layer.DERIVATIVES
    weight = 0.9
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        base = self._symbol_for(token, ctx)
        if not base:
            return self.skipped(token, "token symbol unknown; no perp lookup possible")

        # Meme perps are often quoted in thousands/millions (1000PEPEUSDT), so the
        # plain symbol is not enough - try the scaled contracts before giving up.
        metrics: dict[str, Any] | None = None
        symbol = ""
        for candidate in perp_symbol_candidates(base):
            metrics = await self._fetch(candidate, ctx)
            if metrics:
                symbol = candidate
                break
        if metrics is None:
            return self.skipped(token, f"no perpetual market for {perp_symbol(base)}")
        if not metrics:
            return self.unavailable(token, f"derivatives providers failed for {perp_symbol(base)}")

        funding = to_float(metrics.get("funding_rate"))
        oi_change = to_float(metrics.get("oi_change_pct"))
        taker = to_float(metrics.get("taker_buy_sell_ratio"))
        ratio = to_float(metrics.get("long_short_ratio"))

        flags: list[str] = []
        if funding is not None and funding > 0.0008:
            flags.append("funding_overheated")
        if funding is not None and funding < -0.0005:
            flags.append("funding_deep_negative")
        if oi_change is not None and oi_change > 40:
            flags.append("oi_spike_above_40pct")
        if ratio is not None and ratio > 3.0:
            flags.append("crowded_long_accounts")

        score = (
            0.30 * funding_score(funding)
            + 0.30 * linear(oi_change, -15.0, 25.0)
            + 0.25 * linear(taker, 0.85, 1.15)
            + 0.15 * position_ratio_score(ratio)
        )
        if "funding_overheated" in flags:
            score = min(score, 45.0)

        source = metrics.get("source", "binance")
        confidence = 0.8 if source == "binance" else 0.55
        if funding is None or oi_change is None:
            confidence *= 0.6
        status = SignalStatus.OK if source == "binance" and oi_change is not None else SignalStatus.DEGRADED

        summary = (
            f"{symbol} perp: funding {(funding * 100 if funding is not None else 0):.4f}%/8h, "
            f"OI {oi_change if oi_change is not None else 'n/a'}%, "
            f"taker {taker if taker is not None else 'n/a'} [{source}]"
        )
        metrics.update({"symbol": symbol, "flags": flags})
        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=summary,
            status=status,
            metrics=metrics,
            flags=flags,
        )

    @staticmethod
    def _symbol_for(token: TokenSnapshot, ctx: ScanContext) -> str:
        """Prefer the DEX pair's base symbol (canonical), fall back to the hint."""
        market = ctx.get_shared("market")
        base = getattr(market, "base_symbol", None)
        return (base or token.symbol or "").strip()

    async def _fetch(self, symbol: str, ctx: ScanContext) -> dict[str, Any] | None:
        """``None`` means "no perp market"; ``{}`` means "everything failed"."""
        try:
            binance = await self._fetch_binance(symbol, ctx)
            if binance is not None:
                return binance
        except Exception as exc:  # noqa: BLE001 - fall through to Bybit
            log.debug("binance_derivatives_failed", extra={"symbol": symbol, "error": str(exc)[:200]})

        try:
            return await self._fetch_bybit(symbol, ctx)
        except Exception as exc:  # noqa: BLE001
            log.debug("bybit_derivatives_failed", extra={"symbol": symbol, "error": str(exc)[:200]})
            return {}

    async def _fetch_binance(self, symbol: str, ctx: ScanContext) -> dict[str, Any] | None:
        params = {"symbol": symbol}
        premium, oi_now, oi_hist, ticker, taker, ratio = await asyncio.gather(
            request_json(ctx.session, f"{BINANCE}/fapi/v1/premiumIndex", params=params),
            request_json(ctx.session, f"{BINANCE}/fapi/v1/openInterest", params=params),
            request_json(
                ctx.session,
                f"{BINANCE}/futures/data/openInterestHist",
                params={"symbol": symbol, "period": "1h", "limit": 25},
            ),
            request_json(ctx.session, f"{BINANCE}/fapi/v1/ticker/24hr", params=params),
            request_json(
                ctx.session,
                f"{BINANCE}/futures/data/takerlongshortRatio",
                params={"symbol": symbol, "period": "1h", "limit": 24},
            ),
            request_json(
                ctx.session,
                f"{BINANCE}/futures/data/globalLongShortAccountRatio",
                params={"symbol": symbol, "period": "1h", "limit": 1},
            ),
            return_exceptions=True,
        )
        if isinstance(premium, Exception) or not isinstance(premium, dict):
            return None
        if to_float(premium.get("markPrice")) is None:
            return None  # symbol not listed on perps

        oi_change = _oi_change_pct(oi_hist if isinstance(oi_hist, list) else None)
        taker_ratio = _taker_ratio(taker if isinstance(taker, list) else None)
        ls_ratio = None
        if isinstance(ratio, list) and ratio:
            ls_ratio = to_float((ratio[-1] or {}).get("longShortRatio"))
        return {
            "source": "binance",
            "funding_rate": to_float(premium.get("lastFundingRate")),
            "mark_price": to_float(premium.get("markPrice")),
            "open_interest": to_float((oi_now or {}).get("openInterest")) if isinstance(oi_now, dict) else None,
            "oi_change_pct": oi_change,
            "taker_buy_sell_ratio": taker_ratio,
            "long_short_ratio": ls_ratio,
            "quote_volume_24h": to_float((ticker or {}).get("quoteVolume")) if isinstance(ticker, dict) else None,
            "price_change_24h_pct": to_float((ticker or {}).get("priceChangePercent")) if isinstance(ticker, dict) else None,
        }

    async def _fetch_bybit(self, symbol: str, ctx: ScanContext) -> dict[str, Any] | None:
        payload = await request_json(
            ctx.session,
            BYBIT_TICKERS,
            params={"category": "linear", "symbol": symbol},
        )
        rows = ((payload or {}).get("result") or {}).get("list") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        if to_float(row.get("markPrice")) is None:
            return None
        pct = to_float(row.get("price24hPcnt"))
        return {
            "source": "bybit",
            "funding_rate": to_float(row.get("fundingRate")),
            "mark_price": to_float(row.get("markPrice")),
            "open_interest": to_float(row.get("openInterest")),
            "oi_change_pct": None,  # Bybit's ticker carries no OI history
            "taker_buy_sell_ratio": None,
            "long_short_ratio": None,
            "quote_volume_24h": to_float(row.get("turnover24h")),
            "price_change_24h_pct": round(pct * 100.0, 3) if pct is not None else None,
        }


def _oi_change_pct(history: list[dict[str, Any]] | None) -> float | None:
    """Percent change in open interest between the first and last history rows."""
    if not history or len(history) < 2:
        return None
    first = to_float((history[0] or {}).get("sumOpenInterestValue"))
    last = to_float((history[-1] or {}).get("sumOpenInterestValue"))
    if first is None or last is None:
        return None
    change = safe_div(last - first, first)
    return round(change * 100.0, 2) if change is not None else None


def _taker_ratio(rows: list[dict[str, Any]] | None) -> float | None:
    if not rows:
        return None
    buys = sum(to_float((row or {}).get("buyVol")) or 0.0 for row in rows)
    sells = sum(to_float((row or {}).get("sellVol")) or 0.0 for row in rows)
    ratio = safe_div(buys, sells)
    return round(ratio, 3) if ratio is not None else None


__all__ = [
    "BYBIT_TICKERS",
    "DerivativesScanner",
    "funding_score",
    "perp_symbol",
    "perp_symbol_candidates",
    "position_ratio_score",
]
