"""Typed domain objects shared by scanners, the AI engine, storage and UI.

Everything here is a plain dataclass (no ORM, no pydantic) so it can cross
process/JSON/Parquet boundaries cheaply. ``slots=True`` keeps millions of
observations memory-tight.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def json_safe(value: Any) -> Any:
    """Recursively convert enums/datetimes/dataclasses into JSON-safe values."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    if hasattr(value, "to_dict"):
        return json_safe(value.to_dict())
    return value


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class Layer(str, Enum):
    """Every collection layer plus the deterministic security screen.

    The stack is asset-class agnostic: each scanner declares which markets it can
    describe (see :attr:`scanners.base.BaseScanner.asset_classes`), and a layer
    that does not apply returns ``SKIPPED`` instead of a fake neutral reading.
    ``DEX`` is crypto microstructure, ``MARKET`` is venue microstructure for
    everything else, and ``FUNDAMENTALS`` covers issuer financials.
    """

    ONCHAIN = "onchain"
    DERIVATIVES = "derivatives"
    SOCIAL = "social"
    DEX = "dex"
    #: Legacy slot kept so historical rows stay readable; new crypto scans use
    #: :attr:`ECOSYSTEM` (DeFiLlama / GrowThePie) instead.
    AIRDROP = "airdrop"
    ECOSYSTEM = "ecosystem"
    TOKENOMICS = "tokenomics"
    MARKET = "market"
    FUNDAMENTALS = "fundamentals"
    MACRO = "macro"
    TECHNICALS = "technicals"
    SECURITY = "security"

    @classmethod
    def collection_layers(cls) -> tuple["Layer", ...]:
        return (
            cls.ONCHAIN,
            cls.DERIVATIVES,
            cls.SOCIAL,
            cls.DEX,
            cls.ECOSYSTEM,
            cls.TOKENOMICS,
            cls.MARKET,
            cls.FUNDAMENTALS,
            cls.MACRO,
            cls.TECHNICALS,
        )


class SignalStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"         # partial data, still usable
    UNAVAILABLE = "unavailable"   # provider failed -> excluded from scoring
    SKIPPED = "skipped"          # not applicable / not configured


class VerdictStatus(str, Enum):
    APPROVED = "approved"
    WATCH = "watch"
    REJECTED = "rejected"
    VETOED = "vetoed"

    @property
    def is_actionable(self) -> bool:
        return self is VerdictStatus.APPROVED


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return {"low": 1, "medium": 2, "high": 3, "critical": 4}[self.value]

    @classmethod
    def coerce(cls, value: Any) -> "Severity":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.MEDIUM


# ---------------------------------------------------------------------------
# Chain metadata
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ChainProfile:
    name: str
    goplus_id: str | None          # GoPlus EVM chain id (None -> unsupported)
    geckoterminal: str             # GeckoTerminal network slug
    blockscout_url: str | None
    evm: bool = True


CHAIN_PROFILES: dict[str, ChainProfile] = {
    "ethereum": ChainProfile("ethereum", "1", "eth", "https://eth.blockscout.com"),
    "bsc": ChainProfile("bsc", "56", "bsc", "https://bsc.blockscout.com"),
    "polygon": ChainProfile("polygon", "137", "polygon_pos", "https://polygon.blockscout.com"),
    "arbitrum": ChainProfile("arbitrum", "42161", "arbitrum", "https://arbitrum.blockscout.com"),
    "base": ChainProfile("base", "8453", "base", "https://base.blockscout.com"),
    "solana": ChainProfile("solana", None, "solana", None, evm=False),
}


def get_chain_profile(chain: str) -> ChainProfile:
    try:
        return CHAIN_PROFILES[chain.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unsupported chain: {chain!r} (known: {sorted(CHAIN_PROFILES)})") from exc


# ---------------------------------------------------------------------------
# Layer signals & security
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class LayerSignal:
    """One layer's contribution. ``score`` is always a 0-100 bullishness value."""

    layer: Layer
    score: float = 50.0
    confidence: float = 0.0
    weight: float = 1.0
    status: SignalStatus = SignalStatus.OK
    summary: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    error: str | None = None
    observed_at: datetime = field(default_factory=utcnow)

    @property
    def contributes(self) -> bool:
        return self.status in {SignalStatus.OK, SignalStatus.DEGRADED} and self.weight > 0

    @property
    def effective_weight(self) -> float:
        return self.weight * self.confidence if self.contributes else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "score": round(clamp(self.score), 2),
            "confidence": round(clamp(self.confidence, 0.0, 1.0), 3),
            "weight": self.weight,
            "status": self.status.value,
            "summary": self.summary,
            "metrics": json_safe(self.metrics),
            "flags": list(self.flags),
            "latency_ms": self.latency_ms,
            "error": self.error,
            "observed_at": iso(self.observed_at),
        }


