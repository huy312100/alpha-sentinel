"""Centralised, environment-driven configuration for AlphaSentinel.

Design rules
------------
1. Every secret is wrapped in :class:`SecretStr` so an accidental ``print()``,
   ``repr()`` or log record can never leak it.
2. Parsing is strict: a malformed integer/bool raises :class:`ConfigError`
   instead of silently falling back to a default (fail fast at boot, not at
   03:00 inside a scan loop).
3. All dependencies are injectable: tests build a ``Settings`` from a plain
   ``dict`` without mutating ``os.environ``.
4. The same file works locally, in Docker and in CI - relative paths resolve
   against the repository root, not the current working directory.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from config.endpoints import (
    BLOCKSCOUT_CHAINS,
    REQUIRED_ENDPOINTS,
    EndpointSettings,
    load_endpoint_settings,
)
from config.errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_TRUE = {"1", "true", "yes", "y", "on"}
_ENV_FALSE = {"0", "false", "no", "n", "off", ""}


# ---------------------------------------------------------------------------
# Secret handling
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SecretStr:
    """A string that refuses to reveal itself through repr/str/logging."""

    _value: str = field(default="", repr=False)

    def get_secret_value(self) -> str:
        return self._value

    def is_configured(self) -> bool:
        return bool(self._value.strip())

    def __bool__(self) -> bool:  # supports ``if settings.llm.api_key:``
        return self.is_configured()

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return "SecretStr('***')" if self.is_configured() else "SecretStr('')"

    __str__ = __repr__


# ---------------------------------------------------------------------------
# Primitive env parsers
# ---------------------------------------------------------------------------
def _get(env: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    raw = env.get(key)
    if raw is None:
        return default
    value = raw.strip()
    return value if value != "" else default


def _env_str(env: Mapping[str, str], key: str, default: str) -> str:
    return _get(env, key, default) or default


def _env_secret(env: Mapping[str, str], key: str) -> SecretStr:
    value = _get(env, key, "") or ""
    # A template placeholder such as ``sk-your-key`` is not a usable credential.
    if value.lower().startswith(("sk-your", "<", "changeme", "your-")):
        value = ""
    return SecretStr(value)


def _env_int(
    env: Mapping[str, str],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{key} must be <= {maximum}, got {value}")
    return value


def _env_float(env: Mapping[str, str], key: str, default: float, *, minimum: float | None = None) -> float:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} must be a float, got {raw!r}") from exc
    if not math.isfinite(value):
        # ``nan`` / ``inf`` silently disable comparisons (a max risk score of NaN
        # never vetoes; an infinite timeout never fires), so fail at boot.
        raise ConfigError(f"{key} must be finite, got {raw!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {value}")
    return value


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _get(env, key)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _ENV_TRUE:
        return True
    if lowered in _ENV_FALSE:
        return False
    raise ConfigError(f"{key} must be a boolean (true/false), got {raw!r}")


def _resolve_existing_prefix(target: Path) -> Path:
    """Resolve ``target`` without letting ``..`` normalise a symlink loop away.

    ``Path.resolve()`` is non-strict by default and normalises ``..`` before it
    follows the component it points at, so ``loop/../hot.db`` comes back as a
    sibling of the loop instead of raising: the gate would pass and the store
    would create its database somewhere the operator never configured.

    Strict resolution stops at the first missing component - ``HOT_DB_PATH``,
    ``PARQUET_ROOT`` and the watchlist are routinely created on first boot - so
    only the longest existing prefix is resolved strictly and the rest is
    re-attached. A loop anywhere in that prefix still raises.
    """
    probe, tail = target, []
    while True:
        try:
            return probe.resolve(strict=True).joinpath(*reversed(tail))
        except FileNotFoundError:
            if probe == probe.parent:
                raise
            tail.append(probe.name)
            probe = probe.parent


def _env_path(env: Mapping[str, str], key: str, default: str) -> Path:
    raw = _get(env, key, default) or default
    try:
        path = Path(raw).expanduser()
        return _resolve_existing_prefix(path if path.is_absolute() else PROJECT_ROOT / path)
    except (OSError, RuntimeError) as exc:
        # Every setting is resolved here - a relative one against the repository
        # root, an absolute one for the target it points at - and that reaches the
        # filesystem, not just the value: ``Path.resolve()`` raises ``RuntimeError``
        # on a symlink loop, ``expanduser()`` when HOME is gone and a stat raises
        # ``OSError``. All three are deploy mistakes, so report them like any other
        # bad setting - escaping ``Settings.load()`` uncaught would print a
        # traceback and exit 1 instead of the diagnostic and exit 2.
        # Deliberately narrow: an unrelated programming error must still escape.
        raise ConfigError(f"{key} cannot be resolved to a path: {raw!r} ({exc})") from exc


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------
#: Layer slugs collected by default, in display order. Kept as a module constant
#: because ``slots=True`` dataclasses do not expose defaults as class attributes.
DEFAULT_ENABLED_LAYERS: tuple[str, ...] = (
    "onchain",
    "derivatives",
    "social",
    "dex",
    "ecosystem",
    "tokenomics",
    "market",
    "fundamentals",
    "macro",
    "technicals",
    "security",
)

#: Layer slugs that older ``.env`` files may still use. ``scanners.registry``
#: resolves them onto the module that owns the data now:
#:   * ``airdrop``  -> ``ecosystem`` (DeFiLlama / GrowThePie ecosystem metrics)
#:   * ``macro``    -> both the listed-market macro backdrop and, for crypto,
#:                     the tokenomics/event layer that replaced it
LEGACY_LAYER_ALIASES: dict[str, str] = {
    "airdrop": "ecosystem",
    "macro": "tokenomics",
}


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    app_env: str = "development"
    log_level: str = "INFO"
    log_json: bool = True
    demo_mode: bool = False
    default_chain: str = "ethereum"

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """Connection defaults for one OpenAI-compatible chat-completions endpoint."""

    base_url: str
    alpha_model: str = ""
    risk_model: str = ""
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0


#: Any OpenAI-compatible endpoint works: pick a preset, or use ``custom`` with
#: ``LLM_BASE_URL`` for vLLM, Ollama, Together, Groq, LiteLLM, an internal gateway...
#: ``ALPHA_MODEL`` / ``RISK_MODEL`` / ``LLM_*_USD_PER_MTOK`` override the preset.
#: Prices are per 1M tokens and only feed the cost ledger - verify them.
LLM_PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "deepseek": ProviderPreset("https://api.deepseek.com", "deepseek-chat", "deepseek-reasoner", 0.27, 1.10),
    "openai": ProviderPreset("https://api.openai.com/v1", "gpt-4o-mini", "gpt-4o", 2.50, 10.00),
    "anthropic": ProviderPreset("https://api.anthropic.com/v1", "claude-sonnet-4-5", "claude-sonnet-4-5", 3.00, 15.00),
    "openrouter": ProviderPreset(
        "https://openrouter.ai/api/v1", "deepseek/deepseek-chat", "deepseek/deepseek-r1", 0.27, 1.10
    ),
    "custom": ProviderPreset(""),
}
DEFAULT_LLM_PROVIDER = "deepseek"

#: How the alpha side of the audit is produced. ``llm`` is the historical
#: behaviour and the default; ``model`` uses a trained artefact and escalates to
#: the LLM only when the prediction is unsafe; ``prior`` never calls the LLM for
#: alpha at all. See :mod:`ml.predictor`.
ALPHA_PREDICTOR_MODES: tuple[str, ...] = ("llm", "model", "prior")


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """Agent 1 (alpha analyst) and Agent 2 (risk auditor) model wiring.

    Both agents speak the OpenAI ``/chat/completions`` dialect; the provider is
    only a base URL plus two model ids. Swapping DeepSeek for GPT or Claude is
    an environment change, not a code change.
    """

    provider: str = DEFAULT_LLM_PROVIDER
    api_key: SecretStr = SecretStr()
    base_url: str = LLM_PROVIDER_PRESETS["deepseek"].base_url
    alpha_model: str = "deepseek-chat"
    risk_model: str = "deepseek-reasoner"
    timeout_seconds: float = 180.0
    max_retries: int = 3
    max_concurrency: int = 4
    alpha_temperature: float = 0.2
    risk_temperature: float = 0.1
    max_tokens: int = 2_048
    risk_max_tokens: int = 4_096
    input_usd_per_mtok: float = 0.27
    output_usd_per_mtok: float = 1.10
    #: Hard spend ceilings in USD; ``0`` disables the ceiling. The client refuses
    #: a request that would cross one, so an unattended daemon cannot run up an
    #: open-ended bill. A ceiling without prices is a boot problem (see
    #: :meth:`Settings.validate`): nothing would be measurable.
    max_usd_per_cycle: float = 0.0
    max_usd_per_day: float = 0.0

    def assert_configured(self) -> None:
        if not self.api_key.is_configured():
            raise ConfigError(
                "LLM_API_KEY is not set. Export it (DEEPSEEK_API_KEY still works for the "
                "default provider), or run with DEMO_MODE=true to exercise the pipeline "
                "with scripted agents."
            )


@dataclass(frozen=True, slots=True)
class R2Settings:
    """Cloudflare R2 is S3-compatible; these map 1:1 onto boto3 kwargs."""

    enabled: bool = False
    endpoint_url: str = ""
    bucket: str = ""
    access_key_id: SecretStr = SecretStr()
    secret_access_key: SecretStr = SecretStr()
    prefix: str = "alpha-sentinel"
    region: str = "auto"

    def is_configured(self) -> bool:
        return bool(
            self.enabled
            and self.endpoint_url
            and self.bucket
            and self.access_key_id.is_configured()
            and self.secret_access_key.is_configured()
        )

    def client_kwargs(self) -> dict[str, Any]:
        return {
            "endpoint_url": self.endpoint_url,
            "region_name": self.region or "auto",
            "aws_access_key_id": self.access_key_id.get_secret_value(),
            "aws_secret_access_key": self.secret_access_key.get_secret_value(),
        }


@dataclass(frozen=True, slots=True)
class StorageSettings:
    data_dir: Path = PROJECT_ROOT / "data"
    hot_db_path: Path = PROJECT_ROOT / "data" / "hot" / "alpha_sentinel.db"
    parquet_root: Path = PROJECT_ROOT / "data" / "cold"
    compression: str = "zstd"
    row_group_size: int = 262_144
    local_retention_days: int = 90
    remote_retention_days: int = 365
    r2: R2Settings = R2Settings()

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.hot_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.parquet_root.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class GoPlusSettings:
    """GoPlus credentials; its endpoint URL lives in the endpoint catalogue."""

    app_key: SecretStr = SecretStr()
    app_secret: SecretStr = SecretStr()
    timeout_seconds: float = 15.0

    @property
    def auth_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.app_key.is_configured():
            headers["Authorization"] = f"Bearer {self.app_key.get_secret_value()}"
        return headers


@dataclass(frozen=True, slots=True)
class SocialSettings:
    lunarcrush_api_key: SecretStr = SecretStr()
    x_bearer_token: SecretStr = SecretStr()

    def is_configured(self) -> bool:
        return self.lunarcrush_api_key.is_configured() or self.x_bearer_token.is_configured()


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Optional data-vendor credentials for the crypto layer stack.

    Every field is optional: a layer whose providers are unconfigured returns
    ``SKIPPED`` (zero weight) instead of inventing a neutral reading. Keys are
    ``SecretStr`` so they can never be printed by an accidental ``repr``/log.
    """

    arkham_api_key: SecretStr = SecretStr()
    nansen_api_key: SecretStr = SecretStr()
    dune_api_key: SecretStr = SecretStr()
    #: A saved Dune query id returning one row with ``net_flow_usd`` per token.
    #: ``0`` disables the provider (Dune has no generic "token netflow" endpoint).
    dune_smart_money_query_id: int = 0
    coinglass_api_key: SecretStr = SecretStr()
    cryptopanic_token: SecretStr = SecretStr()
    #: Optional JSON calendar for unlock/vesting/mainnet-upgrade events. Teams can
    #: point this at TokenUnlocks, an internal sheet export or their own service.
    tokenomics_calendar_url: str = ""

    def configured_sources(self) -> tuple[str, ...]:
        """Names only - never values - for boot logs and ``/status``."""
        sources: list[str] = []
        if self.arkham_api_key.is_configured():
            sources.append("arkham")
        if self.nansen_api_key.is_configured():
            sources.append("nansen")
        if self.dune_api_key.is_configured() and self.dune_smart_money_query_id > 0:
            sources.append("dune")
        if self.coinglass_api_key.is_configured():
            sources.append("coinglass")
        if self.cryptopanic_token.is_configured():
            sources.append("cryptopanic")
        return tuple(sources)


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    bot_token: SecretStr = SecretStr()
    chat_id: str = ""
    alert_min_score: float = 70.0
    alert_on_veto: bool = True

    def is_configured(self) -> bool:
        return self.bot_token.is_configured() and bool(self.chat_id)


