"""Layer 1 - on-chain intelligence: smart-money and exchange flows.

Sources, in order of preference:

* **Arkham** - entity-labelled transfers (smart money, CEX wallets).
* **Nansen** - smart-money netflow for a specific token.
* **Dune** - a saved SQL query returning one net-flow row per token.
* **Blockscout** - keyless fallback for holder concentration and transfer
  velocity, and always used to enrich the paid reads.

Any subset may be unconfigured. The layer only skips when *nothing* - paid or
keyless - can describe the token, so a missing API key degrades the read instead
of silently deleting the layer from the prior.
"""

from __future__ import annotations

import asyncio
from typing import Any

from config.settings import Settings
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import Layer, LayerSignal, SignalStatus, TokenSnapshot, clamp, get_chain_profile
from scanners.base import BaseScanner, ScanContext, log_scale
from scanners.shared.http_client import (
    ProviderResult,
    fetch_provider,
    first_float,
    first_row,
    request_json,
    to_float,
    to_int,
)

log = get_logger(__name__)


#: Addresses that hold supply but are not "insiders": the zero address and burns.
_SINK_ADDRESSES = frozenset(
    {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}
)


CEX_HINTS = (
    "binance",
    "coinbase",
    "okx",
    "bybit",
    "kraken",
    "kucoin",
    "gate",
    "huobi",
    "htx",
    "bitfinex",
    "upbit",
    "mexc",
    "bitstamp",
    "gemini",
    "crypto.com",
)
SMART_MONEY_HINTS = ("smart money", "fund", "capital", "ventures", "family office")


def concentration_from_holders(
    rows: Any,
    *,
    top_n: int = 10,
    total_supply: float | None = None,
) -> tuple[float | None, int]:
    """Return ``(top-N share %, rows inspected)`` from a Blockscout holder list.

    Blockscout returns only the first page (~50 holders), so dividing by the page
    total would measure "share of the top 50" and flag almost every token. When
    the token endpoint reports ``total_supply`` (same raw base units) that is the
    denominator; the page total is a last resort and the caller lowers confidence.
    """
    if not isinstance(rows, list) or not rows:
        return None, 0
    values: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = to_float(row.get("value")) or 0.0
        if value <= 0:
            continue
        address = str(row.get("address") or row.get("holder_address") or "").lower()
        values.append((address, value))
    if not values:
        return None, 0
    denominator = total_supply if total_supply is not None and total_supply > 0 else sum(value for _, value in values)
    eligible = sorted(
        (value for address, value in values if address not in _SINK_ADDRESSES),
        reverse=True,
    )
    top = sum(eligible[:top_n])
    return round(min(100.0, top / denominator * 100.0), 2), len(values)


def _entity_text(address: Any) -> str:
    """Flatten Arkham's entity/label objects into one lower-case search string."""
    if not isinstance(address, dict):
        return ""
    parts: list[str] = []
    for key in ("arkhamEntity", "arkhamLabel", "entity", "label"):
        value = address.get(key)
        if isinstance(value, dict):
            parts.extend(str(value.get(field) or "") for field in ("name", "type", "id"))
        elif value:
            parts.append(str(value))
    parts.append(str(address.get("address") or ""))
    return " ".join(parts).lower()


def is_cex_entity(address: Any) -> bool:
    text = _entity_text(address)
    return any(hint in text for hint in CEX_HINTS)


def is_smart_money_entity(address: Any) -> bool:
    text = _entity_text(address)
    return any(hint in text for hint in SMART_MONEY_HINTS)


def parse_arkham_transfers(payload: Any) -> dict[str, float | int]:
    """Aggregate Arkham transfers into CEX and smart-money netflows.

    Convention: ``cex_netflow_usd`` is positive when tokens move *into* an
    exchange (sell-side pressure) and ``smart_money_netflow_usd`` is positive
    when labelled funds accumulate.
    """
    rows = first_row(payload)
    cex_in = cex_out = smart_in = smart_out = 0.0
    counted = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        usd = first_float(row, "historicalUSD", "usd", "valueUsd", "unitValue")
        if usd is None or usd <= 0:
            continue
        sender = row.get("fromAddress") or row.get("from")
        receiver = row.get("toAddress") or row.get("to")
        counted += 1
        sender_cex, receiver_cex = is_cex_entity(sender), is_cex_entity(receiver)
        if receiver_cex and not sender_cex:
            cex_in += usd
        elif sender_cex and not receiver_cex:
            cex_out += usd
        sender_smart, receiver_smart = is_smart_money_entity(sender), is_smart_money_entity(receiver)
        if receiver_smart and not sender_smart:
            smart_in += usd
        elif sender_smart and not receiver_smart:
            smart_out += usd
    if not counted:
        return {}
    return {
        "transfers_sampled": counted,
        "cex_inflow_usd": round(cex_in, 2),
        "cex_outflow_usd": round(cex_out, 2),
        "cex_netflow_usd": round(cex_in - cex_out, 2),
        "smart_money_inflow_usd": round(smart_in, 2),
        "smart_money_outflow_usd": round(smart_out, 2),
        "smart_money_netflow_usd": round(smart_in - smart_out, 2),
    }