@dataclass(slots=True)
class SecurityReport:
    """GoPlus (or equivalent) token contract security assessment."""

    available: bool = False
    honeypot: bool = False
    buy_tax_pct: float = 0.0
    sell_tax_pct: float = 0.0
    mintable: bool = False
    owner_privileges: bool = False
    hidden_owner: bool = False
    is_proxy: bool = False
    can_take_back_ownership: bool = False
    blacklist_enabled: bool = False
    transfer_pausable: bool = False
    holder_count: int | None = None
    top10_holder_pct: float | None = None
    lp_locked_pct: float | None = None
    flags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=utcnow)

    CRITICAL_FLAGS = frozenset(
        {
            "honeypot",
            "cannot_sell",
            "hidden_owner",
            "can_take_back_ownership",
            "owner_change_balance",
            "selfdestruct",
            "sell_tax_gt_10",
            "blacklist_enabled",
        }
    )
    HIGH_FLAGS = frozenset(
        {
            "mintable",
            "owner_privileges",
            "transfer_pausable",
            "buy_tax_gt_10",
            "top10_holders_gt_80",
            "lp_unlocked",
            "proxy_contract",
        }
    )

    def critical_flags(self) -> list[str]:
        return [flag for flag in self.flags if flag in self.CRITICAL_FLAGS]

    def high_flags(self) -> list[str]:
        return [flag for flag in self.flags if flag in self.HIGH_FLAGS]

    def risk_points(self) -> float:
        """Deterministic 0-100 danger score, blended into the risk prior."""
        if not self.available:
            return 0.0
        points = 0.0
        points += 60.0 if self.honeypot else 0.0
        points += min(25.0, max(self.buy_tax_pct, self.sell_tax_pct) * 2.0)
        points += 10.0 if self.hidden_owner else 0.0
        points += 10.0 if self.can_take_back_ownership else 0.0
        points += 8.0 if self.mintable else 0.0
        points += 8.0 if self.blacklist_enabled else 0.0
        points += 6.0 if self.owner_privileges else 0.0
        points += 5.0 if self.transfer_pausable else 0.0
        points += 4.0 if self.is_proxy else 0.0
        if self.top10_holder_pct is not None and self.top10_holder_pct > 60:
            points += min(15.0, (self.top10_holder_pct - 60) * 0.4)
        if self.lp_locked_pct is not None and self.lp_locked_pct < 50:
            points += 10.0
        return clamp(points)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "honeypot": self.honeypot,
            "buy_tax_pct": round(self.buy_tax_pct, 3),
            "sell_tax_pct": round(self.sell_tax_pct, 3),
            "mintable": self.mintable,
            "owner_privileges": self.owner_privileges,
            "hidden_owner": self.hidden_owner,
            "is_proxy": self.is_proxy,
            "holder_count": self.holder_count,
            "top10_holder_pct": self.top10_holder_pct,
            "lp_locked_pct": self.lp_locked_pct,
            "flags": list(self.flags),
            "risk_points": round(self.risk_points(), 2),
            "checked_at": iso(self.checked_at),
        }


@dataclass(slots=True)
class WashTradingReport:
    """Output of the wash-trading filter (``scanners/filters/wash_trading.py``)."""

    suspicion_score: float = 0.0
    trade_count: int = 0
    unique_wallets: int = 0
    avg_trade_usd: float = 0.0
    median_trade_usd: float = 0.0
    unique_wallet_ratio: float = 0.0
    round_amount_ratio: float = 0.0
    self_trade_ratio: float = 0.0
    buy_sell_imbalance: float = 0.0
    flags: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    evaluated: bool = False

    @property
    def severity(self) -> Severity:
        if self.suspicion_score >= 70:
            return Severity.CRITICAL
        if self.suspicion_score >= 50:
            return Severity.HIGH
        if self.suspicion_score >= 30:
            return Severity.MEDIUM
        return Severity.LOW

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "evaluated": self.evaluated,
            "suspicion_score": round(self.suspicion_score, 2),
            "severity": self.severity.value,
            "trade_count": self.trade_count,
            "unique_wallets": self.unique_wallets,
            "avg_trade_usd": round(self.avg_trade_usd, 2),
            "median_trade_usd": round(self.median_trade_usd, 2),
            "unique_wallet_ratio": round(self.unique_wallet_ratio, 4),
            "round_amount_ratio": round(self.round_amount_ratio, 4),
            "self_trade_ratio": round(self.self_trade_ratio, 4),
            "buy_sell_imbalance": round(self.buy_sell_imbalance, 4),
            "flags": list(self.flags),
        }
        payload.update(self.metrics)
        return payload