@dataclass(frozen=True, slots=True)
class ScannerSettings:
    watchlist_path: Path = PROJECT_ROOT / "config" / "watchlists.yaml"
    scan_interval_seconds: int = 300
    token_concurrency: int = 4
    layer_timeout_seconds: float = 20.0
    http_timeout_seconds: float = 25.0
    maintenance_interval_seconds: int = 3_600
    trades_lookback: int = 500
    #: ``--health`` calls a cycle stale after this long; ``0`` derives it from
    #: ``scan_interval_seconds`` with a floor (see :mod:`bot.health`).
    health_max_stale_seconds: int = 0
    enabled_layers: tuple[str, ...] = DEFAULT_ENABLED_LAYERS
    #: SEC EDGAR's fair-access policy requires a contact in the User-Agent.
    #: Empty -> the fundamentals layer reports SKIPPED instead of calling out.
    sec_user_agent: str = ""


@dataclass(frozen=True, slots=True)
class ThresholdSettings:
    """Decision boundaries used by :func:`core.scoring.reconcile`."""

    min_final_score: float = 65.0
    max_risk_score: float = 70.0
    watch_score: float = 50.0
    veto_score_ceiling: float = 35.0
    risk_drag: float = 0.5
    alpha_weight: float = 0.6
    position_tiers: tuple[tuple[float, float], ...] = ((85.0, 1.0), (75.0, 0.75), (65.0, 0.5))


