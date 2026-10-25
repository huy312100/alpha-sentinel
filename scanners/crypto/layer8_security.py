"""Layer 8 - contract security screen with a hard veto.

GoPlus (via :class:`scanners.crypto.providers.security.SecurityScanner`) supplies ownership powers,
mint/blacklist/pause flags, buy/sell tax and LP-lock data. **honeypot.is** is a
second, independent simulation of an actual buy and sell, which catches honeypots
that GoPlus misses and confirms taxes with real execution.

Rule: any critical flag (honeypot, cannot sell, hidden owner, takeover, >10% sell
tax, blacklist) sets ``token.meta["security_veto"]`` and the snapshot's
:class:`SecurityReport`. ``core.scoring.reconcile`` then vetoes the verdict no
matter how strong every other layer looks, and the risk agent's LLM is
short-circuited.
"""

from __future__ import annotations

from typing import Any

from config.settings import Settings
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import Layer, LayerSignal, SecurityReport, SignalStatus, TokenSnapshot, clamp, get_chain_profile
from scanners.base import BaseScanner, ScanContext
from scanners.crypto.providers.security import SecurityScanner
from scanners.shared.http_client import request_json, to_float

log = get_logger(__name__)


HONEYPOT_CHECK_PATH = "/v2/IsHoneypot"


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

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._goplus = SecurityScanner(settings)

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        report = await self._goplus_report(token, ctx)
        honeypot_info: dict[str, Any] = {}
        profile = get_chain_profile(token.chain)
        if profile.evm and profile.goplus_id is not None:
            try:
                payload = await request_json(
                    ctx.session,
                    f"{self.settings.providers.honeypot_base_url.rstrip('/')}{HONEYPOT_CHECK_PATH}",
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
            return await self._goplus.scan(token, ctx)
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
    "HONEYPOT_CHECK_PATH",
    "build_scanner",
    "merge_honeypot_report",
    "parse_honeypot_check",
]