# ---------------------------------------------------------------------------
# Market snapshot
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class TokenSnapshot:
    """The cleaned, normalised input handed to the dual-agent engine.

    Despite the historical name, this describes *any* instrument: for a crypto
    token ``token_address`` is the contract and ``chain`` the network; for an
    equity/FX/commodity/rate it is the canonical symbol and the venue slug (see
    :mod:`core.instruments`). ``asset_class`` is what tells the scanners which
    layers can describe it.
    """

    token_address: str
    chain: str = "ethereum"
    asset_class: str = "crypto"
    symbol: str = ""
    name: str = ""

    chain_id: int | None = None
    pair_address: str | None = None
    price_usd: float | None = None
    market_cap_usd: float | None = None
    fdv_usd: float | None = None
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    volume_1h_usd: float | None = None
    price_change_24h_pct: float | None = None
    price_change_1h_pct: float | None = None
    holder_count: int | None = None
    age_hours: float | None = None
    security: SecurityReport = field(default_factory=SecurityReport)
    wash: WashTradingReport = field(default_factory=WashTradingReport)
    signals: list[LayerSignal] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    collected_at: datetime = field(default_factory=utcnow)

    def signal_for(self, layer: Layer) -> LayerSignal | None:
        for signal in self.signals:
            if signal.layer is layer:
                return signal
        return None

    def contributing_signals(self) -> list[LayerSignal]:
        return [signal for signal in self.signals if signal.contributes]

    def to_prompt_payload(self) -> dict[str, Any]:
        """Compact, token-efficient representation sent to the LLMs."""
        return {
            "instrument": {
                "asset_class": self.asset_class,
                "symbol": self.symbol or "UNKNOWN",
                "name": self.name,
                "venue": self.chain,
                "address": self.token_address,
                "age_hours": self.age_hours,
                "holder_count": self.holder_count,
            },
            "market": {
                "price_usd": self.price_usd,
                "market_cap_usd": self.market_cap_usd,
                "fdv_usd": self.fdv_usd,
                "liquidity_usd": self.liquidity_usd,
                "volume_24h_usd": self.volume_24h_usd,
                "volume_1h_usd": self.volume_1h_usd,
                "price_change_24h_pct": self.price_change_24h_pct,
                "price_change_1h_pct": self.price_change_1h_pct,
            },
            "security": self.security.to_dict(),
            "wash_trading": self.wash.to_dict(),
            "layers": [signal.to_dict() for signal in self.signals],
        }


# ---------------------------------------------------------------------------
# Agent outputs
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class AlphaAssessment:
    """Agent 1 (alpha analyst) output."""

    alpha_score: float = 50.0
    confidence: float = 0.0
    thesis: str = ""
    momentum_horizon: str = "swing"
    catalysts: list[str] = field(default_factory=list)
    layer_highlights: dict[str, str] = field(default_factory=dict)
    red_flags: list[str] = field(default_factory=list)
    model: str = "unknown"
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alpha_score": round(clamp(self.alpha_score), 2),
            "confidence": round(clamp(self.confidence, 0.0, 1.0), 3),
            "thesis": self.thesis,
            "momentum_horizon": self.momentum_horizon,
            "catalysts": list(self.catalysts),
            "layer_highlights": dict(self.layer_highlights),
            "red_flags": list(self.red_flags),
            "model": self.model,
            "usage": dict(self.usage),
            "latency_ms": self.latency_ms,
        }


@dataclass(slots=True)
class RiskFinding:
    category: str
    severity: Severity = Severity.MEDIUM
    description: str = ""
    evidence: str = ""
    score_impact: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "severity": self.severity.value,
            "description": self.description,
            "evidence": self.evidence,
            "score_impact": round(self.score_impact, 2),
        }