@dataclass(frozen=True, slots=True)
class MLSettings:
    """Optional learned alpha scorer (see :mod:`ml`).

    Defaults are inert: ``alpha_predictor="llm"`` with ``shadow=False`` runs
    exactly the pre-existing two-agent flow, so deploying this code changes no
    behaviour until an operator opts in.
    """

    #: ``llm`` | ``model`` | ``prior`` - who is authoritative for alpha.
    alpha_predictor: str = "llm"
    model_path: Path = PROJECT_ROOT / "models" / "alpha_model.pkl"
    #: Run the model next to the LLM without letting it decide, to measure
    #: agreement on live traffic before switching ``alpha_predictor`` over.
    shadow: bool = False
    #: Escalate to the LLM when a prediction is within this distance of a
    #: decision boundary - the call could flip, so the cheap path is not safe.
    escalate_margin: float = 5.0
    #: Escalate when the *input trust* score falls below this (thin snapshot).
    min_confidence: float = 0.6
    #: Persist one ``training_rows`` row per completed audit.
    write_training_rows: bool = True


@dataclass(frozen=True, slots=True)
class Settings:
    runtime: RuntimeSettings = RuntimeSettings()
    llm: LLMSettings = LLMSettings()
    storage: StorageSettings = StorageSettings()
    goplus: GoPlusSettings = GoPlusSettings()
    social: SocialSettings = SocialSettings()
    providers: ProviderSettings = ProviderSettings()
    endpoints: EndpointSettings = EndpointSettings()
    telegram: TelegramSettings = TelegramSettings()
    scanners: ScannerSettings = ScannerSettings()
    thresholds: ThresholdSettings = ThresholdSettings()
    ml: MLSettings = MLSettings()

    # -- validation ---------------------------------------------------------
    def missing_critical(self) -> list[str]:
        missing: list[str] = []
        if not self.runtime.demo_mode and not self.llm.api_key.is_configured():
            missing.append("LLM_API_KEY")
        if self.storage.r2.enabled and not self.storage.r2.is_configured():
            missing.append("R2_ENDPOINT_URL/R2_BUCKET/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY")
        return missing

    def validate(self) -> list[str]:
        """Return human-readable problems. An empty list means ready to boot."""
        problems = [f"missing required setting: {key}" for key in self.missing_critical()]
        if self.runtime.is_production and self.runtime.demo_mode:
            problems.append("DEMO_MODE must be false in production")
        if self.storage.compression not in {"zstd", "snappy", "gzip", "brotli", "none"}:
            problems.append(f"unsupported PARQUET_COMPRESSION: {self.storage.compression}")
        if self.telegram.bot_token.is_configured() and not self.telegram.chat_id:
            problems.append("TELEGRAM_BOT_TOKEN is set but TELEGRAM_CHAT_ID is empty")
        if (self.llm.max_usd_per_cycle or self.llm.max_usd_per_day) and not (
            self.llm.input_usd_per_mtok or self.llm.output_usd_per_mtok
        ):
            problems.append(
                "LLM_MAX_USD_PER_CYCLE/LLM_MAX_USD_PER_DAY are set but "
                "LLM_INPUT_USD_PER_MTOK and LLM_OUTPUT_USD_PER_MTOK are both 0: "
                "spend cannot be measured, so the ceiling would never bind"
            )
        if 0 < self.scanners.health_max_stale_seconds < 60:
            problems.append("HEALTH_MAX_STALE_SECONDS must be 0 (auto) or >= 60")
        unknown_layers = [
            name
            for name in self.scanners.enabled_layers
            if name not in DEFAULT_ENABLED_LAYERS and name not in LEGACY_LAYER_ALIASES
        ]
        if unknown_layers:
            problems.append(
                "unknown ENABLED_LAYERS slug(s): "
                + ", ".join(sorted(unknown_layers))
                + f"; expected one of: {', '.join(DEFAULT_ENABLED_LAYERS)}"
            )
        missing_endpoints = [name for name in REQUIRED_ENDPOINTS if not self.endpoints.has(name)]
        if missing_endpoints:
            problems.append(
                "endpoint catalogue is missing: "
                + ", ".join(missing_endpoints)
                + " (add them to config/endpoints.yaml or the file mounted at ENDPOINTS_FILE)"
            )
        return problems

    def validate_or_raise(self) -> None:
        problems = self.validate()
        if problems:
            raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))

    def redacted_summary(self) -> dict[str, Any]:
        """Safe to log verbatim at boot."""
        return {
            "env": self.runtime.app_env,
            "demo_mode": self.runtime.demo_mode,
            "llm_provider": self.llm.provider,
            "llm_base_url": self.llm.base_url,
            "llm_configured": self.llm.api_key.is_configured(),
            "alpha_model": self.llm.alpha_model,
            "risk_model": self.llm.risk_model,
            "parquet_root": str(self.storage.parquet_root),
            "hot_db_path": str(self.storage.hot_db_path),
            "parquet_compression": self.storage.compression,
            "r2_enabled": self.storage.r2.enabled,
            "r2_configured": self.storage.r2.is_configured(),
            "goplus_configured": self.goplus.app_key.is_configured(),
            "provider_sources": list(self.providers.configured_sources()),
            "telegram_configured": self.telegram.is_configured(),
            "scan_interval_seconds": self.scanners.scan_interval_seconds,
            "health_max_stale_seconds": self.scanners.health_max_stale_seconds,
            "llm_max_usd_per_cycle": self.llm.max_usd_per_cycle,
            "llm_max_usd_per_day": self.llm.max_usd_per_day,
            "alpha_predictor": self.ml.alpha_predictor,
            "alpha_predictor_shadow": self.ml.shadow,
            "alpha_model_path": str(self.ml.model_path),
            "layers": list(self.scanners.enabled_layers),
            "sec_user_agent_configured": bool(self.scanners.sec_user_agent),
        }

    # -- construction -------------------------------------------------------
    @classmethod
    def load(cls, env: Mapping[str, str] | None = None, *, load_dotenv_file: bool = True) -> "Settings":
        if env is None:
            if load_dotenv_file:
                _maybe_load_dotenv()
            env = os.environ

        runtime = RuntimeSettings(
            app_env=_env_str(env, "APP_ENV", "development"),
            log_level=_env_str(env, "LOG_LEVEL", "INFO").upper(),
            log_json=_env_bool(env, "LOG_JSON", True),
            demo_mode=_env_bool(env, "DEMO_MODE", False),
            default_chain=_env_str(env, "DEFAULT_CHAIN", "ethereum").lower(),
        )

        provider = _env_str(env, "LLM_PROVIDER", DEFAULT_LLM_PROVIDER).lower()
        preset = _llm_provider_preset(provider)
        # Old .env files keep working: DEEPSEEK_* only fills in values that the
        # LLM_* names did not already set, and only for the DeepSeek preset. A
        # legacy DeepSeek key must never be sent to another vendor's endpoint.
        env = _expand_legacy_llm_env(env, include_legacy=provider == DEFAULT_LLM_PROVIDER)

        llm = LLMSettings(
            provider=provider,
            api_key=_env_secret(env, "LLM_API_KEY"),
            base_url=_env_str(env, "LLM_BASE_URL", preset.base_url).rstrip("/"),
            alpha_model=_env_str(env, "ALPHA_MODEL", preset.alpha_model),
            risk_model=_env_str(env, "RISK_MODEL", preset.risk_model),
            timeout_seconds=_env_float(env, "LLM_TIMEOUT_SECONDS", 180.0, minimum=5.0),
            max_retries=_env_int(env, "LLM_MAX_RETRIES", 3, minimum=0, maximum=10),
            max_concurrency=_env_int(env, "LLM_MAX_CONCURRENCY", 4, minimum=1, maximum=32),
            input_usd_per_mtok=_env_float(
                env, "LLM_INPUT_USD_PER_MTOK", preset.input_usd_per_mtok, minimum=0.0
            ),
            output_usd_per_mtok=_env_float(
                env, "LLM_OUTPUT_USD_PER_MTOK", preset.output_usd_per_mtok, minimum=0.0
            ),
            max_usd_per_cycle=_env_float(env, "LLM_MAX_USD_PER_CYCLE", 0.0, minimum=0.0),
            max_usd_per_day=_env_float(env, "LLM_MAX_USD_PER_DAY", 0.0, minimum=0.0),
        )
        _check_llm_wiring(llm)

        r2 = R2Settings(
            enabled=_env_bool(env, "R2_ENABLED", False),
            endpoint_url=_env_str(env, "R2_ENDPOINT_URL", ""),
            bucket=_env_str(env, "R2_BUCKET", ""),
            access_key_id=_env_secret(env, "R2_ACCESS_KEY_ID"),
            secret_access_key=_env_secret(env, "R2_SECRET_ACCESS_KEY"),
            prefix=_env_str(env, "R2_PREFIX", "alpha-sentinel").strip("/"),
            region=_env_str(env, "R2_REGION", "auto"),
        )

        storage = StorageSettings(
            data_dir=_env_path(env, "DATA_DIR", "data"),
            hot_db_path=_env_path(env, "HOT_DB_PATH", "data/hot/alpha_sentinel.db"),
            parquet_root=_env_path(env, "PARQUET_ROOT", "data/cold"),
            compression=_env_str(env, "PARQUET_COMPRESSION", "zstd").lower(),
            row_group_size=_env_int(env, "PARQUET_ROW_GROUP_SIZE", 262_144, minimum=1_024),
            local_retention_days=_env_int(env, "LOCAL_RETENTION_DAYS", 90, minimum=1),
            remote_retention_days=_env_int(env, "REMOTE_RETENTION_DAYS", 365, minimum=1),
            r2=r2,
        )

        goplus = GoPlusSettings(
            app_key=_env_secret(env, "GOPLUS_APP_KEY"),
            app_secret=_env_secret(env, "GOPLUS_APP_SECRET"),
        )

        social = SocialSettings(
            lunarcrush_api_key=_env_secret(env, "LUNARCRUSH_API_KEY"),
            x_bearer_token=_env_secret(env, "X_BEARER_TOKEN"),
        )

        providers = ProviderSettings(
            arkham_api_key=_env_secret(env, "ARKHAM_API_KEY"),
            nansen_api_key=_env_secret(env, "NANSEN_API_KEY"),
            dune_api_key=_env_secret(env, "DUNE_API_KEY"),
            dune_smart_money_query_id=_env_int(env, "DUNE_SMART_MONEY_QUERY_ID", 0, minimum=0),
            coinglass_api_key=_env_secret(env, "COINGLASS_API_KEY"),
            cryptopanic_token=_env_secret(env, "CRYPTOPANIC_TOKEN"),
            tokenomics_calendar_url=_env_str(env, "TOKENOMICS_CALENDAR_URL", ""),
        )

        endpoints = load_endpoint_settings(env, default_chain=runtime.default_chain)

        telegram = TelegramSettings(
            bot_token=_env_secret(env, "TELEGRAM_BOT_TOKEN"),
            chat_id=_env_str(env, "TELEGRAM_CHAT_ID", ""),
            alert_min_score=_env_float(env, "ALERT_MIN_SCORE", 70.0, minimum=0.0),
            alert_on_veto=_env_bool(env, "ALERT_ON_VETO", True),
        )

        scanners = ScannerSettings(
            watchlist_path=_env_path(env, "WATCHLIST_PATH", "config/watchlists.yaml"),
            scan_interval_seconds=_env_int(env, "SCAN_INTERVAL_SECONDS", 300, minimum=30),
            token_concurrency=_env_int(env, "TOKEN_CONCURRENCY", 4, minimum=1, maximum=64),
            layer_timeout_seconds=_env_float(env, "LAYER_TIMEOUT_SECONDS", 20.0, minimum=1.0),
            http_timeout_seconds=_env_float(env, "HTTP_TIMEOUT_SECONDS", 25.0, minimum=1.0),
            maintenance_interval_seconds=_env_int(env, "MAINTENANCE_INTERVAL_SECONDS", 3_600, minimum=60),
            health_max_stale_seconds=_env_int(env, "HEALTH_MAX_STALE_SECONDS", 0, minimum=0),
            enabled_layers=_parse_csv(_env_str(env, "ENABLED_LAYERS", ",".join(DEFAULT_ENABLED_LAYERS))),
            sec_user_agent=_env_str(env, "SEC_USER_AGENT", "").strip(),
        )

        thresholds = ThresholdSettings(
            min_final_score=_env_float(env, "MIN_FINAL_SCORE", 65.0, minimum=0.0),
            max_risk_score=_env_float(env, "MAX_RISK_SCORE", 70.0, minimum=0.0),
            alpha_weight=_env_float(env, "ALPHA_WEIGHT", 0.6, minimum=0.0),
        )

        alpha_predictor = _env_str(env, "ALPHA_PREDICTOR", "llm").strip().lower()
        if alpha_predictor not in ALPHA_PREDICTOR_MODES:
            raise ConfigError(
                f"ALPHA_PREDICTOR must be one of {', '.join(ALPHA_PREDICTOR_MODES)}, got {alpha_predictor!r}"
            )
        ml = MLSettings(
            alpha_predictor=alpha_predictor,
            model_path=_env_path(env, "ALPHA_MODEL_PATH", "models/alpha_model.pkl"),
            shadow=_env_bool(env, "ALPHA_SHADOW", False),
            escalate_margin=_env_float(env, "ALPHA_ESCALATE_MARGIN", 5.0, minimum=0.0),
            min_confidence=_env_float(env, "ALPHA_MIN_CONFIDENCE", 0.6, minimum=0.0),
            write_training_rows=_env_bool(env, "ML_WRITE_TRAINING_ROWS", True),
        )

        settings = cls(
            runtime=runtime,
            llm=llm,
            storage=storage,
            goplus=goplus,
            social=social,
            providers=providers,
            endpoints=endpoints,
            telegram=telegram,
            scanners=scanners,
            thresholds=thresholds,
            ml=ml,
        )
        settings._register_secrets()
        return settings

    def _register_secrets(self) -> None:
        try:
            from core.logging import register_secret
        except ImportError:  # pragma: no cover - core is always importable in-tree
            return
        for secret in (
            self.llm.api_key,
            self.storage.r2.access_key_id,
            self.storage.r2.secret_access_key,
            self.goplus.app_key,
            self.goplus.app_secret,
            self.social.lunarcrush_api_key,
            self.social.x_bearer_token,
            self.providers.arkham_api_key,
            self.providers.nansen_api_key,
            self.providers.dune_api_key,
            self.providers.coinglass_api_key,
            self.providers.cryptopanic_token,
            self.telegram.bot_token,
        ):
            register_secret(secret.get_secret_value())
        #: A calendar URL is a plain string, and a self-hosted feed may carry its key
        #: in the query string; ``register_secret`` ignores values that are too short
        #: or empty, so an unset calendar is a no-op.
        register_secret(self.providers.tokenomics_calendar_url)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#: New name -> legacy names still accepted so existing ``.env`` files boot.
