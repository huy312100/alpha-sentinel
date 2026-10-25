"""Provider-neutral settings: presets, ``LLM_*`` parsing and legacy aliases."""

from __future__ import annotations

import pytest

from config.settings import (
    DEFAULT_LLM_PROVIDER,
    LLM_PROVIDER_PRESETS,
    ConfigError,
    Settings,
    iter_secret_values,
)


def load(**env: str) -> Settings:
    return Settings.load(env, load_dotenv_file=False)


def test_defaults_to_deepseek():
    settings = load()
    assert settings.llm.provider == DEFAULT_LLM_PROVIDER == "deepseek"
    assert settings.llm.base_url == "https://api.deepseek.com"
    assert settings.llm.alpha_model == "deepseek-chat"
    assert settings.llm.risk_model == "deepseek-reasoner"
    assert not settings.llm.api_key.is_configured()


@pytest.mark.parametrize("provider", sorted(LLM_PROVIDER_PRESETS))
def test_every_preset_resolves(provider):
    env = {"LLM_PROVIDER": provider}
    if provider == "custom":
        env.update({"LLM_BASE_URL": "http://localhost:8000/v1", "ALPHA_MODEL": "a", "RISK_MODEL": "b"})
    settings = load(**env)
    assert settings.llm.provider == provider
    assert settings.llm.base_url
    assert settings.llm.alpha_model and settings.llm.risk_model


def test_provider_switch_changes_base_url_and_models():
    settings = load(LLM_PROVIDER="openai", LLM_API_KEY="sk-live")
    assert settings.llm.base_url == "https://api.openai.com/v1"
    assert settings.llm.alpha_model == "gpt-4o-mini"
    assert settings.llm.risk_model == "gpt-4o"
    assert settings.llm.api_key.is_configured()


def test_explicit_values_override_the_preset():
    settings = load(
        LLM_PROVIDER="anthropic",
        LLM_BASE_URL="https://gateway.internal/v1/",
        ALPHA_MODEL="claude-fast",
        RISK_MODEL="claude-deep",
        LLM_INPUT_USD_PER_MTOK="1.5",
        LLM_OUTPUT_USD_PER_MTOK="7.5",
        LLM_TIMEOUT_SECONDS="90",
        LLM_MAX_CONCURRENCY="2",
    )
    assert settings.llm.base_url == "https://gateway.internal/v1"  # trailing slash trimmed
    assert (settings.llm.alpha_model, settings.llm.risk_model) == ("claude-fast", "claude-deep")
    assert settings.llm.input_usd_per_mtok == 1.5
    assert settings.llm.output_usd_per_mtok == 7.5
    assert settings.llm.timeout_seconds == 90.0
    assert settings.llm.max_concurrency == 2


def test_legacy_deepseek_env_still_boots():
    settings = load(
        DEEPSEEK_API_KEY="sk-legacy",
        DEEPSEEK_BASE_URL="https://proxy.example/v1",
        DEEPSEEK_TIMEOUT_SECONDS="60",
        DEEPSEEK_MAX_RETRIES="1",
        DEEPSEEK_INPUT_USD_PER_MTOK="0.5",
    )
    assert settings.llm.api_key.is_configured()
    assert settings.llm.base_url == "https://proxy.example/v1"
    assert settings.llm.timeout_seconds == 60.0
    assert settings.llm.max_retries == 1
    assert settings.llm.input_usd_per_mtok == 0.5


def test_new_names_win_over_legacy():
    settings = load(LLM_API_KEY="sk-new", DEEPSEEK_API_KEY="sk-old", LLM_TIMEOUT_SECONDS="30")
    assert settings.llm.api_key.get_secret_value() == "sk-new"
    assert settings.llm.timeout_seconds == 30.0


def test_legacy_base_url_is_ignored_for_other_providers():
    settings = load(LLM_PROVIDER="openai", DEEPSEEK_BASE_URL="https://api.deepseek.com")
    assert settings.llm.base_url == "https://api.openai.com/v1"


def test_legacy_key_and_prices_never_leak_to_another_provider():
    settings = load(
        LLM_PROVIDER="openai",
        DEEPSEEK_API_KEY="sk-legacy-deepseek",
        DEEPSEEK_INPUT_USD_PER_MTOK="0.5",
        DEEPSEEK_OUTPUT_USD_PER_MTOK="2.0",
    )
    assert settings.llm.api_key.is_configured() is False
    assert settings.llm.input_usd_per_mtok == LLM_PROVIDER_PRESETS["openai"].input_usd_per_mtok
    assert settings.llm.output_usd_per_mtok == LLM_PROVIDER_PRESETS["openai"].output_usd_per_mtok


def test_unknown_provider_is_rejected():
    with pytest.raises(ConfigError, match="unknown LLM_PROVIDER"):
        load(LLM_PROVIDER="mistral")


def test_custom_provider_needs_a_base_url():
    with pytest.raises(ConfigError, match="LLM_BASE_URL"):
        load(LLM_PROVIDER="custom", ALPHA_MODEL="a", RISK_MODEL="b")


def test_custom_provider_needs_both_models():
    with pytest.raises(ConfigError, match="RISK_MODEL"):
        load(LLM_PROVIDER="custom", LLM_BASE_URL="http://localhost:8000/v1", ALPHA_MODEL="a")


def test_placeholder_key_is_not_a_credential():
    settings = load(LLM_API_KEY="sk-your-key")
    assert not settings.llm.api_key.is_configured()
    assert "LLM_API_KEY" in settings.missing_critical()


def test_demo_mode_needs_no_key():
    settings = load(DEMO_MODE="true")
    assert settings.missing_critical() == []
    assert settings.validate() == []


def test_bad_numeric_llm_setting_fails_fast():
    with pytest.raises(ConfigError, match="LLM_MAX_CONCURRENCY"):
        load(LLM_MAX_CONCURRENCY="lots")


@pytest.mark.parametrize("key", ["LLM_TIMEOUT_SECONDS", "MAX_RISK_SCORE", "LAYER_TIMEOUT_SECONDS"])
def test_non_finite_numbers_are_rejected(key):
    with pytest.raises(ConfigError, match="finite"):
        load(**{key: "nan"})
    with pytest.raises(ConfigError, match="finite"):
        load(**{key: "inf"})


def test_unknown_enabled_layer_is_a_boot_problem():
    settings = load(ENABLED_LAYERS="onchain,technical,dex")
    problems = settings.validate()
    assert any("ENABLED_LAYERS" in problem and "technical" in problem for problem in problems)


def test_env_example_does_not_override_presets():
    """Copying the template must not send model ids like ``model`` to a provider."""
    from config.settings import PROJECT_ROOT

    active = [
        line.strip()
        for line in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(("ALPHA_MODEL=", "RISK_MODEL=", "LLM_INPUT_USD_PER_MTOK=", "LLM_OUTPUT_USD_PER_MTOK="))
    ]
    assert active == []


def test_redacted_summary_never_leaks_the_key():
    settings = load(LLM_API_KEY="sk-super-secret", LLM_PROVIDER="openai")
    summary = settings.redacted_summary()
    assert summary["llm_provider"] == "openai"
    assert summary["llm_configured"] is True
    assert "sk-super-secret" not in str(summary)
    assert "sk-super-secret" in list(iter_secret_values(settings))
