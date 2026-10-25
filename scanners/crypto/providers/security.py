"""GoPlus contract security screen - the deterministic floor under every veto.

This is the one layer that does not ask a language model anything. It reads the
contract's actual powers (can it block sells? change balances? mint?) and turns
them into :class:`SecurityReport` flags. Those flags are *hard*: a honeypot cannot
be talked out of a veto by a persuasive alpha thesis.
"""

from __future__ import annotations

from typing import Any

from core.http import request_json, to_float, to_int
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import Layer, SecurityReport, SignalStatus, LayerSignal, TokenSnapshot, get_chain_profile
from scanners.base import BaseScanner, ScanContext, clamp

log = get_logger(__name__)

GOPLUS_TOKEN_SECURITY_URL = "https://api.gopluslabs.io/api/v1/token_security/{chain_id}"

# Fields GoPlus reports as "1"/"0" strings.
_FLAG_MAP: dict[str, str] = {
    "is_honeypot": "honeypot",
    "hidden_owner": "hidden_owner",
    "can_take_back_ownership": "can_take_back_ownership",
    "owner_change_balance": "owner_change_balance",
    "selfdestruct": "selfdestruct",
    "is_blacklisted": "blacklist_enabled",
    "is_mintable": "mintable",
    "transfer_pausable": "transfer_pausable",
    "is_proxy": "proxy_contract",
    "slippage_modifiable": "slippage_modifiable",
    "trading_cooldown": "trading_cooldown",
    "anti_whale_modifiable": "anti_whale_modifiable",
    "external_call": "external_call",
    "is_anti_whale": "anti_whale",
    "is_open_source": None,  # inverted: "0" is bad, handled separately
}


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def _holder_concentration(holders: Any) -> tuple[float | None, int | None]:
    """Sum the top-10 non-contract holders, ignoring locked/LP wallets."""
    if not isinstance(holders, list):
        return None, None
    percentages: list[float] = []
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        if _truthy(holder.get("is_contract")) or _truthy(holder.get("is_locked")):
            continue
        percent = to_float(holder.get("percent"))
        if percent is not None:
            percentages.append(percent * 100.0)
    if not percentages:
        return None, None
    percentages.sort(reverse=True)
    return round(sum(percentages[:10]), 2), len(holders)


def _lp_locked_pct(lp_holders: Any) -> float | None:
    if not isinstance(lp_holders, list) or not lp_holders:
        return None
    locked = 0.0
    for holder in lp_holders:
        if not isinstance(holder, dict):
            continue
        percent = to_float(holder.get("percent")) or 0.0
        if _truthy(holder.get("is_locked")):
            locked += percent
    return round(min(100.0, locked * 100.0), 2)


def parse_security_report(payload: dict[str, Any]) -> SecurityReport:
    """Map a GoPlus ``result`` object onto our normalised report."""
    flags: list[str] = []
    for field, flag in _FLAG_MAP.items():
        if flag is None:
            continue
        if _truthy(payload.get(field)):
            flags.append(flag)

    buy_tax = to_float(payload.get("buy_tax")) or 0.0
    sell_tax = to_float(payload.get("sell_tax")) or 0.0
    buy_tax_pct, sell_tax_pct = round(buy_tax * 100.0, 2), round(sell_tax * 100.0, 2)
    if buy_tax_pct > 10.0:
        flags.append("buy_tax_gt_10")
    if sell_tax_pct > 10.0:
        flags.append("sell_tax_gt_10")
    if sell_tax_pct >= 99.0 or _truthy(payload.get("cannot_sell_all")):
        flags.append("cannot_sell")
    if not _truthy(payload.get("is_open_source")):
        flags.append("closed_source")
    if not _truthy(payload.get("is_in_dex")) and payload.get("is_in_dex") is not None:
        flags.append("not_tradeable_on_dex")

    top10_pct, holder_rows = _holder_concentration(payload.get("holders"))
    if top10_pct is not None and top10_pct > 80.0:
        flags.append("top10_holders_gt_80")
    lp_locked = _lp_locked_pct(payload.get("lp_holders"))
    if lp_locked is not None and lp_locked <= 0.0:
        flags.append("lp_unlocked")

    holder_count = to_int(payload.get("holder_count")) or holder_rows
    return SecurityReport(
        available=True,
        honeypot="honeypot" in flags,
        buy_tax_pct=buy_tax_pct,
        sell_tax_pct=sell_tax_pct,
        mintable="mintable" in flags,
        owner_privileges=bool({"slippage_modifiable", "anti_whale_modifiable"} & set(flags)),
        hidden_owner="hidden_owner" in flags,
        is_proxy="proxy_contract" in flags,
        can_take_back_ownership="can_take_back_ownership" in flags,
        blacklist_enabled="blacklist_enabled" in flags,
        transfer_pausable="transfer_pausable" in flags,
        holder_count=holder_count,
        top10_holder_pct=top10_pct,
        lp_locked_pct=lp_locked,
        flags=sorted(set(flags)),
        raw=payload,
    )


