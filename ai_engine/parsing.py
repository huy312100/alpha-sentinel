"""Defensive parsing of LLM output.

Reasoning models are *mostly* well-behaved, but they do occasionally wrap JSON in
markdown fences, append a trailing sentence or emit ``"veto": "true"``. A trading
system must never crash - or silently accept - any of that, so every field is
coerced explicitly and every fallback is logged.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from core.logging import get_logger
from core.models import AlphaAssessment, RiskAudit, RiskFinding, Severity, clamp
from core.scoring import severity_for_score

log = get_logger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_TRUE = {"true", "yes", "y", "1", "veto", "block"}
_FALSE = {"false", "no", "n", "0", ""}


class ResponseParseError(ValueError):
    """Raised when no JSON object can be recovered from a model response."""


def extract_json(text: str) -> dict[str, Any]:  # noqa: C901 - a JSON scavenger is branchy by nature
    """Recover the first JSON object from a model response."""
    if not text or not text.strip():
        raise ResponseParseError("empty response")

    candidates: list[str] = []
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text.strip())

    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : index + 1])
                    break

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
    raise ResponseParseError(f"no JSON object found in response: {text[:300]!r}")


def to_float(value: Any, default: float = 0.0) -> float:
    """Coerce a number, rejecting ``NaN``/``Infinity`` instead of passing them on.

    ``json.loads`` accepts the literals ``NaN`` and ``Infinity`` and ``float()``
    accepts ``"nan"``/``"inf"``. Those values poison every score they touch
    (``clamp(nan)`` returns the ceiling), so they are treated as missing.
    """
    if isinstance(value, bool):
        return default
    number: float | None = None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        cleaned = value.strip().replace("%", "").replace(",", "")
        cleaned = re.sub(r"^(score|risk|alpha)\s*[:=]\s*", "", cleaned, flags=re.IGNORECASE)
        try:
            number = float(cleaned)
        except ValueError:
            return default
    if number is None or not math.isfinite(number):
        return default
    return number


def normalise_score(value: Any, default: float = 50.0) -> float:
    """Coerce a score on the prompt contract's 0-100 scale.

    A fractional value in ``(0, 1)`` is read as a percentage (0.82 -> 82), because
    several models drift there. Small integers are *not* rescaled: the contract is
    0-100, so ``8`` is a broken setup, not a top score.
    """
    score = to_float(value, default)
    if 0.0 < score < 1.0:
        score *= 100.0
    return round(clamp(score), 2)


def to_confidence(value: Any, default: float = 0.5) -> float:
    confidence = to_float(value, default)
    if confidence > 1.0:
        confidence /= 100.0
    return round(clamp(confidence, 0.0, 1.0), 3)


def to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    return default


def to_str_list(value: Any, limit: int = 12) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [part.strip() for part in re.split(r"[\n;]+", value)]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value]
    else:
        items = [str(value).strip()]
    return [item for item in items if item][:limit]


def to_str_map(value: Any, limit: int = 12) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item)[:500] for key, item in list(value.items())[:limit]}


def parse_alpha(
    payload: dict[str, Any],
    *,
    model: str,
    usage: dict[str, int] | None = None,
    latency_ms: float = 0.0,
) -> AlphaAssessment:
    """Coerce Agent 1 (V3) output. ``alpha_score`` is the only required field."""
    return AlphaAssessment(
        alpha_score=normalise_score(payload.get("alpha_score", payload.get("score", 50.0))),
        confidence=to_confidence(payload.get("confidence", 0.5)),
        thesis=str(payload.get("thesis") or payload.get("summary") or "")[:4000],
        momentum_horizon=str(payload.get("momentum_horizon") or payload.get("horizon") or "swing")[:64],
        catalysts=to_str_list(payload.get("catalysts")),
        layer_highlights=to_str_map(payload.get("layer_highlights")),
        red_flags=to_str_list(payload.get("red_flags")),
        model=model,
        usage=usage or {},
        latency_ms=round(latency_ms, 1),
        raw=payload,
    )


def parse_findings(value: Any, *, fallback_score: float) -> list[RiskFinding]:
    findings: list[RiskFinding] = []
    if not isinstance(value, (list, tuple)):
        return findings
    for item in value[:20]:
        if isinstance(item, str):
            findings.append(
                RiskFinding(
                    category="unclassified",
                    severity=severity_for_score(fallback_score),
                    description=item[:2000],
                )
            )
            continue
        if not isinstance(item, dict):
            continue
        findings.append(
            RiskFinding(
                category=str(item.get("category") or item.get("type") or "unclassified")[:64],
                severity=Severity.coerce(item.get("severity", "medium")),
                description=str(item.get("description") or item.get("detail") or "")[:2000],
                evidence=str(item.get("evidence") or "")[:2000],
                score_impact=round(to_float(item.get("score_impact"), 0.0), 2),
            )
        )
    return findings


def require_risk_payload(payload: dict[str, Any]) -> None:
    """Validator used by the dual-agent flow: reject a payload without a risk score.

    A missing or non-numeric ``risk_score`` used to become ``50``/no-veto, which
    silently passed a token through the auditor. Raising here triggers the repair
    path, and the caller then fails closed if the repair is unusable too.
    """
    raw = payload.get("risk_score", payload.get("risk"))
    if not math.isfinite(to_float(raw, default=math.nan)):
        raise ResponseParseError("risk payload has no usable risk_score")


def parse_risk(
    payload: dict[str, Any],
    *,
    model: str,
    usage: dict[str, int] | None = None,
    latency_ms: float = 0.0,
) -> RiskAudit:
    """Coerce Agent 2 (R1) output into a :class:`RiskAudit`."""
    require_risk_payload(payload)
    risk_score = normalise_score(payload.get("risk_score", payload.get("risk")))
    findings = parse_findings(payload.get("findings") or payload.get("risks"), fallback_score=risk_score)
    severity = Severity.coerce(payload.get("severity", severity_for_score(risk_score).value))
    veto = to_bool(payload.get("veto"), default=False)
    if not veto and (severity is Severity.CRITICAL or any(f.severity is Severity.CRITICAL for f in findings)):
        veto = True
    audit = RiskAudit(
        risk_score=risk_score,
        veto=veto,
        severity=severity,
        findings=findings,
        invalidation_triggers=to_str_list(payload.get("invalidation_triggers")),
        reasoning_summary=str(payload.get("reasoning_summary") or payload.get("reasoning") or "")[:4000],
        confidence=to_confidence(payload.get("confidence", 0.5)),
        model=model,
        usage=usage or {},
        latency_ms=round(latency_ms, 1),
        raw=payload,
    )
    if audit.effective_veto() and not veto:
        log.warning("risk_veto_inferred", extra={"risk_score": risk_score, "severity": severity.value})
    return audit


__all__ = [
    "ResponseParseError",
    "extract_json",
    "normalise_score",
    "parse_alpha",
    "parse_findings",
    "parse_risk",
    "require_risk_payload",
    "to_bool",
    "to_confidence",
    "to_float",
    "to_str_list",
    "to_str_map",
]
