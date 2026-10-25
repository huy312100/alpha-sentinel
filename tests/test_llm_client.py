"""Cross-provider behaviour of the LLM client (no sockets, no keys)."""

from __future__ import annotations

from typing import Any

import pytest

from ai_engine import llm_client as llm_module
from ai_engine.llm_client import LLMClient, LLMError
from config.settings import LLM_PROVIDER_PRESETS, ConfigError, LLMSettings, SecretStr
from core.http import HttpError


class FakeSession:
    """Stands in for ``aiohttp.ClientSession`` so nothing touches the network."""

    closed = False


def make_settings(**overrides: Any) -> LLMSettings:
    base = {
        "provider": "openai",
        "api_key": SecretStr("sk-test"),
        "base_url": "https://api.openai.com/v1",
        "alpha_model": "alpha-x",
        "risk_model": "risk-y",
        "input_usd_per_mtok": 1.0,
        "output_usd_per_mtok": 2.0,
    }
    base.update(overrides)
    return LLMSettings(**base)


def ok_body(model: str = "alpha-x") -> dict[str, Any]:
    return {
        "model": model,
        "choices": [
            {
                "message": {"content": '{"alpha_score": 71}', "reasoning_content": "thought"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 500_000, "total_tokens": 1_500_000},
    }


def install(monkeypatch, outcomes: list[Any]) -> list[dict[str, Any]]:
    """Queue bodies/exceptions and record every call made to ``request_json``."""
    calls: list[dict[str, Any]] = []

    async def fake_request_json(session, url, *, method="GET", json_body=None, headers=None, **kwargs):
        calls.append(
            {"url": url, "payload": json_body, "headers": headers, "method": method, "kwargs": kwargs}
        )
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(llm_module, "request_json", fake_request_json)
    return calls


async def test_posts_to_the_configured_provider(monkeypatch):
    calls = install(monkeypatch, [ok_body()])
    client = LLMClient(make_settings(), session=FakeSession())

    result = await client.chat_json(system="s", user="u", temperature=0.2, max_tokens=128)

    assert calls[0]["url"] == "https://api.openai.com/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert calls[0]["payload"]["model"] == "alpha-x"
    assert calls[0]["payload"]["max_tokens"] == 128
    assert calls[0]["payload"]["response_format"] == {"type": "json_object"}
    assert client.provider == "openai"
    assert result.content == '{"alpha_score": 71}'
    assert result.reasoning_content == "thought"
    assert result.total_tokens == 1_500_000
    # 1M input @1.0 + 0.5M output @2.0
    assert client.stats["cost_usd"] == pytest.approx(2.0)
    assert client.stats["requests"] == 1


async def test_risk_model_gets_a_trace_header(monkeypatch):
    calls = install(monkeypatch, [ok_body("risk-y")])
    client = LLMClient(make_settings(), session=FakeSession())
    await client.chat_json(model="risk-y", system="s", user="u")
    assert calls[0]["headers"]["X-Trace-Agent"] == "risk-auditor"


async def test_json_mode_rejection_is_retried_without_response_format(monkeypatch):
    calls = install(
        monkeypatch,
        [HttpError("response_format is not supported", status=400), ok_body()],
    )
    client = LLMClient(make_settings(), session=FakeSession())

    result = await client.chat_json(system="s", user="u")

    assert result.content == '{"alpha_score": 71}'
    assert "response_format" in calls[0]["payload"]
    assert "response_format" not in calls[1]["payload"]
    assert client.stats["retries"] == 1


async def test_max_tokens_is_renamed_for_openai_reasoning_models(monkeypatch):
    calls = install(
        monkeypatch,
        [
            HttpError(
                "Unsupported parameter: 'max_tokens' is not supported, use 'max_completion_tokens'",
                status=400,
            ),
            ok_body(),
        ],
    )
    client = LLMClient(make_settings(), session=FakeSession())

    await client.chat_json(system="s", user="u", max_tokens=99)

    assert calls[1]["payload"]["max_completion_tokens"] == 99
    assert "max_tokens" not in calls[1]["payload"]


async def test_temperature_is_dropped_when_pinned(monkeypatch):
    calls = install(
        monkeypatch,
        [HttpError("Unsupported value: 'temperature' does not support 0.2", status=400), ok_body()],
    )
    client = LLMClient(make_settings(), session=FakeSession())

    await client.chat_json(system="s", user="u", temperature=0.2)

    assert "temperature" in calls[0]["payload"]
    assert "temperature" not in calls[1]["payload"]


async def test_auth_errors_are_not_retried(monkeypatch):
    calls = install(monkeypatch, [HttpError("invalid api key", status=401)])
    client = LLMClient(make_settings(), session=FakeSession())

    with pytest.raises(LLMError, match="llm request failed"):
        await client.chat_json(system="s", user="u")

    assert len(calls) == 1
    assert client.stats["failures"] == 1


async def test_empty_choices_raise(monkeypatch):
    install(monkeypatch, [{"choices": []}])
    client = LLMClient(make_settings(), session=FakeSession())
    with pytest.raises(LLMError, match="no choices"):
        await client.chat_json(system="s", user="u")


async def test_blank_content_raises(monkeypatch):
    install(monkeypatch, [{"choices": [{"message": {"content": "  "}, "finish_reason": "length"}]}])
    client = LLMClient(make_settings(), session=FakeSession())
    with pytest.raises(LLMError, match="empty content"):
        await client.chat_json(system="s", user="u")


def test_missing_key_is_caught_at_construction():
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        LLMClient(make_settings(api_key=SecretStr("")))


def test_require_key_false_allows_anonymous_gateways():
    client = LLMClient(make_settings(api_key=SecretStr(""), provider="custom"), require_key=False)
    assert client.provider == "custom"


async def test_the_last_adaptation_is_actually_sent(monkeypatch):
    calls = install(
        monkeypatch,
        [
            HttpError("response_format is not supported", status=400),
            HttpError("Unsupported parameter: max_tokens", status=400),
            HttpError("Unsupported value: temperature does not support 0.2", status=400),
            ok_body(),
        ],
    )
    client = LLMClient(make_settings(), session=FakeSession())

    result = await client.chat_json(system="s", user="u")

    assert result.content == '{"alpha_score": 71}'
    assert len(calls) == 4
    final = calls[3]["payload"]
    assert "response_format" not in final
    assert "max_tokens" not in final
    assert "temperature" not in final
    assert client.stats["retries"] == 3


async def test_completions_are_marked_idempotent_for_retries(monkeypatch):
    calls = install(monkeypatch, [ok_body()])
    client = LLMClient(make_settings(), session=FakeSession())
    await client.chat_json(system="s", user="u")
    assert calls[0]["kwargs"]["idempotent"] is True


async def test_list_content_parts_are_joined(monkeypatch):
    body = ok_body()
    body["choices"][0]["message"]["content"] = [
        {"type": "text", "text": '{"alpha_score": '},
        {"type": "text", "text": "71}"},
    ]
    install(monkeypatch, [body])
    client = LLMClient(make_settings(), session=FakeSession())

    result = await client.chat_json(system="s", user="u")

    assert result.content == '{"alpha_score": 71}'


async def test_non_json_body_counts_as_a_provider_failure(monkeypatch):
    install(monkeypatch, [ValueError("Expecting value: line 1 column 1 (char 0)")])
    client = LLMClient(make_settings(), session=FakeSession())

    with pytest.raises(LLMError, match="transport error"):
        await client.chat_json(system="s", user="u")

    assert client.stats["failures"] == 1


async def test_a_non_dict_choice_is_a_clean_failure(monkeypatch):
    install(monkeypatch, [{"choices": ["not-an-object"]}])
    client = LLMClient(make_settings(), session=FakeSession())

    with pytest.raises(LLMError, match="empty content"):
        await client.chat_json(system="s", user="u")

    assert client.stats["failures"] == 1


@pytest.mark.parametrize("provider", ["deepseek", "openai", "anthropic", "openrouter"])
def test_presets_have_usable_models(provider):
    preset = LLM_PROVIDER_PRESETS[provider]
    assert preset.alpha_model and preset.risk_model
    assert preset.base_url.startswith("https://")