class SecurityScanner(BaseScanner):
    """Wraps GoPlus; caches the report so the pipeline can attach it to the snapshot."""

    layer = Layer.SECURITY
    weight = 0.0  # veto-only: never contributes bullishness to the prior
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def scan(self, token: TokenSnapshot, ctx: ScanContext) -> SecurityReport:
        cached = ctx.get_shared("security")
        if isinstance(cached, SecurityReport):
            return cached

        profile = get_chain_profile(token.chain)
        if profile.goplus_id is None:
            report = SecurityReport(available=False, flags=["goplus_chain_unsupported"])
            ctx.set_shared("security", report)
            return report

        payload = await request_json(
            ctx.session,
            GOPLUS_TOKEN_SECURITY_URL.format(chain_id=profile.goplus_id),
            params={"contract_addresses": token.token_address},
            headers=self.settings.goplus.auth_headers,
        )
        result = (payload or {}).get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            raise ValueError(f"unexpected GoPlus response: {str(payload)[:200]}")
        entry = result.get(token.token_address) or result.get(token.token_address.lower())
        if not isinstance(entry, dict):
            if result:
                entry = next(iter(result.values()))
            else:
                report = SecurityReport(available=False, flags=["goplus_no_data"])
                ctx.set_shared("security", report)
                return report
        report = parse_security_report(entry)
        ctx.set_shared("security", report)
        log.debug(
            "goplus_report",
            extra={
                "token": token.token_address,
                "flags": report.flags,
                "critical": report.critical_flags(),
                "top10_pct": report.top10_holder_pct,
            },
        )
        return report

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:
        try:
            report = await self.scan(token, ctx)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash
            ctx.set_shared("security", SecurityReport(available=False, flags=["goplus_unreachable"]))
            return self.unavailable(token, f"{type(exc).__name__}: {str(exc)[:200]}")
        if not report.available:
            return self.unavailable(token, f"security data missing: {', '.join(report.flags) or 'no data'}")

        score = clamp(100.0 - report.risk_points())
        flags = report.critical_flags()
        status = SignalStatus.DEGRADED if flags else SignalStatus.OK
        summary = (
            f"GoPlus: {len(report.flags)} flags ({', '.join(report.flags[:4]) or 'none'}); "
            f"top10 {report.top10_holder_pct if report.top10_holder_pct is not None else 'n/a'}%, "
            f"lp locked {report.lp_locked_pct if report.lp_locked_pct is not None else 'n/a'}%"
        )
        return self.signal(
            token,
            score=score,
            confidence=1.0,
            summary=summary,
            status=status,
            metrics={
                "flags": report.flags,
                "critical_flags": flags,
                "high_flags": report.high_flags(),
                "risk_points": round(report.risk_points(), 2),
                "top10_holder_pct": report.top10_holder_pct,
                "lp_locked_pct": report.lp_locked_pct,
                "holder_count": report.holder_count,
            },
            flags=report.flags,
        )


__all__ = ["GOPLUS_TOKEN_SECURITY_URL", "SecurityScanner", "parse_security_report"]