_LEGACY_LLM_ENV: dict[str, tuple[str, ...]] = {
    "LLM_API_KEY": ("DEEPSEEK_API_KEY",),
    "LLM_BASE_URL": ("DEEPSEEK_BASE_URL",),
    "LLM_TIMEOUT_SECONDS": ("DEEPSEEK_TIMEOUT_SECONDS",),
    "LLM_MAX_RETRIES": ("DEEPSEEK_MAX_RETRIES",),
    "LLM_MAX_CONCURRENCY": ("DEEPSEEK_MAX_CONCURRENCY",),
    "LLM_INPUT_USD_PER_MTOK": ("DEEPSEEK_INPUT_USD_PER_MTOK",),
    "LLM_OUTPUT_USD_PER_MTOK": ("DEEPSEEK_OUTPUT_USD_PER_MTOK",),
}


def _expand_legacy_llm_env(env: Mapping[str, str], *, include_legacy: bool) -> Mapping[str, str]:
    """Copy legacy ``DEEPSEEK_*`` values onto the ``LLM_*`` names (never the reverse).

    Only for the DeepSeek preset: forwarding a legacy key or price table to
    ``openai``/``openrouter``/``custom`` would send the secret to another vendor and
    corrupt the cost ledger.
    """
    if not include_legacy:
        return env
    merged: dict[str, str] = dict(env)
    for canonical, legacy_names in _LEGACY_LLM_ENV.items():
        if (merged.get(canonical) or "").strip():
            continue
        for legacy in legacy_names:
            value = (merged.get(legacy) or "").strip()
            if value:
                merged[canonical] = value
                break
    return merged


