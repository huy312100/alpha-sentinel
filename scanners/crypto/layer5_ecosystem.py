"""Layer 5 - ecosystem health: TVL trajectory and active-address growth.

* **DeFiLlama** - protocol TVL history, matched by token symbol and chain.
* **GrowThePie** - chain-level active addresses (``daa``) and activity trend.

Both are keyless. GrowThePie only covers the chains in its registry; an
unsupported chain simply drops that component instead of failing the layer.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from config.settings import Settings
from core.instruments import AssetClass
from core.models import Layer, LayerSignal, SignalStatus, TokenSnapshot
from scanners.base import BaseScanner, ScanContext, linear, log_scale, safe_div
from scanners.shared.http_client import (
    ProviderResult,
    fetch_provider,
    first_float,
    first_row,
    request_json,
    to_float,
)


#: Chain slug -> GrowThePie ``origin_key``. Absent means "not covered".
GROWTHEPIE_CHAINS = {
    "ethereum": "ethereum",
    "arbitrum": "arbitrum",
    "base": "base",
    "optimism": "optimism",
    "polygon": "polygon",
}

#: Our chain slug -> the names DeFiLlama uses in ``protocol.chains``. ``bsc`` is
#: listed as ``Binance`` and ``optimism`` as ``OP Mainnet``; without the mapping a
#: correct match looks like a chain mismatch and the TVL read is dropped.
DEFILLAMA_CHAIN_ALIASES: dict[str, tuple[str, ...]] = {
    "bsc": ("bsc", "binance", "bnb smart chain"),
    "optimism": ("optimism", "op mainnet", "op"),
    "polygon": ("polygon", "polygon pos", "matic"),
}


#: The protocol catalogue is ~2 MB; one read per hour is plenty.
PROTOCOLS_TTL_SECONDS = 3_600.0
_protocols_cache: dict[str, Any] = {"stamp": 0.0, "rows": []}


def _tvl_series(payload: Any) -> list[tuple[float, float]]:
    rows = payload.get("tvl") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    series: list[tuple[float, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = first_float(row, "totalLiquidityUSD", "totalLiquidity", "tvl")
        stamp = first_float(row, "date", "timestamp")
        if value is None:
            continue
        series.append((stamp or 0.0, value))
    series.sort(key=lambda item: item[0])
    return series


def parse_defillama_protocol(payload: Any) -> dict[str, Any]:
    """Current TVL plus 7d/30d change and market-cap coverage."""
    series = _tvl_series(payload)
    if not series:
        return {}
    values = [value for _, value in series]
    current = values[-1]
    week = values[-8] if len(values) >= 8 else values[0]
    month = values[-31] if len(values) >= 31 else values[0]

    def change(previous: float) -> float | None:
        delta = safe_div(current - previous, previous)
        return round(delta * 100.0, 2) if delta is not None else None

    mcap = first_float(payload, "mcap", "marketCap")
    return {
        "tvl_usd": round(current, 2),
        "tvl_change_7d_pct": change(week),
        "tvl_change_30d_pct": change(month),
        "mcap_usd": mcap,
        "tvl_to_mcap": round(current / mcap, 4) if mcap and mcap > 0 else None,
        "tvl_samples": len(values),
    }


def _chain_key(name: Any) -> str:
    """Chain name reduced to ``[a-z0-9]``: ``"OP Mainnet"`` -> ``opmainnet``."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _chain_keys(chain: str) -> set[str]:
    """Every DeFiLlama spelling of our ``chain`` slug."""
    slug = (chain or "").strip().lower()
    names = (slug, *DEFILLAMA_CHAIN_ALIASES.get(slug, ()))
    return {_chain_key(name) for name in names if name}


def _addresses_match(left: Any, right: str) -> bool:
    return str(left or "").strip().lower() == (right or "").strip().lower()


def match_protocol(
    protocols: Any,
    symbol: str,
    chain: str,
    *,
    address: str | None = None,
) -> dict[str, Any] | None:
    """Pick the DeFiLlama protocol that owns ``symbol`` on ``chain``.

    A ticker is not an identity: bridged and wrapped variants share it, and the
    catalogue covers every chain. The match therefore needs the chain, and when
    ``address`` is supplied it needs the address too - the biggest same-ticker
    protocol elsewhere is another token's TVL, not this one's.

    ``None`` means "not indexed here", never an outage.
    """
    if not isinstance(protocols, list):
        return None
    wanted = symbol.strip().upper()
    if not wanted:
        return None
    chain_keys = _chain_keys(chain)
    pool = [
        row
        for row in protocols
        if isinstance(row, dict)
        and str(row.get("symbol") or "").strip().upper() == wanted
        and chain_keys & {_chain_key(name) for name in (row.get("chains") or [])}
    ]
    if not pool:
        return None
    token_address = (address or "").strip().lower()
    if token_address:
        matched = [row for row in pool if _addresses_match(row.get("address"), token_address)]
        if matched:
            pool = matched
        elif any(str(row.get("address") or "").strip() for row in pool):
            # The catalogue knows the addresses and none is ours: same ticker,
            # different token on the same chain.
            return None
    return max(pool, key=lambda row: to_float(row.get("tvl")) or 0.0)


