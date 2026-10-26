"""Fundamentals layer: issuer financials from SEC EDGAR (US-listed equities).

Price layers tell you what the market believes; company facts tell you what the
business has actually earned. For an equity that is the difference between a
momentum trade and an investment, and it is the one thing crypto's on-chain stack
has no analogue for.

SEC EDGAR is free, keyless and authoritative, but its fair-access policy requires
a declared contact in the ``User-Agent``. So the layer is *opt-in*: with
``SEC_USER_AGENT`` unset it returns ``SKIPPED`` (zero weight, no HTTP call) rather
than hammering the API anonymously. Non-US listings have no CIK and skip too.

Only annual (10-K) figures are used. Mixing quarterly and annual values silently
produces meaningless growth rates, and a fundamentals layer that lies is worse
than one that is absent.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping

from core.http import request_json, to_float
from core.instruments import AssetClass
from core.models import Layer, SignalStatus, LayerSignal, TokenSnapshot, clamp
from scanners.base import BaseScanner, ScanContext, linear

#: SEC asks for <10 requests/second; 150 ms spacing keeps us far below that.
MIN_SEC_INTERVAL_SECONDS = 0.15
TICKER_CACHE_SECONDS = 86_400.0

REVENUE_CONCEPTS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)


class _SecRateLimiter:
    """Serialise EDGAR calls - the fair-access budget is per IP, not per coroutine."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_call_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            wait = MIN_SEC_INTERVAL_SECONDS - (time.monotonic() - self._last_call_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call_at = time.monotonic()


