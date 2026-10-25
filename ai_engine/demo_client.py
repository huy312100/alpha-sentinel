"""Scripted agents for ``DEMO_MODE`` and for tests.

Demo mode exists so the whole pipeline (scanners -> agents -> storage -> bot) can
be exercised end-to-end with **zero API keys**. The demo agents are deterministic:
the same snapshot always yields the same verdict, which is exactly what tests and
screen recordings need.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from ai_engine.llm_client import ChatResult
from ai_engine.parsing import ResponseParseError, extract_json

_PRIOR_RE = re.compile(r"DETERMINISTIC LAYER PRIOR:\s*([0-9.]+)")


class DemoLLMClient:
    """Drop-in stand-in for :class:`LLMClient` with deterministic answers."""

    def __init__(self, latency_ms: float = 12.0) -> None:
        self._latency_ms = latency_ms
        self._requests = 0

    async def __aenter__(self) -> "DemoLLMClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:  # pragma: no cover - nothing to release
        return None

    @property
    def stats(self) -> dict[str, float | int]:
        return {
            "requests": self._requests,
            "failures": 0,
            "retries": 0,
            "prompt_tokens": self._requests * 900,
            "completion_tokens": self._requests * 260,
            "cost_usd": 0.0,
            "avg_latency_ms": self._latency_ms,
        }

    async def chat_json(
        self,
        *,
        model: str | None = None,
        system: str,
        user: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        json_mode: bool = True,
    ) -> ChatResult:
        self._requests += 1
        await asyncio.sleep(self._latency_ms / 1000.0)
        payload = self._snapshot_from_prompt(user)
        prior = self._prior_from_prompt(user)
        answer = self._risk_payload(payload, prior) if "SENTINEL-RISK" in system else self._alpha_payload(payload, prior)
        return ChatResult(
            content=json.dumps(answer, ensure_ascii=False),
            model=model or "demo-agent",
            usage={"prompt_tokens": 900, "completion_tokens": 260, "total_tokens": 1160},
            latency_ms=self._latency_ms,
        )

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _snapshot_from_prompt(user: str) -> dict[str, Any]:
        try:
            start = user.index("{")
            end = user.index("\n\n", start)
            return extract_json(user[start:end])
        except (ValueError, ResponseParseError):
            return {}

    @staticmethod
    def _prior_from_prompt(user: str) -> float:
        match = _PRIOR_RE.search(user)
        return float(match.group(1)) if match else 50.0

    @staticmethod
    def _seed(payload: dict[str, Any]) -> int:
        instrument = payload.get("instrument") or {}
        key = f"{instrument.get('venue', '')}:{instrument.get('address', '')}:{instrument.get('symbol', '')}"
        return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)

    def _alpha_payload(self, payload: dict[str, Any], prior: float) -> dict[str, Any]:
        seed = self._seed(payload)
        drift = (seed % 21) - 10  # -10..+10 deterministic jitter
        alpha = max(5.0, min(96.0, prior + drift))
        instrument = payload.get("instrument") or {}
        symbol = instrument.get("symbol") or "TOKEN"
        return {
            "alpha_score": round(alpha, 1),
            "confidence": 0.62 if prior == 50.0 else 0.78,
            "thesis": (
                f"{symbol} shows a deterministic demo edge: layer prior {prior:.1f} with "
                f"{'aligned flows' if alpha >= 60 else 'mixed flows'} across on-chain and DEX data. "
                "Demo mode substitutes scripted agents for the live LLM, so treat this as a wiring test."
            ),
            "momentum_horizon": "swing",
            "catalysts": ["demo-scripted catalyst"],
            "layer_highlights": {"dex": "volume/liquidity ratio carried by demo payload"},
            "red_flags": ["scripted output - not investment research"] if alpha < 70 else [],
        }

    def _risk_payload(self, payload: dict[str, Any], prior: float) -> dict[str, Any]:
        seed = self._seed(payload)
        security = payload.get("security") or {}
        honeypot = bool(security.get("honeypot"))
        risk = 88.0 if honeypot else max(8.0, min(72.0, 100.0 - prior + (seed % 15)))
        findings: list[dict[str, Any]] = []
        if honeypot:
            findings.append(
                {
                    "category": "tax",
                    "severity": "critical",
                    "description": "Sell transfers are blocked by the token contract.",
                    "evidence": "goplus.is_honeypot=true",
                    "score_impact": 95,
                }
            )
        elif risk >= 55:
            findings.append(
                {
                    "category": "liquidity",
                    "severity": "high" if risk >= 70 else "medium",
                    "description": "Exit liquidity is thin relative to measured inflow.",
                    "evidence": f"demo risk score {risk:.0f}",
                    "score_impact": round(risk, 1),
                }
            )
        severity = "critical" if honeypot else "high" if risk >= 70 else "medium" if risk >= 35 else "low"
        return {
            "risk_score": round(risk, 1),
            "veto": honeypot,
            "severity": severity,
            "findings": findings,
            "invalidation_triggers": ["liquidity drops below 50k", "top-10 holders above 65%"],
            "reasoning_summary": (
                "Demo auditor: honeypot flag detected in the snapshot."
                if honeypot
                else f"Demo auditor: no hard security flag; residual risk scored {risk:.0f}/100."
            ),
            "confidence": 0.7,
        }


__all__ = ["DemoLLMClient"]