def parse_nansen_netflow(payload: Any, token_address: str = "") -> dict[str, float | int]:
    """Sum Nansen's smart-money netflow rows, preferring rows for this token."""
    rows = first_row(payload)
    total = 0.0
    matched = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        address = str(row.get("token_address") or row.get("address") or "").lower()
        if token_address and address and address != token_address.lower():
            continue
        value = first_float(row, "net_flow_usd", "netflow_usd", "net_flow_24h_usd", "value_usd")
        if value is None:
            continue
        total += value
        matched += 1
    if not matched:
        return {}
    return {
        "nansen_rows": matched,
        "nansen_smart_money_netflow_usd": round(total, 2),
    }


def parse_dune_rows(payload: Any, token_address: str = "") -> dict[str, float | int]:
    """Read a saved Dune query's cached results (one net-flow row per token)."""
    envelope = payload.get("result") if isinstance(payload, dict) else payload
    rows = first_row(envelope)
    total = 0.0
    cex = 0.0
    matched = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        address = str(row.get("token_address") or row.get("address") or "").lower()
        if token_address and address and address != token_address.lower():
            continue
        value = first_float(row, "net_flow_usd", "netflow_usd", "smart_money_netflow_usd")
        if value is None:
            continue
        total += value
        flow_type = str(row.get("flow_type") or row.get("category") or "").lower()
        if "cex" in flow_type or "exchange" in flow_type:
            cex += value
        matched += 1
    if not matched:
        return {}
    return {
        "dune_rows": matched,
        "dune_smart_money_netflow_usd": round(total, 2),
        "dune_cex_netflow_usd": round(cex, 2),
    }


def flow_score(netflow_usd: float | None, market_cap_usd: float | None = None) -> float | None:
    """Map a dollar netflow onto 0-100, scaled by market cap when available.

    0.1% of market cap is treated as a meaningful day of flow, with a $25k floor
    so micro-caps do not turn a $500 transfer into a max-conviction signal.
    """
    if netflow_usd is None:
        return None
    scale = max(25_000.0, (market_cap_usd or 0.0) * 0.001)
    return clamp(50.0 + (netflow_usd / scale) * 25.0)