def _llm_provider_preset(provider: str) -> ProviderPreset:
    try:
        return LLM_PROVIDER_PRESETS[provider]
    except KeyError as exc:
        known = ", ".join(sorted(LLM_PROVIDER_PRESETS))
        raise ConfigError(f"unknown LLM_PROVIDER {provider!r}; expected one of: {known}") from exc


def _check_llm_wiring(llm: LLMSettings) -> None:
    if not llm.base_url:
        raise ConfigError(
            f"LLM_PROVIDER={llm.provider!r} needs LLM_BASE_URL (e.g. http://localhost:11434/v1 "
            "for Ollama, https://api.together.xyz/v1, your gateway URL...)."
        )
    missing = [name for name, value in (("ALPHA_MODEL", llm.alpha_model), ("RISK_MODEL", llm.risk_model)) if not value]
    if missing:
        raise ConfigError(
            f"LLM_PROVIDER={llm.provider!r} has no built-in model defaults; set {', '.join(missing)}."
        )


def _parse_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return DEFAULT_ENABLED_LAYERS
    items = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return items or DEFAULT_ENABLED_LAYERS


def _maybe_load_dotenv() -> None:
    """Load ``.env`` once, without making python-dotenv mandatory."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is pinned
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Call ``get_settings.cache_clear()`` in tests."""
    settings = Settings.load()
    settings.validate_or_raise()
    return settings


