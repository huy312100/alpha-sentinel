"""Provider-neutral settings: presets, ``LLM_*`` parsing and legacy aliases."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from string import Formatter

import pytest

from config.endpoints import EndpointSettings, REQUIRED_ENDPOINTS
from config.settings import (
    DEFAULT_LLM_PROVIDER,
    LLM_PROVIDER_PRESETS,
    PROJECT_ROOT,
    ConfigError,
    Settings,
    iter_secret_values,
)
from core.logging import redact
from scanners import __file__ as scanners_module


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


def test_a_path_that_cannot_be_resolved_is_a_boot_problem(tmp_path):
    """A symlink loop must be a ``ConfigError``, not a traceback with exit 1.

    ``_env_path`` anchors a relative setting to the repository root, and
    ``Path.resolve()`` raises ``RuntimeError`` when the path loops through itself.
    That used to escape ``Settings.load()``, so the CLI printed a traceback and
    exited 1 - the one failure mode this gate exists to prevent.
    """
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    relative = os.path.relpath(tmp_path / "a", PROJECT_ROOT)

    with pytest.raises(ConfigError, match="DATA_DIR cannot be resolved to a path"):
        load(DATA_DIR=relative)


def test_env_example_does_not_override_presets():
    """Copying the template must not send model ids like ``model`` to a provider."""

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


# ---------------------------------------------------------------------------
# Vendor endpoints: the whole URL (host and path) is configuration
# ---------------------------------------------------------------------------
def test_endpoints_default_to_the_public_vendor_hosts():
    endpoints = load().endpoints
    assert endpoints.url("coingecko.contract", platform="ethereum", address="0xabc") == (
        "https://api.coingecko.com/api/v3/coins/ethereum/contract/0xabc"
    )
    assert endpoints.url("dexscreener.token", address="0xabc") == (
        "https://api.dexscreener.com/latest/dex/tokens/0xabc"
    )
    assert endpoints.url("binance_futures.premium_index") == "https://fapi.binance.com/fapi/v1/premiumIndex"
    assert endpoints.url("blockscout.token", chain="base", address="0xabc") == (
        "https://base.blockscout.com/api/v2/tokens/0xabc"
    )


def test_every_endpoint_is_overridable_without_touching_code():
    settings = load(
        COINGECKO_BASE_URL="https://coingecko.internal/api/v3/",
        GECKOTERMINAL_BASE_URL="https://gecko.internal/api/v2/",
        DEXSCREENER_BASE_URL="https://dex.internal/",
        YAHOO_BASE_URL="https://yahoo.internal/",
        BINANCE_FUTURES_BASE_URL="https://binance.internal/",
        BYBIT_BASE_URL="https://bybit.internal/",
        HYPERLIQUID_BASE_URL="https://hyperliquid.internal/",
        COINGLASS_BASE_URL="https://coinglass.internal/",
        DEFILLAMA_BASE_URL="https://llama.internal/",
        SEC_BASE_URL="https://sec-data.internal/",
        SEC_FILES_BASE_URL="https://sec-files.internal/",
        X_API_BASE_URL="https://x.internal/",
        GOPLUS_BASE_URL="https://goplus.internal/",
        HONEYPOT_BASE_URL="https://honeypot.internal/",
        TELEGRAM_BASE_URL="https://telegram.internal/",
    )
    endpoints = settings.endpoints
    # Trailing slashes are trimmed so base + path never doubles a slash.
    assert endpoints.url("coingecko.global") == "https://coingecko.internal/api/v3/global"
    assert endpoints.url("geckoterminal.token_pools", network="eth", address="0x1") == (
        "https://gecko.internal/api/v2/networks/eth/tokens/0x1/pools"
    )
    assert endpoints.url("dexscreener.token", address="0x1") == "https://dex.internal/latest/dex/tokens/0x1"
    assert endpoints.url("yahoo.chart", symbol="AAPL") == "https://yahoo.internal/v8/finance/chart/AAPL"
    assert endpoints.url("binance_futures.ticker_24h") == "https://binance.internal/fapi/v1/ticker/24hr"
    assert endpoints.url("bybit.tickers") == "https://bybit.internal/v5/market/tickers"
    assert endpoints.url("hyperliquid.info") == "https://hyperliquid.internal/info"
    assert endpoints.url("coinglass.funding_history") == (
        "https://coinglass.internal/api/futures/funding-rate/history"
    )
    assert endpoints.url("defillama.protocols") == "https://llama.internal/protocols"
    assert endpoints.url("sec.company_facts", cik=320193) == (
        "https://sec-data.internal/api/xbrl/companyfacts/CIK0000320193.json"
    )
    assert endpoints.url("sec_files.company_tickers") == (
        "https://sec-files.internal/files/company_tickers.json"
    )
    assert endpoints.url("x_api.tweet_counts") == "https://x.internal/2/tweets/counts/recent"
    assert endpoints.url("goplus.token_security", chain_id="1") == (
        "https://goplus.internal/api/v1/token_security/1"
    )
    assert endpoints.url("honeypot.check") == "https://honeypot.internal/v2/IsHoneypot"
    assert endpoints.url("telegram.send_message", token="123") == (
        "https://telegram.internal/bot123/sendMessage"
    )


def test_a_single_endpoint_can_point_at_another_host():
    settings = load(COINGECKO_CONTRACT_URL="https://proxy.internal/coins/{platform}/{address}")
    assert settings.endpoints.url("coingecko.contract", platform="base", address="0x1") == (
        "https://proxy.internal/coins/base/0x1"
    )
    # The rest of the vendor keeps the shared base.
    assert settings.endpoints.url("coingecko.global") == "https://api.coingecko.com/api/v3/global"


def test_a_mounted_catalogue_overlays_the_bundled_one(tmp_path):
    overlay = tmp_path / "endpoints.yaml"
    overlay.write_text(
        "coingecko:\n"
        "  base: https://cfg.internal/v3\n"
        "cryptopanic:\n"
        "  posts: https://news.internal/api/v1/posts/\n",
        encoding="utf-8",
    )
    settings = load(ENDPOINTS_FILE=str(overlay))
    assert settings.endpoints.url("coingecko.global") == "https://cfg.internal/v3/global"
    assert settings.endpoints.url("cryptopanic.posts") == "https://news.internal/api/v1/posts/"
    # Entries the overlay does not mention keep their bundled values.
    assert settings.endpoints.url("defillama.protocols") == "https://api.llama.fi/protocols"


def test_a_duplicated_key_in_a_mounted_catalogue_is_rejected(tmp_path):
    """``yaml.safe_load`` keeps the last duplicate - live traffic would move silently."""
    overlay = tmp_path / "endpoints.yaml"
    overlay.write_text(
        "coingecko:\n"
        "  base: https://first.internal/v3\n"
        "  base: https://second.internal/v3\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="duplicate key 'base'"):
        load(ENDPOINTS_FILE=str(overlay))


def test_a_calendar_url_is_registered_for_redaction():
    url = "https://calendar.internal/feed?key=calendar-secret-key"
    settings = load(TOKENOMICS_CALENDAR_URL=url)
    assert settings.providers.tokenomics_calendar_url == url
    # A feed URL can carry its own credential; the log filter must scrub it whole.
    assert url not in redact(f"calendar fetch {url}")


def test_an_unknown_endpoint_name_fails_loudly():
    with pytest.raises(ConfigError, match="unknown endpoint 'coingecko.nope'"):
        load().endpoints.url("coingecko.nope")


def test_a_missing_placeholder_fails_loudly():
    settings = load(COINGECKO_CONTRACT_URL="https://proxy.internal/{platform}/{address}")
    with pytest.raises(ConfigError, match="placeholder"):
        settings.endpoints.url("coingecko.contract", platform="ethereum")


def test_a_catalogue_missing_a_required_entry_cannot_boot():
    settings = replace(load(), endpoints=EndpointSettings(bases={}, paths={}))
    problems = settings.validate()
    assert any("endpoint catalogue is missing" in problem for problem in problems)
    assert any("coingecko.contract" in problem for problem in problems)
    assert len(REQUIRED_ENDPOINTS) > 30


def test_the_bundled_catalogue_satisfies_every_required_endpoint():
    endpoints = load().endpoints
    assert [name for name in REQUIRED_ENDPOINTS if not endpoints.has(name)] == []


def test_every_bundled_endpoint_resolves_to_a_concrete_url():
    """Catches a typo in endpoints.yaml (bad scheme, unknown placeholder)."""
    endpoints = load().endpoints
    templates = endpoints.as_dict()
    for name in endpoints.keys():
        fields = [field for _, field, _, _ in Formatter().parse(templates[name]) if field]
        url = endpoints.url(name, **{field: 1 for field in fields})
        assert url.startswith(("http://", "https://")), name
        assert "{" not in url, name


def test_no_scanner_contains_a_url():
    """The guard that keeps this refactor from rotting: URLs live in YAML only."""
    scanners_root = Path(scanners_module).parent
    offenders = [
        f"{path.relative_to(scanners_root)}:{lineno}"
        for path in scanners_root.rglob("*.py")
        if "__pycache__" not in path.parts
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if "http://" in line or "https://" in line
    ]
    assert offenders == []


def test_blockscout_chain_override_beats_the_template():
    settings = load(BLOCKSCOUT_BASE_URL_ARBITRUM="https://arb.internal/")
    profile_url = "https://arbitrum.blockscout.com"
    assert settings.endpoints.blockscout_url("arbitrum", profile_url) == "https://arb.internal"
    assert settings.endpoints.blockscout_url("ethereum", "https://eth.blockscout.com") == (
        "https://ethereum.blockscout.com"
    )


def test_legacy_blockscout_key_applies_to_the_default_chain_only():
    settings = load(BLOCKSCOUT_BASE_URL="https://eth.internal", DEFAULT_CHAIN="polygon")
    assert settings.endpoints.blockscout_url("polygon", "https://polygon.blockscout.com") == (
        "https://eth.internal"
    )
    assert settings.endpoints.blockscout_url("base", "https://base.blockscout.com") == (
        "https://base.blockscout.com"
    )


def test_an_unsupported_chain_never_gets_an_explorer():
    """Solana has no Blockscout, so the template must not invent a URL for it."""
    assert load().endpoints.blockscout_url("solana", None) is None