class CryptoOnChainScanner(BaseScanner):
    """Layer 1: smart-money netflow, CEX balances and holder structure."""

    layer = Layer.ONCHAIN
    weight = 1.3
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        calls = self._provider_calls(token, ctx)
        results: list[ProviderResult] = (
            await asyncio.gather(*(fetch_provider(name, coro) for name, coro in calls.items()))
            if calls
            else []
        )

        metrics: dict[str, Any] = {}
        sources: list[str] = []
        for result in results:
            if result.ok:
                metrics.update(result.metrics)
                sources.append(result.source)

        metrics.update(await self._holder_metrics(token, ctx))
        if not metrics:
            raise RuntimeError("on-chain providers returned no data")

        if "smart_money_netflow_usd" not in metrics:
            for key in ("nansen_smart_money_netflow_usd", "dune_smart_money_netflow_usd"):
                if key in metrics:
                    metrics["smart_money_netflow_usd"] = metrics[key]
                    break
        if "cex_netflow_usd" not in metrics and "dune_cex_netflow_usd" in metrics:
            metrics["cex_netflow_usd"] = metrics["dune_cex_netflow_usd"]

        smart = to_float(metrics.get("smart_money_netflow_usd"))
        cex = to_float(metrics.get("cex_netflow_usd"))
        top10 = to_float(metrics.get("top10_holder_pct"))
        holders = to_int(metrics.get("holder_count"))

        components: list[tuple[float, float]] = []
        smart_score = flow_score(smart, token.market_cap_usd)
        if smart_score is not None:
            components.append((0.40, smart_score))
        cex_score = flow_score(cex, token.market_cap_usd)
        if cex_score is not None:
            # Inflow to exchanges is distribution, so the scale is inverted.
            components.append((0.25, 100.0 - cex_score))
        if top10 is not None:
            components.append((0.25, clamp(100.0 - top10 * 1.15)))
        if holders is not None:
            components.append((0.10, log_scale(holders, 100.0, 50_000.0)))

        total_weight = sum(weight for weight, _ in components) or 1.0
        score = sum(weight * value for weight, value in components) / total_weight

        flags: list[str] = []
        if smart is not None and smart < 0:
            flags.append("smart_money_distribution")
        if cex is not None and cex > 0:
            flags.append("net_exchange_inflow")
        if top10 is not None and top10 > 60.0:
            flags.append("top10_concentration_high")
        if holders is not None and holders < 200:
            flags.append("few_holders")
        if not sources:
            flags.append("paid_providers_unconfigured")

        confidence = 0.85 if smart is not None and cex is not None else 0.6 if len(components) >= 2 else 0.45
        if metrics.get("concentration_basis") == "holders_page":
            confidence *= 0.8
        status = SignalStatus.OK if len(components) >= 3 else SignalStatus.DEGRADED
        smart_text = f"${smart:,.0f}" if smart is not None else "n/a"
        cex_text = f"${cex:,.0f}" if cex is not None else "n/a"
        summary = (
            f"smart money netflow {smart_text}, CEX netflow {cex_text}, "
            f"top10 {top10 if top10 is not None else 'n/a'}%, holders {holders or 'n/a'} "
            f"[{'+'.join(sources) or 'blockscout only'}]"
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
    def _provider_calls(self, token: TokenSnapshot, ctx: ScanContext) -> dict[str, Any]:
        calls: dict[str, Any] = {}
        providers = self.settings.providers
        if providers.arkham_api_key.is_configured():
            calls["arkham"] = self._arkham(token, ctx)
        if providers.nansen_api_key.is_configured():
            calls["nansen"] = self._nansen(token, ctx)
        if providers.dune_api_key.is_configured() and providers.dune_smart_money_query_id > 0:
            calls["dune"] = self._dune(token, ctx)
        return calls

    async def _arkham(self, token: TokenSnapshot, ctx: ScanContext) -> dict[str, Any]:
        payload = await request_json(
            ctx.session,
            self.settings.endpoints.url("arkham.transfers"),
            params={"base": token.token_address, "chains": token.chain, "flow": "all", "limit": 100},
            headers={"API-Key": self.settings.providers.arkham_api_key.get_secret_value()},
        )
        return parse_arkham_transfers(payload)

    async def _nansen(self, token: TokenSnapshot, ctx: ScanContext) -> dict[str, Any]:
        payload = await request_json(
            ctx.session,
            self.settings.endpoints.url("nansen.smart_money_netflows"),
            method="POST",
            json_body={
                "chains": [token.chain],
                "token_addresses": [token.token_address],
                "limit": 100,
            },
            headers={"api-key": self.settings.providers.nansen_api_key.get_secret_value()},
        )
        parsed = parse_nansen_netflow(payload, token.token_address)
        if parsed and "smart_money_netflow_usd" not in parsed:
            parsed["smart_money_netflow_usd"] = parsed["nansen_smart_money_netflow_usd"]
        return parsed

    async def _dune(self, token: TokenSnapshot, ctx: ScanContext) -> dict[str, Any]:
        query_id = self.settings.providers.dune_smart_money_query_id
        payload = await request_json(
            ctx.session,
            self.settings.endpoints.url("dune.query_results", query_id=query_id),
            headers={"X-Dune-API-Key": self.settings.providers.dune_api_key.get_secret_value()},
        )
        return parse_dune_rows(payload, token.token_address)

    async def _holder_metrics(self, token: TokenSnapshot, ctx: ScanContext) -> dict[str, Any]:
        """Keyless Blockscout read: concentration, holder count, transfer count."""
        profile = get_chain_profile(token.chain)
        endpoints = self.settings.endpoints
        explorer = {
            resource: endpoints.blockscout(
                resource,
                chain=token.chain,
                default=profile.blockscout_url,
                address=token.token_address,
            )
            for resource in ("token", "counters", "holders")
        }
        if explorer["token"] is None:
            return {}

        async def get(url: str | None) -> dict[str, Any]:
            if url is None:
                return {}
            try:
                payload = await request_json(ctx.session, url)
                return payload if isinstance(payload, dict) else {}
            except Exception as exc:  # noqa: BLE001 - enrichment must not fail the layer
                log.debug("blockscout_endpoint_failed", extra={"url": url, "error": str(exc)[:160]})
                return {}

        info, counters, holders = await asyncio.gather(
            get(explorer["token"]),
            get(explorer["counters"]),
            get(explorer["holders"]),
        )
        total_supply = to_float(info.get("total_supply"))
        top10, inspected = concentration_from_holders(
            (holders or {}).get("items"), total_supply=total_supply
        )
        holder_count = to_int(info.get("holders")) or to_int(counters.get("token_holders_count"))
        transfers = to_int(counters.get("transfers_count"))
        metrics: dict[str, Any] = {}
        if holder_count is not None:
            metrics["holder_count"] = holder_count
            if token.holder_count is None:
                token.holder_count = holder_count
        if top10 is not None:
            metrics["top10_holder_pct"] = top10
            metrics["concentration_basis"] = (
                "total_supply" if total_supply and total_supply > 0 else "holders_page"
            )
            metrics["holders_inspected"] = inspected
        if transfers is not None:
            metrics["transfers"] = transfers
        return metrics


def build_scanner(settings: Settings) -> CryptoOnChainScanner:
    return CryptoOnChainScanner(settings)


__all__ = [
    "CryptoOnChainScanner",
    "build_scanner",
    "concentration_from_holders",
    "flow_score",
    "is_cex_entity",
    "is_smart_money_entity",
    "parse_arkham_transfers",
    "parse_dune_rows",
    "parse_nansen_netflow",
]