class _TickerIndex:
    """Process-wide ticker -> CIK cache with a lock around the one download."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.loaded_at: float | None = None
        self.entries: dict[str, tuple[int, str]] = {}

    def is_fresh(self) -> bool:
        return self.loaded_at is not None and (time.monotonic() - self.loaded_at) < TICKER_CACHE_SECONDS

    def lookup(self, symbol: str) -> tuple[int, str] | None:
        entry = self.entries.get(symbol)
        return (entry[0], entry[1]) if entry else None

    def store(self, index: dict[str, tuple[int, str]]) -> None:
        self.entries = index
        self.loaded_at = time.monotonic()


_SEC_LIMITER = _SecRateLimiter()
_TICKER_INDEX = _TickerIndex()


@dataclass(slots=True)
class Fundamentals:
    """Annual company facts, already reduced to the ratios we score."""

    symbol: str = ""
    revenue: float | None = None
    revenue_prior: float | None = None
    net_income: float | None = None
    equity: float | None = None
    assets: float | None = None
    liabilities: float | None = None
    operating_cash_flow: float | None = None
    capex: float | None = None
    eps: float | None = None
    eps_prior: float | None = None
    fiscal_year_end: str = ""
    concepts: list[str] = field(default_factory=list)

    @property
    def revenue_growth_pct(self) -> float | None:
        if self.revenue is None or self.revenue_prior in (None, 0):
            return None
        return (self.revenue - self.revenue_prior) / abs(self.revenue_prior) * 100.0

    @property
    def net_margin_pct(self) -> float | None:
        if self.net_income is None or not self.revenue:
            return None
        return self.net_income / self.revenue * 100.0

    @property
    def roe_pct(self) -> float | None:
        if self.net_income is None or not self.equity or self.equity <= 0:
            return None
        return self.net_income / self.equity * 100.0

    @property
    def debt_ratio(self) -> float | None:
        if self.liabilities is None or not self.assets:
            return None
        return self.liabilities / self.assets

    @property
    def free_cash_flow(self) -> float | None:
        if self.operating_cash_flow is None:
            return None
        return self.operating_cash_flow - (self.capex or 0.0)

    @property
    def fcf_margin_pct(self) -> float | None:
        fcf = self.free_cash_flow
        if fcf is None or not self.revenue:
            return None
        return fcf / self.revenue * 100.0

    @property
    def eps_growth_pct(self) -> float | None:
        if self.eps is None or self.eps_prior in (None, 0):
            return None
        return (self.eps - self.eps_prior) / abs(self.eps_prior) * 100.0

    def to_metrics(self) -> dict[str, Any]:
        def _round(value: float | None, digits: int = 2) -> float | None:
            return round(value, digits) if value is not None else None

        return {
            "fiscal_year_end": self.fiscal_year_end,
            "revenue": self.revenue,
            "revenue_prior": self.revenue_prior,
            "revenue_growth_pct": _round(self.revenue_growth_pct),
            "net_income": self.net_income,
            "net_margin_pct": _round(self.net_margin_pct),
            "stockholders_equity": self.equity,
            "assets": self.assets,
            "liabilities": self.liabilities,
            "debt_ratio": _round(self.debt_ratio, 3),
            "operating_cash_flow": self.operating_cash_flow,
            "capex": self.capex,
            "fcf_margin_pct": _round(self.fcf_margin_pct),
            "eps_diluted": self.eps,
            "eps_growth_pct": _round(self.eps_growth_pct),
            "concepts": list(self.concepts),
        }


def _annual_duration(start: Any, end: Any) -> bool:
    """True when ``start..end`` looks like a fiscal year rather than a quarter."""
    try:
        span = (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days
    except (TypeError, ValueError):
        return False
    return 330 <= span <= 400


def _series(payload: Mapping[str, Any], concept: str, *, unit: str = "USD") -> list[tuple[str, float]]:
    """Annual 10-K values for ``concept``, oldest first; restatements win."""
    facts = payload.get("facts") if isinstance(payload.get("facts"), Mapping) else payload
    us_gaap = (facts or {}).get("us-gaap") if isinstance(facts, Mapping) else None
    node = (us_gaap or {}).get(concept) if isinstance(us_gaap, Mapping) else None
    units = (node or {}).get("units") if isinstance(node, Mapping) else None
    rows = (units or {}).get(unit) if isinstance(units, Mapping) else None
    if not isinstance(rows, list):
        return []
    by_end: dict[str, float] = {}
    for entry in rows:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("form") != "10-K" or entry.get("fp") != "FY":
            continue
        end = entry.get("end")
        value = to_float(entry.get("val"))
        if not end or value is None:
            continue
        start = entry.get("start")
        is_duration = start is not None
        if is_duration and not _annual_duration(start, end):
            continue
        by_end[str(end)] = value
    return sorted(by_end.items(), key=lambda item: item[0])


def _latest(series: Iterable[tuple[str, float]], offset: int = 0) -> float | None:
    rows = list(series)
    if len(rows) <= offset:
        return None
    return rows[-1 - offset][1]


def _latest_end(series: Iterable[tuple[str, float]]) -> str:
    rows = list(series)
    return rows[-1][0] if rows else ""


def _first_series(
    payload: Mapping[str, Any], concepts: Iterable[str], *, unit: str = "USD"
) -> tuple[str, list[tuple[str, float]]]:
    """First concept in ``concepts`` that has annual data."""
    for concept in concepts:
        rows = _series(payload, concept, unit=unit)
        if rows:
            return concept, rows
    return "", []


def parse_company_facts(payload: Any, *, symbol: str = "") -> Fundamentals:
    """Reduce an EDGAR ``companyfacts`` document to :class:`Fundamentals`."""
    if not isinstance(payload, Mapping):
        raise ValueError("unexpected SEC companyfacts response")

    revenue_concept, revenue_rows = _first_series(payload, REVENUE_CONCEPTS)
    income_concept, income_rows = _first_series(payload, ("NetIncomeLoss", "ProfitLoss"))
    equity_rows = _series(payload, "StockholdersEquity") or _series(
        payload, "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"
    )
    asset_rows = _series(payload, "Assets")
    liability_rows = _series(payload, "Liabilities")
    cash_rows = _series(payload, "NetCashProvidedByUsedInOperatingActivities")
    capex_rows = _series(payload, "PaymentsToAcquirePropertyPlantAndEquipment")
    eps_concept, eps_rows = _first_series(payload, ("EarningsPerShareDiluted",), unit="USD/shares")

    fundamentals = Fundamentals(
        symbol=symbol,
        revenue=_latest(revenue_rows),
        revenue_prior=_latest(revenue_rows, offset=1),
        net_income=_latest(income_rows),
        equity=_latest(equity_rows),
        assets=_latest(asset_rows),
        liabilities=_latest(liability_rows),
        operating_cash_flow=_latest(cash_rows),
        capex=_latest(capex_rows),
        eps=_latest(eps_rows),
        eps_prior=_latest(eps_rows, offset=1),
        fiscal_year_end=_latest_end(revenue_rows) or _latest_end(asset_rows),
        concepts=[
            name
            for name in (revenue_concept, income_concept, eps_concept, "StockholdersEquity", "Assets")
            if name
        ],
    )
    if all(
        value is None
        for value in (
            fundamentals.revenue,
            fundamentals.net_income,
            fundamentals.assets,
            fundamentals.eps,
        )
    ):
        raise ValueError(f"no annual us-gaap facts for {symbol or 'instrument'}")
    return fundamentals


def parse_ticker_index(payload: Any) -> dict[str, tuple[int, str]]:
    """Map ``TICKER -> (CIK, company title)`` from SEC's company_tickers.json."""
    if not isinstance(payload, Mapping):
        raise ValueError("unexpected SEC ticker index response")
    index: dict[str, tuple[int, str]] = {}
    for row in payload.values():
        if not isinstance(row, Mapping):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        cik = to_float(row.get("cik_str"))
        if not ticker or cik is None:
            continue
        index[ticker] = (int(cik), str(row.get("title") or "").strip())
    return index


