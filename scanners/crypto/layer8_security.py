"""Layer 8 - contract security screen with a hard veto.

**GoPlus** supplies ownership powers, mint/blacklist/pause flags, buy/sell tax and
LP-lock data. **honeypot.is** is a second, independent simulation of an actual buy
and sell, which catches honeypots that GoPlus misses and confirms taxes with real
execution.

Rule: any critical flag (honeypot, cannot sell, hidden owner, takeover, >10% sell
tax, blacklist) sets ``token.meta["security_veto"]`` and the snapshot's
:class:`SecurityReport`. ``core.scoring.reconcile`` then vetoes the verdict no
matter how strong every other layer looks, and the risk agent's LLM is
short-circuited.

:func:`fetch_security_report` is the one GoPlus entry point: the pipeline calls it
before the layers so the veto has a report even if this layer is skipped, and the
layer reuses the cached value instead of billing the API twice.
"""

from __future__ import annotations

from typing import Any

from config.settings import Settings
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import Layer, LayerSignal, SecurityReport, SignalStatus, TokenSnapshot, clamp, get_chain_profile
from scanners.base import BaseScanner, ScanContext
from scanners.shared.http_client import request_json, to_float, to_int

log = get_logger(__name__)


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


async def fetch_security_report(token: TokenSnapshot, ctx: ScanContext) -> SecurityReport:
    """One GoPlus read per scan, cached on the context for the layer and the veto."""
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
        ctx.settings.endpoints.url("goplus.token_security", chain_id=profile.goplus_id),
        params={"contract_addresses": token.token_address},
        headers=ctx.settings.goplus.auth_headers,
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
    if not isinstance(entry, dict):
        raise ValueError("GoPlus returned a malformed token entry")

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


def parse_honeypot_check(payload: Any) -> dict[str, Any]:
    """Normalise honeypot.is' simulation result into flags and metrics."""
    if not isinstance(payload, dict):
        return {}
    result = payload.get("honeypotResult") or {}
    simulation = payload.get("simulationResult") or {}
    summary = payload.get("summary") or {}
    holders = payload.get("holderAnalysis") or {}
    is_honeypot = bool(result.get("isHoneypot")) if isinstance(result, dict) else False
    buy_tax = to_float(simulation.get("buyTax")) if isinstance(simulation, dict) else None
    sell_tax = to_float(simulation.get("sellTax")) if isinstance(simulation, dict) else None
    transfer_tax = to_float(simulation.get("transferTax")) if isinstance(simulation, dict) else None

    flags: list[str] = []
    if is_honeypot:
        flags.append("honeypot")
    if sell_tax is not None and sell_tax > 10.0:
        flags.append("sell_tax_gt_10")
    if buy_tax is not None and buy_tax > 10.0:
        flags.append("buy_tax_gt_10")
    if transfer_tax is not None and transfer_tax > 10.0:
        flags.append("transfer_tax_gt_10")
    risk_level = to_float(summary.get("riskLevel")) if isinstance(summary, dict) else None
    if risk_level is not None and risk_level >= 4.0:
        flags.append("honeypot_high_risk")

    return {
        "honeypot": is_honeypot,
        "buy_tax_pct": buy_tax,
        "sell_tax_pct": sell_tax,
        "transfer_tax_pct": transfer_tax,
        "risk_level": risk_level,
        "risk": summary.get("risk") if isinstance(summary, dict) else None,
        "holders": to_float(holders.get("holders")) if isinstance(holders, dict) else None,
        "failed_sells": to_float(holders.get("failed")) if isinstance(holders, dict) else None,
        "simulation_success": bool(payload.get("simulationSuccess")),
        "flags": flags,
    }