def iter_secret_values(settings: Settings) -> Iterable[str]:
    """Diagnostics/test helper; yields only non-empty secret values."""
    for secret in (
        settings.llm.api_key,
        settings.storage.r2.access_key_id,
        settings.storage.r2.secret_access_key,
        settings.goplus.app_key,
        settings.goplus.app_secret,
        settings.social.lunarcrush_api_key,
        settings.social.x_bearer_token,
        settings.providers.arkham_api_key,
        settings.providers.nansen_api_key,
        settings.providers.dune_api_key,
        settings.providers.coinglass_api_key,
        settings.providers.cryptopanic_token,
        settings.telegram.bot_token,
    ):
        if secret.is_configured():
            yield secret.get_secret_value()


__all__ = [
    "ALPHA_PREDICTOR_MODES",
    "BLOCKSCOUT_CHAINS",
    "ConfigError",
    "DEFAULT_LLM_PROVIDER",
    "EndpointSettings",
    "GoPlusSettings",
    "LEGACY_LAYER_ALIASES",
    "LLM_PROVIDER_PRESETS",
    "LLMSettings",
    "MLSettings",
    "PROJECT_ROOT",
    "ProviderPreset",
    "ProviderSettings",
    "R2Settings",
    "RuntimeSettings",
    "ScannerSettings",
    "SecretStr",
    "Settings",
    "SocialSettings",
    "StorageSettings",
    "TelegramSettings",
    "ThresholdSettings",
    "get_settings",
    "iter_secret_values",
]