def score_fundamentals(fundamentals: Fundamentals) -> tuple[float, list[str], float, dict[str, float]]:  # noqa: C901
    """Map annual facts onto a 0-100 quality score, flags and confidence."""
    components: list[tuple[float, str, float]] = []

    growth = fundamentals.revenue_growth_pct
    if growth is not None:
        components.append((0.25, "revenue_growth", linear(growth, -10.0, 25.0)))
    margin = fundamentals.net_margin_pct
    if margin is not None:
        components.append((0.20, "net_margin", linear(margin, 0.0, 25.0)))
    roe = fundamentals.roe_pct
    if roe is not None:
        components.append((0.15, "roe", linear(roe, 0.0, 30.0)))
    debt = fundamentals.debt_ratio
    if debt is not None:
        components.append((0.15, "leverage", 100.0 - linear(debt * 100.0, 30.0, 90.0)))
    fcf_margin = fundamentals.fcf_margin_pct
    if fcf_margin is not None:
        components.append((0.15, "fcf_margin", linear(fcf_margin, 0.0, 20.0)))
    eps_growth = fundamentals.eps_growth_pct
    if eps_growth is not None:
        components.append((0.10, "eps_growth", linear(eps_growth, -20.0, 30.0)))

    total_weight = sum(weight for weight, _, _ in components)
    if total_weight <= 0:
        return 50.0, ["no_usable_fundamentals"], 0.2, {}
    score = sum(weight * value for weight, _, value in components) / total_weight
    detail = {name: round(value, 2) for _, name, value in components}

    flags: list[str] = []
    if growth is not None and growth < 0:
        flags.append("revenue_declining")
    if fundamentals.net_income is not None and fundamentals.net_income < 0:
        flags.append("unprofitable")
    if fundamentals.equity is not None and fundamentals.equity < 0:
        flags.append("negative_equity")
    if debt is not None and debt > 0.8:
        flags.append("high_leverage")
    if fundamentals.operating_cash_flow is not None and fundamentals.operating_cash_flow < 0:
        flags.append("cash_burn")
    if eps_growth is not None and eps_growth < 0:
        flags.append("eps_declining")

    if len(components) >= 5:
        confidence = 0.85
    elif len(components) >= 3:
        confidence = 0.65
    else:
        confidence = 0.45
    return clamp(score), flags, confidence, detail


