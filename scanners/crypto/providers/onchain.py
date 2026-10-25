"""On-chain layer: holder distribution and transfer activity (Blockscout v2).

Blockscout is free, keyless and available for every EVM chain we cover. We use it
for what it is actually good at - *who holds the supply* and *how busy the token
is* - and never for price data (that is the DEX layer's job).
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.http import request_json, to_float, to_int
from core.logging import get_logger
from core.instruments import AssetClass
from core.models import Layer, SignalStatus, LayerSignal, TokenSnapshot, get_chain_profile
from scanners.base import BaseScanner, ScanContext, log_scale, safe_div

log = get_logger(__name__)

TOKEN_INFO_PATH = "/api/v2/tokens/{address}"
TOKEN_COUNTERS_PATH = "/api/v2/tokens/{address}/counters"
TOKEN_HOLDERS_PATH = "/api/v2/tokens/{address}/holders"


#: Addresses that hold supply but are not "insiders": the zero address and burns.
_SINK_ADDRESSES = frozenset(
    {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}
)


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


class OnChainScanner(BaseScanner):
    """Holder concentration, holder count and transfer velocity."""

    layer = Layer.ONCHAIN
    weight = 1.3
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        profile = get_chain_profile(token.chain)
        if not profile.blockscout_url:
            return self.skipped(token, f"no Blockscout instance for {token.chain}")

        base = profile.blockscout_url.rstrip("/")
        info, counters, holders = await self._fetch_all(base, token, ctx)

        holder_count = to_int(info.get("holders")) or to_int(counters.get("token_holders_count"))
        transfers = to_int(counters.get("transfers_count"))
        total_supply = to_float(info.get("total_supply"))
        top10_pct, inspected = concentration_from_holders(
            (holders or {}).get("items"), total_supply=total_supply
        )
        concentration_basis = "total_supply" if total_supply and total_supply > 0 else "holders_page"
        if token.holder_count is None and holder_count:
            token.holder_count = holder_count

        if holder_count is None and top10_pct is None and transfers is None:
            return self.unavailable(token, "Blockscout returned no usable counters")

        flags: list[str] = []
        if top10_pct is not None and top10_pct > 60.0:
            flags.append("top10_concentration_high")
        if holder_count is not None and holder_count < 200:
            flags.append("low_holder_count")

        concentration_score = 50.0 if top10_pct is None else max(0.0, 100.0 - top10_pct * 1.15)
        holders_score = log_scale(holder_count, 100.0, 50_000.0)
        age_hours = token.age_hours
        transfers_per_day = safe_div(transfers, safe_div(age_hours, 24.0)) if transfers and age_hours else None
        activity_score = log_scale(transfers_per_day, 5.0, 2_000.0) if transfers_per_day else 50.0

        score = 0.55 * concentration_score + 0.25 * holders_score + 0.20 * activity_score
        if "top10_concentration_high" in flags:
            score = min(score, 45.0)

        confidence = 0.9 if top10_pct is not None else 0.55
        if concentration_basis != "total_supply":
            # A page-relative ratio overstates concentration; keep the score but
            # make the layer contribute less to the prior.
            confidence *= 0.75
        if inspected < 10:
            confidence *= 0.7
        status = SignalStatus.OK if (top10_pct is not None and transfers is not None) else SignalStatus.DEGRADED

        summary = (
            f"{holder_count or 'n/a'} holders, top10 {top10_pct if top10_pct is not None else 'n/a'}%, "
            f"{transfers or 'n/a'} transfers"
            + (f" (~{transfers_per_day:,.0f}/day)" if transfers_per_day else "")
        )
        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=summary,
            status=status,
            metrics={
                "holder_count": holder_count,
                "top10_holder_pct": top10_pct,
                "concentration_basis": concentration_basis,
                "holders_inspected": inspected,
                "transfers": transfers,
                "transfers_per_day": round(transfers_per_day, 1) if transfers_per_day else None,
                "total_supply": total_supply,
            },
            flags=flags,
        )

    async def _fetch_all(
        self,
        base: str,
        token: TokenSnapshot,
        ctx: ScanContext,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        async def get(path: str) -> dict[str, Any]:
            try:
                payload = await request_json(ctx.session, base + path.format(address=token.token_address))
                return payload if isinstance(payload, dict) else {}
            except Exception as exc:  # noqa: BLE001 - partial data is fine
                log.debug("blockscout_endpoint_failed", extra={"path": path, "error": str(exc)[:200]})
                return {}

        info, counters, holders = await asyncio.gather(
            get(TOKEN_INFO_PATH),
            get(TOKEN_COUNTERS_PATH),
            get(TOKEN_HOLDERS_PATH),
        )
        return info, counters, holders


__all__ = ["OnChainScanner", "concentration_from_holders"]