def merge_honeypot_report(report: SecurityReport, info: dict[str, Any]) -> SecurityReport:
    """Fold honeypot.is' findings into the GoPlus report (in place).

    GoPlus remains the source of truth for ownership powers; honeypot.is adds an
    executed buy/sell simulation, so its findings can only *raise* risk.
    """
    if not info:
        return report
    report.flags = sorted(set(report.flags) | set(info.get("flags") or []))
    if info.get("honeypot"):
        report.honeypot = True
    buy_tax = to_float(info.get("buy_tax_pct"))
    sell_tax = to_float(info.get("sell_tax_pct"))
    if buy_tax is not None:
        report.buy_tax_pct = max(report.buy_tax_pct, buy_tax)
    if sell_tax is not None:
        report.sell_tax_pct = max(report.sell_tax_pct, sell_tax)
    report.available = True
    return report


class CryptoSecurityScanner(BaseScanner):
    """Layer 8: contract powers, honeypot simulation, taxes and LP lock."""

    layer = Layer.SECURITY
    weight = 1.5
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        report = await self._goplus_report(token, ctx)
        honeypot_info: dict[str, Any] = {}
        profile = get_chain_profile(token.chain)
        if profile.evm and profile.goplus_id is not None:
            try:
                payload = await request_json(
                    ctx.session,
                    self.settings.endpoints.url("honeypot.check"),
                    params={"address": token.token_address, "chainID": profile.goplus_id},
                )
                honeypot_info = parse_honeypot_check(payload)
            except Exception as exc:  # noqa: BLE001 - one provider missing is DEGRADED
                log.info(
                    "honeypot_provider_failed",
                    extra={"token": token.token_address, "error": f"{type(exc).__name__}: {str(exc)[:160]}"},
                )

        report = merge_honeypot_report(report, honeypot_info)
        token.security = report
        if not report.available and not honeypot_info:
            return self.skipped(token, "no security provider returned data for this chain/token")

        critical = report.critical_flags()
        token.meta["security_veto"] = bool(critical)
        token.meta["security_critical_flags"] = critical
        risk_points = report.risk_points()
        score = 0.0 if critical else clamp(100.0 - risk_points)

        status = SignalStatus.DEGRADED if (critical or not report.available) else SignalStatus.OK
        confidence = 1.0 if report.available else 0.6
        summary = (
            f"{'VETO' if critical else 'clear'}: {len(report.flags)} flags "
            f"({', '.join(critical[:4]) or 'no critical'}), "
            f"buy/sell tax {report.buy_tax_pct:.2f}%/{report.sell_tax_pct:.2f}%, "
            f"LP locked {report.lp_locked_pct if report.lp_locked_pct is not None else 'n/a'}%"
        )
        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=summary,
            status=status,
            metrics={
                "flags": report.flags,
                "critical_flags": critical,
                "high_flags": report.high_flags(),
                "risk_points": round(risk_points, 2),
                "buy_tax_pct": report.buy_tax_pct,
                "sell_tax_pct": report.sell_tax_pct,
                "lp_locked_pct": report.lp_locked_pct,
                "holder_count": report.holder_count,
                "top10_holder_pct": report.top10_holder_pct,
                "veto": bool(critical),
                "honeypot": honeypot_info,
            },
            flags=report.flags,
        )

    async def _goplus_report(self, token: TokenSnapshot, ctx: ScanContext) -> SecurityReport:
        try:
            return await fetch_security_report(token, ctx)
        except Exception as exc:  # noqa: BLE001 - honeypot.is may still answer
            log.info(
                "goplus_provider_failed",
                extra={"token": token.token_address, "error": f"{type(exc).__name__}: {str(exc)[:160]}"},
            )
            return SecurityReport(available=False, flags=["goplus_unreachable"])


def build_scanner(settings: Settings) -> CryptoSecurityScanner:
    return CryptoSecurityScanner(settings)


__all__ = [
    "CryptoSecurityScanner",
    "build_scanner",
    "fetch_security_report",
    "merge_honeypot_report",
    "parse_honeypot_check",
    "parse_security_report",
]