async def _respect_sec_rate_limit() -> None:
    """Keep consecutive EDGAR calls under the published fair-access limit."""
    await _SEC_LIMITER.wait()


class FundamentalsScanner(BaseScanner):
    """Issuer quality for US-listed equities, from SEC company facts."""

    layer = Layer.FUNDAMENTALS
    weight = 1.1
    asset_classes = frozenset({AssetClass.EQUITY})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        user_agent = self.settings.scanners.sec_user_agent
        if not user_agent:
            return self.skipped(token, "SEC_USER_AGENT not configured (required by SEC fair access)")

        symbol = (token.symbol or token.token_address or "").strip().upper()
        if not symbol:
            return self.skipped(token, "symbol unknown; cannot resolve an SEC CIK")

        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        cik, company = await self._cik_for(symbol, ctx, headers)
        if cik is None:
            return self.skipped(token, f"no SEC CIK for {symbol} (non-US listing?)")

        await _respect_sec_rate_limit()
        payload = await request_json(
            ctx.session,
            ctx.settings.endpoints.url("sec.company_facts", cik=cik),
            headers=headers,
        )
        fundamentals = parse_company_facts(payload, symbol=symbol)
        score, flags, confidence, components = score_fundamentals(fundamentals)

        metrics = fundamentals.to_metrics()
        metrics.update({"cik": cik, "company": company, "components": components, "source": "sec_edgar"})
        summary_bits = [company or symbol]
        if fundamentals.revenue_growth_pct is not None:
            summary_bits.append(f"rev {fundamentals.revenue_growth_pct:+.1f}% y/y")
        if fundamentals.net_margin_pct is not None:
            summary_bits.append(f"margin {fundamentals.net_margin_pct:.1f}%")
        if fundamentals.roe_pct is not None:
            summary_bits.append(f"ROE {fundamentals.roe_pct:.1f}%")
        if fundamentals.debt_ratio is not None:
            summary_bits.append(f"debt {fundamentals.debt_ratio * 100:.0f}% of assets")
        if fundamentals.fiscal_year_end:
            summary_bits.append(f"FY {fundamentals.fiscal_year_end}")

        status = SignalStatus.OK if confidence >= 0.65 else SignalStatus.DEGRADED
        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=", ".join(summary_bits),
            status=status,
            metrics=metrics,
            flags=flags,
        )

    @staticmethod
    async def _cik_for(
        symbol: str,
        ctx: ScanContext,
        headers: Mapping[str, str],
    ) -> tuple[int | None, str]:
        if _TICKER_INDEX.is_fresh():
            entry = _TICKER_INDEX.lookup(symbol)
            return (entry[0], entry[1]) if entry else (None, "")
        # One download, not one per concurrent equity: re-check inside the lock.
        async with _TICKER_INDEX.lock:
            if _TICKER_INDEX.is_fresh():
                entry = _TICKER_INDEX.lookup(symbol)
                return (entry[0], entry[1]) if entry else (None, "")
            await _respect_sec_rate_limit()
            payload = await request_json(
                ctx.session,
                ctx.settings.endpoints.url("sec_files.company_tickers"),
                headers=headers,
            )
            _TICKER_INDEX.store(parse_ticker_index(payload))
        entry = _TICKER_INDEX.lookup(symbol)
        return (entry[0], entry[1]) if entry else (None, "")


__all__ = [
    "Fundamentals",
    "FundamentalsScanner",
    "MIN_SEC_INTERVAL_SECONDS",
    "REVENUE_CONCEPTS",
    "TICKER_CACHE_SECONDS",
    "parse_company_facts",
    "parse_ticker_index",
    "score_fundamentals",
]