@dataclass(slots=True)
class RiskAudit:
    """Agent 2 (risk auditor) output - holds the VETO."""

    risk_score: float = 50.0
    veto: bool = False
    severity: Severity = Severity.MEDIUM
    findings: list[RiskFinding] = field(default_factory=list)
    invalidation_triggers: list[str] = field(default_factory=list)
    reasoning_summary: str = ""
    confidence: float = 0.0
    model: str = "unknown"
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def has_critical_finding(self) -> bool:
        return any(finding.severity is Severity.CRITICAL for finding in self.findings)

    def effective_veto(self) -> bool:
        """Defence in depth: a critical finding triggers the veto even when the
        model wrote ``false`` in the boolean field."""
        return self.veto or self.has_critical_finding() or self.severity is Severity.CRITICAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_score": round(clamp(self.risk_score), 2),
            "veto": self.effective_veto(),
            "severity": self.severity.value,
            "findings": [finding.to_dict() for finding in self.findings],
            "invalidation_triggers": list(self.invalidation_triggers),
            "reasoning_summary": self.reasoning_summary,
            "confidence": round(clamp(self.confidence, 0.0, 1.0), 3),
            "model": self.model,
            "usage": dict(self.usage),
            "latency_ms": self.latency_ms,
        }


@dataclass(slots=True)
class FinalVerdict:
    """The single decision record persisted to SQLite (hot) and Parquet (cold)."""

    token_address: str
    chain: str
    symbol: str
    status: VerdictStatus
    final_score: float
    prior_score: float
    blended_score: float
    alpha: AlphaAssessment
    risk: RiskAudit
    asset_class: str = "crypto"
    position_multiplier: float = 0.0
    rationale: str = ""
    decided_at: datetime = field(default_factory=utcnow)
    latency_ms: float = 0.0
    security_flags: list[str] = field(default_factory=list)

    @property
    def vetoed(self) -> bool:
        return self.status is VerdictStatus.VETOED

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_address": self.token_address,
            "chain": self.chain,
            "asset_class": self.asset_class,
            "symbol": self.symbol,
            "status": self.status.value,
            "final_score": round(self.final_score, 2),
            "prior_score": round(self.prior_score, 2),
            "blended_score": round(self.blended_score, 2),
            "position_multiplier": round(self.position_multiplier, 3),
            "rationale": self.rationale,
            "security_flags": list(self.security_flags),
            "decided_at": iso(self.decided_at),
            "latency_ms": self.latency_ms,
            "alpha": self.alpha.to_dict(),
            "risk": self.risk.to_dict(),
        }

    def to_record(self) -> dict[str, Any]:
        """Flat row: scalars stay scalar, nested structures become JSON strings."""
        findings = [finding.to_dict() for finding in self.risk.findings]
        return {
            "token_address": self.token_address,
            "chain": self.chain,
            "asset_class": self.asset_class,
            "symbol": self.symbol,
            "status": self.status.value,
            "final_score": round(self.final_score, 2),
            "prior_score": round(self.prior_score, 2),
            "blended_score": round(self.blended_score, 2),
            "alpha_score": round(self.alpha.alpha_score, 2),
            "alpha_confidence": round(self.alpha.confidence, 3),
            "alpha_thesis": self.alpha.thesis[:4000],
            "alpha_model": self.alpha.model,
            "risk_score": round(self.risk.risk_score, 2),
            "risk_veto": self.risk.effective_veto(),
            "risk_severity": self.risk.severity.value,
            "risk_summary": self.risk.reasoning_summary[:4000],
            "risk_model": self.risk.model,
            "position_multiplier": round(self.position_multiplier, 3),
            "rationale": self.rationale[:4000],
            "security_flags": ",".join(self.security_flags),
            "catalysts_json": json.dumps(self.alpha.catalysts, ensure_ascii=False),
            "findings_json": json.dumps(findings, ensure_ascii=False),
            "invalidation_json": json.dumps(self.risk.invalidation_triggers, ensure_ascii=False),
            "alpha_usage_json": json.dumps(self.alpha.usage, ensure_ascii=False),
            "risk_usage_json": json.dumps(self.risk.usage, ensure_ascii=False),
            "latency_ms": self.latency_ms,
            "decided_at": self.decided_at,
        }