def parse_growthepie_metric(payload: Any) -> dict[str, Any]:
    """Latest daily value and its 7-day change from GrowThePie's detail payload."""
    details = payload.get("details") if isinstance(payload, dict) else None
    timeseries = (details or {}).get("timeseries") if isinstance(details, dict) else None
    daily = (timeseries or {}).get("daily") if isinstance(timeseries, dict) else None
    rows = first_row(daily) or (daily.get("data") if isinstance(daily, dict) else None)
    if not isinstance(rows, list) or not rows:
        return {}
    values: list[float] = []
    for row in rows:
        if isinstance(row, list) and len(row) >= 2:
            value = to_float(row[1])
        elif isinstance(row, dict):
            value = first_float(row, "value", "daa", "active_addresses")
        else:
            value = None
        if value is not None:
            values.append(value)
    if not values:
        return {}
    latest = values[-1]
    week = values[-8] if len(values) >= 8 else values[0]
    change = safe_div(latest - week, week) if week else None
    return {
        "active_addresses": latest,
        "active_addresses_change_7d_pct": round(change * 100.0, 2) if change is not None else None,
        "active_addresses_samples": len(values),
    }


class CryptoEcosystemScanner(BaseScanner):
    """Layer 5: protocol TVL trajectory and chain activity growth."""

    layer = Layer.ECOSYSTEM
    weight = 0.8
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        symbol = (token.symbol or "").strip().upper()
        if not symbol:
            return self.skipped(token, "token symbol unknown; ecosystem lookup needs a ticker")

        calls = self._provider_calls(symbol, token.chain, token.token_address, ctx)
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
            reasons = "; ".join(f"{result.source}: {result.error}" for result in results)
            return self.skipped(token, f"no ecosystem data for {symbol} ({reasons or 'no provider'})")

        tvl = to_float(metrics.get("tvl_usd"))
        tvl_30d = to_float(metrics.get("tvl_change_30d_pct"))
        tvl_7d = to_float(metrics.get("tvl_change_7d_pct"))
        active = to_float(metrics.get("active_addresses"))
        active_change = to_float(metrics.get("active_addresses_change_7d_pct"))

        components: list[tuple[float, float]] = []
        if tvl_30d is not None:
            components.append((0.40, linear(tvl_30d, -30.0, 30.0)))
        if tvl is not None:
            components.append((0.35, log_scale(tvl, 100_000.0, 1_000_000_000.0)))
        if active_change is not None:
            components.append((0.25, linear(active_change, -25.0, 25.0)))
        elif active is not None:
            components.append((0.25, log_scale(active, 100.0, 1_000_000.0)))

        total_weight = sum(weight for weight, _ in components) or 1.0
        score = sum(weight * value for weight, value in components) / total_weight

        flags: list[str] = []
        if tvl_30d is not None and tvl_30d < -15.0:
            flags.append("tvl_outflow_30d")
        if tvl_7d is not None and tvl_7d < -25.0:
            flags.append("tvl_collapse_7d")
        if active_change is not None and active_change < -20.0:
            flags.append("active_addresses_falling")
        if tvl is not None and tvl < 100_000.0:
            flags.append("tvl_below_100k")
        if tvl_30d is not None and tvl_30d > 25.0:
            flags.append("tvl_growth_above_25pct")

        confidence = 0.8 if len(components) >= 3 else 0.6 if len(components) == 2 else 0.45
        status = SignalStatus.OK if len(components) >= 2 else SignalStatus.DEGRADED
        tvl_text = f"${tvl:,.0f}" if tvl is not None else "n/a"
        summary = (
            f"{symbol}: TVL {tvl_text} ({tvl_30d if tvl_30d is not None else 'n/a'}% 30d), "
            f"active addresses {active if active is not None else 'n/a'} "
            f"({active_change if active_change is not None else 'n/a'}% 7d) "
            f"[{'+'.join(sources)}]"
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
    def _provider_calls(
        self, symbol: str, chain: str, address: str, ctx: ScanContext
    ) -> dict[str, Any]:
        calls: dict[str, Any] = {"defillama": self._defillama(symbol, chain, address, ctx)}
        if GROWTHEPIE_CHAINS.get(chain.lower()):
            calls["growthepie"] = self._growthepie(chain.lower(), ctx)
        return calls

    async def _defillama(
        self, symbol: str, chain: str, address: str, ctx: ScanContext
    ) -> dict[str, Any]:
        protocols = await self._protocol_catalogue(ctx)
        protocol = match_protocol(protocols, symbol, chain, address=address)
        if protocol is None:
            return {}
        slug = str(protocol.get("slug") or protocol.get("id") or "").strip()
        if not slug:
            return {}
        payload = await request_json(
            ctx.session,
            self.settings.endpoints.url("defillama.protocol", slug=slug),
        )
        metrics = parse_defillama_protocol(payload)
        metrics["defillama_slug"] = slug
        metrics["defillama_name"] = protocol.get("name")
        return metrics

    async def _protocol_catalogue(self, ctx: ScanContext) -> list[Any]:
        now = time.monotonic()
        rows = _protocols_cache.get("rows")
        if rows and now - float(_protocols_cache.get("stamp") or 0.0) <= PROTOCOLS_TTL_SECONDS:
            return rows
        payload = await request_json(
            ctx.session,
            self.settings.endpoints.url("defillama.protocols"),
        )
        rows = payload if isinstance(payload, list) else []
        if rows:
            _protocols_cache["stamp"] = now
            _protocols_cache["rows"] = rows
        return rows

    async def _growthepie(self, chain: str, ctx: ScanContext) -> dict[str, Any]:
        origin = GROWTHEPIE_CHAINS[chain]
        payload = await request_json(
            ctx.session,
            self.settings.endpoints.url("growthepie.chain_metric", chain=origin, metric="daa"),
        )
        return parse_growthepie_metric(payload)


def build_scanner(settings: Settings) -> CryptoEcosystemScanner:
    return CryptoEcosystemScanner(settings)


__all__ = [
    "CryptoEcosystemScanner",
    "DEFILLAMA_CHAIN_ALIASES",
    "GROWTHEPIE_CHAINS",
    "build_scanner",
    "match_protocol",
    "parse_defillama_protocol",
    "parse_growthepie_metric",
]
