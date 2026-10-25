"""Dual-agent intelligence layer (alpha analyst + adversarial risk auditor)."""

from __future__ import annotations

from ai_engine.llm_client import ChatResult, ClientStats, LLMClient, LLMError
from ai_engine.dual_audit_flow import AUDITOR_UNAVAILABLE_RISK, AuditOutcome, DualAuditFlow
from ai_engine.parsing import ResponseParseError, extract_json, parse_alpha, parse_risk
from ai_engine.prompts import PROMPT_VERSION

__all__ = [
    "AUDITOR_UNAVAILABLE_RISK",
    "AuditOutcome",
    "ChatResult",
    "ClientStats",
    "LLMClient",
    "LLMError",
    "DualAuditFlow",
    "PROMPT_VERSION",
    "ResponseParseError",
    "extract_json",
    "parse_alpha",
    "parse_risk",
]
