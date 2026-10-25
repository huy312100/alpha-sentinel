"""Async LLM client for any OpenAI-compatible ``/chat/completions`` endpoint.

DeepSeek, OpenAI, Anthropic's compatibility layer, OpenRouter, Together, Groq,
vLLM, Ollama and LiteLLM gateways all speak the same dialect, so the provider is
just a base URL plus two model ids (see ``LLM_PROVIDER_PRESETS``).

Design notes that matter in production:

* **One client per process.** The aiohttp session is injected so the bot, the
  dashboard and the scanners share a single connection pool.
* **Bounded concurrency.** A semaphore keeps us inside the provider's rate limits
  instead of collecting 429s during a 40-token scan.
* **Payload adaptation instead of per-vendor branches.** Providers disagree on
  ``response_format``, on ``max_tokens`` vs ``max_completion_tokens``, and on
  whether a model accepts a custom ``temperature``. When the API rejects one of
  those, the request is retried once with the offending field removed/renamed and
  :mod:`ai_engine.parsing` recovers the object from prose if needed.
* **Cost accounting.** Token usage is aggregated so every run can be priced.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from config.settings import LLMSettings
from core.http import HttpError, build_session, request_json
from core.logging import get_logger

log = get_logger(__name__)

#: Maximum number of payload rewrites before we give up on a provider.
_MAX_ADAPTATIONS = 3


class LLMError(RuntimeError):
    """Any unrecoverable failure talking to the LLM endpoint."""


@dataclass(slots=True)
class ChatResult:
    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0
    reasoning_content: str = ""

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("total_tokens", 0))


@dataclass(slots=True)
class ClientStats:
    requests: int = 0
    failures: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    total_latency_ms: float = 0.0

    def as_dict(self) -> dict[str, float | int]:
        avg = self.total_latency_ms / self.requests if self.requests else 0.0
        return {
            "requests": self.requests,
            "failures": self.failures,
            "retries": self.retries,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "avg_latency_ms": round(avg, 1),
        }


class LLMClient:
    """Thin, retrying, concurrency-limited wrapper around ``/chat/completions``."""

    def __init__(
        self,
        settings: LLMSettings,
        *,
        session: aiohttp.ClientSession | None = None,
        require_key: bool = True,
    ) -> None:
        if require_key:
            settings.assert_configured()
        self._settings = settings
        self._session = session
        self._owns_session = session is None
        self._semaphore = asyncio.Semaphore(max(1, settings.max_concurrency))
        self._stats = ClientStats()

    # -- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> "LLMClient":
        await self.session()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = build_session(timeout_seconds=self._settings.timeout_seconds)
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def stats(self) -> dict[str, float | int]:
        return self._stats.as_dict()

    @property
    def provider(self) -> str:
        return self._settings.provider

    # -- core call ---------------------------------------------------------
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
        """Call the model and return its raw text (JSON parsing happens upstream)."""
        model = model or self._settings.alpha_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens or self._settings.max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        async with self._semaphore:
            body = await self._post_adapted(payload, model=model)
        latency_ms = (time.perf_counter() - started) * 1000.0
        return self._parse_body(body, model=model, latency_ms=latency_ms)

    async def _post_adapted(self, payload: dict[str, Any], *, model: str) -> dict[str, Any]:
        """POST, adapting the payload when a provider rejects an optional field.

        A rejection can remove one optional field at a time, so the loop needs one
        attempt *more* than the number of adaptations: otherwise the last adapted
        payload is built and never sent.
        """
        current = dict(payload)
        for _ in range(_MAX_ADAPTATIONS + 1):
            try:
                return await self._post(current)
            except LLMError as exc:
                adapted = _adapt_payload(exc, current)
                if adapted is None:
                    raise
                self._stats.retries += 1
                log.info(
                    "llm_payload_adapted",
                    extra={
                        "model": model,
                        "provider": self._settings.provider,
                        "dropped": sorted(set(current) - set(adapted)),
                        "added": sorted(set(adapted) - set(current)),
                    },
                )
                current = adapted
        raise LLMError(f"{self._settings.provider} rejected the payload after {_MAX_ADAPTATIONS} adaptations")

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = await self.session()
        url = f"{self._settings.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._settings.api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if payload.get("model") == self._settings.risk_model:
            headers["X-Trace-Agent"] = "risk-auditor"
        try:
            body = await request_json(
                session,
                url,
                method="POST",
                json_body=payload,
                headers=headers,
                retries=max(0, self._settings.max_retries - 1),
                backoff=2.0,
                idempotent=True,
            )
        except HttpError as exc:
            self._stats.failures += 1
            raise LLMError(f"llm request failed: {exc}") from exc
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            # ``request_json`` raises ValueError for a 200 response with a non-JSON
            # body (an HTML gateway page, for example). That is a provider failure
            # and must be counted, not leak as an unclassified exception.
            self._stats.failures += 1
            raise LLMError(f"llm transport error: {exc}") from exc
        if not isinstance(body, dict):
            self._stats.failures += 1
            raise LLMError(f"unexpected llm payload type: {type(body).__name__}")
        return body

    # -- response handling -------------------------------------------------
    def _parse_body(self, body: dict[str, Any], *, model: str, latency_ms: float) -> ChatResult:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            self._stats.failures += 1
            raise LLMError(f"llm returned no choices: {str(body)[:300]}")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content")
        # Some OpenAI-compatible gateways return content as a list of parts.
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str) or not content.strip():
            self._stats.failures += 1
            raise LLMError(f"llm returned empty content (finish_reason={first.get('finish_reason')})")

        usage_raw = body.get("usage") or {}
        usage = {
            key: int(usage_raw.get(key) or 0)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        cost = (
            usage["prompt_tokens"] / 1_000_000 * self._settings.input_usd_per_mtok
            + usage["completion_tokens"] / 1_000_000 * self._settings.output_usd_per_mtok
        )
        self._stats.requests += 1
        self._stats.prompt_tokens += usage["prompt_tokens"]
        self._stats.completion_tokens += usage["completion_tokens"]
        self._stats.cost_usd += cost
        self._stats.total_latency_ms += latency_ms

        log.debug(
            "llm_response",
            extra={
                "provider": self._settings.provider,
                "model": model,
                "latency_ms": round(latency_ms, 1),
                "total_tokens": usage["total_tokens"],
                "cost_usd": round(cost, 6),
            },
        )
        return ChatResult(
            content=content,
            model=str(body.get("model") or model),
            usage=usage,
            latency_ms=round(latency_ms, 1),
            reasoning_content=str(message.get("reasoning_content") or ""),
        )


# ---------------------------------------------------------------------------
# Cross-provider compatibility
# ---------------------------------------------------------------------------
def _rejection_text(exc: LLMError) -> tuple[int | None, str]:
    cause = exc.__cause__
    return getattr(cause, "status", None), str(cause or exc).lower()


def _adapt_payload(exc: LLMError, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return a payload the provider should accept, or ``None`` if we cannot help.

    Handles the three recurring incompatibilities:

    * ``response_format`` not implemented (older reasoning models) -> drop it and
      rely on :func:`ai_engine.parsing.extract_json`.
    * ``max_tokens`` renamed to ``max_completion_tokens`` (OpenAI o-series/GPT-5).
    * ``temperature`` pinned to the default (same reasons) -> drop it.
    """
    status, text = _rejection_text(exc)
    if status not in {400, 404, 422}:
        return None
    adapted = dict(payload)
    if "response_format" in adapted and ("response_format" in text or "json_object" in text):
        adapted.pop("response_format")
    elif "max_tokens" in adapted and ("max_tokens" in text or "max_completion_tokens" in text):
        adapted["max_completion_tokens"] = adapted.pop("max_tokens")
    elif "temperature" in adapted and "temperature" in text:
        adapted.pop("temperature")
    else:
        return None
    return adapted


__all__ = ["ChatResult", "ClientStats", "LLMClient", "LLMError"]
