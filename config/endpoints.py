"""Vendor endpoint catalogue: every outbound URL is data, not scanner code.

A scanner must not know where a vendor lives. Instead it asks for a *named*
endpoint - ``endpoints.url("coingecko.contract", platform=..., address=...)`` -
and this module resolves the name against, in increasing priority:

1. ``config/endpoints.yaml`` (bundled defaults, next to this module);
2. ``ENDPOINTS_FILE``, a mounted ConfigMap/secret whose entries are merged over
   the bundled file, so a deployment only lists what it changes;
3. ``<VENDOR>_BASE_URL`` for a whole vendor (a mirror, regional host or gateway);
4. ``<VENDOR>_<RESOURCE>_URL`` for one absolute URL.

Why a data file rather than a dataclass full of literals: moving a provider,
pointing a scanner at a paid proxy or reproducing an incident against a mock
server is an operations change, and operations cannot patch Python. The trade is
that a typo in the YAML surfaces as :class:`ConfigError` at boot
(:meth:`config.settings.Settings.validate` checks every name the code calls)
instead of a ``KeyError`` at 03:00.

Every placeholder is filled by the caller; a missing one raises rather than
producing ``/coins/None/contract/``. ``{cik:010d}``-style format specs are
supported because some vendors need zero-padded identifiers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml

from config.errors import ConfigError

#: Bundled catalogue. Shipped as YAML so a deployment can read, diff and replace it.
CATALOGUE_PATH: Path = Path(__file__).resolve().parent / "endpoints.yaml"

#: Mounted overlay (ConfigMap/secret); merged entry-by-entry over the bundled file.
ENDPOINTS_FILE_ENV = "ENDPOINTS_FILE"

#: Chains that accept a per-chain Blockscout override (``BLOCKSCOUT_BASE_URL_<CHAIN>``).
BLOCKSCOUT_CHAINS: tuple[str, ...] = ("ethereum", "bsc", "polygon", "arbitrum", "base")

BLOCKSCOUT_GROUP = "blockscout"
#: ``BLOCKSCOUT_BASE_URL`` historically means "override DEFAULT_CHAIN only", so the
#: all-chain base needs its own name.
_BLOCKSCOUT_BASE_ENV = "BLOCKSCOUT_EXPLORER_BASE_URL"
#: The original template key, kept working as an alias for that base.
_BLOCKSCOUT_TEMPLATE_ENV = "BLOCKSCOUT_BASE_URL_TEMPLATE"

#: Every name scanner/bot code calls. ``Settings.validate`` refuses to boot when a
#: replacement catalogue cannot satisfy one of them, so a partial ConfigMap mount
#: fails at startup instead of mid-scan.
REQUIRED_ENDPOINTS: tuple[str, ...] = (
    "arkham.transfers",
    "binance_futures.long_short_ratio",
    "binance_futures.open_interest",
    "binance_futures.open_interest_history",
    "binance_futures.premium_index",
    "binance_futures.taker_ratio",
    "binance_futures.ticker_24h",
    "blockscout.counters",
    "blockscout.holders",
    "blockscout.token",
    "bybit.tickers",
    "coingecko.contract",
    "coingecko.global",
    "coinglass.funding_history",
    "coinglass.liquidation_map",
    "coinglass.open_interest_history",
    "cryptopanic.posts",
    "defillama.protocol",
    "defillama.protocols",
    "dexscreener.token",
    "dune.query_results",
    "fear_greed.index",
    "geckoterminal.pool_ohlcv",
    "geckoterminal.pool_trades",
    "geckoterminal.token_pools",
    "goplus.token_security",
    "growthepie.chain_metric",
    "honeypot.check",
    "hyperliquid.info",
    "lunarcrush.coin",
    "nansen.smart_money_netflows",
    "sec.company_facts",
    "sec_files.company_tickers",
    "telegram.send_message",
    "x_api.tweet_counts",
    "x_api.tweet_search",
    "yahoo.chart",
)

#: Any ``{...}`` group, used to build a sample URL for validation only.
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")

_ABSOLUTE_PREFIXES = ("http://", "https://")


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------
def _get(env: Mapping[str, str], key: str) -> str | None:
    raw = env.get(key)
    if raw is None:
        return None
    return raw.strip() or None


def _base_env_key(group: str) -> str:
    """``coingecko`` -> ``COINGECKO_BASE_URL``; ``x_api`` -> ``X_API_BASE_URL``."""
    if group == BLOCKSCOUT_GROUP:
        return _BLOCKSCOUT_BASE_ENV
    return f"{group.upper()}_BASE_URL"


def _entry_env_key(name: str) -> str:
    """``coingecko.contract`` -> ``COINGECKO_CONTRACT_URL``."""
    return name.upper().replace(".", "_") + "_URL"


# ---------------------------------------------------------------------------
# Catalogue loading
# ---------------------------------------------------------------------------
def _read_catalogue(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read endpoint catalogue {path}: {exc}") from exc
    except yaml.YAMLError as exc:  # pragma: no cover - malformed deploy artefact
        raise ConfigError(f"invalid YAML in endpoint catalogue {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"endpoint catalogue {path} must map vendor -> endpoints")
    return raw


def _flatten(raw: Mapping[str, Any], *, source: str) -> dict[str, str]:
    """``{vendor: {leaf: url}}`` -> ``{"vendor.leaf": url}`` with shape checks."""
    flat: dict[str, str] = {}
    for group, leaves in raw.items():
        name = str(group).strip().lower()
        if not isinstance(leaves, dict):
            raise ConfigError(f"{source}: vendor {name!r} must be a mapping of endpoints")
        for leaf, value in leaves.items():
            key = f"{name}.{str(leaf).strip().lower()}"
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{source}: {key} must be a non-empty string")
            flat[key] = value.strip()
    return flat


@lru_cache(maxsize=1)
def _bundled_leaves() -> dict[str, str]:
    return _flatten(_read_catalogue(CATALOGUE_PATH), source=str(CATALOGUE_PATH))


def _join(base: str, path: str) -> str:
    """Join a base and a path exactly once, so ``//`` never reaches a vendor."""
    if not path:
        return base.rstrip("/")
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _is_absolute(template: str) -> bool:
    return template.lower().startswith(_ABSOLUTE_PREFIXES)


def _format(name: str, template: str, params: Mapping[str, Any]) -> str:
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError) as exc:
        missing = exc.args[0] if exc.args else exc
        raise ConfigError(
            f"endpoint {name!r} cannot fill placeholder {missing!r}; template is {template!r}"
        ) from exc


def _check_placeholders(name: str, template: str) -> None:
    for _, field_name, _, _ in Formatter().parse(template):
        if field_name is not None and not field_name.isidentifier():
            raise ConfigError(
                f"endpoint {name!r} uses unsupported placeholder {{{field_name}}}; "
                "only plain names are allowed"
            )


def _check_absolute(name: str, sample: str) -> None:
    parsed = urlparse(sample)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(
            f"endpoint {name!r} must resolve to an absolute http(s) URL, got {sample!r}"
        )


def _blockscout_overrides(env: Mapping[str, str], *, default_chain: str) -> dict[str, str]:
    """Per-chain explorer bases: ``BLOCKSCOUT_BASE_URL_<CHAIN>`` beats the template.

    ``BLOCKSCOUT_BASE_URL`` (the original single-chain key) still works and is
    applied to ``DEFAULT_CHAIN`` when no explicit per-chain value is present.
    """
    overrides: dict[str, str] = {}
    for chain in BLOCKSCOUT_CHAINS:
        value = _get(env, f"BLOCKSCOUT_BASE_URL_{chain.upper()}")
        if value:
            overrides[chain] = value.rstrip("/")
    legacy = _get(env, "BLOCKSCOUT_BASE_URL")
    if legacy and default_chain not in overrides:
        overrides[default_chain] = legacy.rstrip("/")
    return overrides


# ---------------------------------------------------------------------------
# The settings object
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EndpointSettings:
    """Resolved endpoint catalogue: vendor bases plus named URL templates.

    Both maps are read-only snapshots taken at load time, so a scan can never see
    a half-updated catalogue. Use :meth:`url` / :meth:`blockscout` rather than
    reading the maps directly - they are the ones that know how to join a base
    with a path and how to report a bad name.
    """

    #: ``vendor -> base``; may still contain ``{chain}`` (explorers).
    bases: Mapping[str, str] = field(default_factory=lambda: bundled_endpoints().bases)
    #: ``vendor.resource -> path or absolute URL``.
    paths: Mapping[str, str] = field(default_factory=lambda: bundled_endpoints().paths)
    #: ``chain -> explorer base``; overrides the template for that chain only.
    blockscout_overrides: Mapping[str, str] = field(
        default_factory=lambda: bundled_endpoints().blockscout_overrides
    )

    # -- introspection -------------------------------------------------------
    def keys(self) -> tuple[str, ...]:
        """Every configured endpoint name, sorted (used by validation and diagnostics)."""
        return tuple(sorted(self.paths))

    def has(self, name: str) -> bool:
        """True when ``name`` is callable - present and backed by a base if relative."""
        template = self.paths.get(name)
        if template is None:
            return False
        return _is_absolute(template) or name.partition(".")[0] in self.bases

    def as_dict(self) -> dict[str, str]:
        """Resolved templates with placeholders intact, for diagnostics/logging.

        Endpoints are addresses, never credential stores - see ``endpoints.yaml``.
        """
        return {name: self._template(name) for name in self.keys() if self.has(name)}

    # -- resolution ----------------------------------------------------------
    def base(self, group: str, **params: Any) -> str:
        """Base URL for ``group``, with any ``{placeholder}`` filled from ``params``."""
        if group == BLOCKSCOUT_GROUP:
            chain = str(params.get("chain") or "").strip().lower()
            override = self.blockscout_overrides.get(chain)
            if override:
                return override.rstrip("/")
        template = self.bases.get(group)
        if template is None:
            raise ConfigError(
                f"no base URL configured for endpoint vendor {group!r}; "
                f"add {group}.base to the catalogue or set {_base_env_key(group)}"
            )
        return _format(f"{group}.base", template, params).rstrip("/")

    def url(self, name: str, **params: Any) -> str:
        """Absolute URL for endpoint ``name``, with placeholders filled from ``params``."""
        if name not in self.paths:
            raise ConfigError(
                f"unknown endpoint {name!r}; declare it in config/endpoints.yaml "
                f"(known: {', '.join(self.keys())})"
            )
        return self._resolve(name, params)

    def blockscout_url(self, chain: str, default: str | None = None) -> str | None:
        """Explorer base for ``chain``: explicit override, then template, then default.

        ``default`` is the chain profile's own URL and doubles as the "does this
        chain have an explorer at all?" signal: callers pass ``None`` for an
        unsupported chain and get ``None`` back, so a template can never invent an
        explorer URL for a network that has none.
        """
        slug = (chain or "").strip().lower()
        override = self.blockscout_overrides.get(slug)
        if override:
            return override.rstrip("/")
        if default is None:
            return None
        template = self.bases.get(BLOCKSCOUT_GROUP)
        if template:
            return _format(f"{BLOCKSCOUT_GROUP}.base", template, {"chain": slug}).rstrip("/")
        return default.rstrip("/") or None

    def blockscout(
        self,
        resource: str,
        *,
        chain: str,
        default: str | None = None,
        **params: Any,
    ) -> str | None:
        """Explorer URL for ``resource`` (``token``/``counters``/``holders``).

        Returns ``None`` for a chain without an explorer, so the caller skips the
        enrichment instead of inventing a host.
        """
        if self.blockscout_url(chain, default) is None:
            return None
        return self.url(f"{BLOCKSCOUT_GROUP}.{resource}", chain=chain, **params)

    # -- internals -----------------------------------------------------------
    def _template(self, name: str) -> str:
        """Base joined with path (or the absolute entry), placeholders intact."""
        template = self.paths[name]
        if _is_absolute(template):
            return template
        group = name.partition(".")[0]
        base = self.bases.get(group)
        if base is None:
            raise ConfigError(
                f"no base URL configured for endpoint vendor {group!r}; "
                f"add {group}.base to the catalogue or set {_base_env_key(group)}"
            )
        return _join(base, template)

    def _resolve(self, name: str, params: Mapping[str, Any]) -> str:
        template = self.paths[name]
        if _is_absolute(template):
            return _format(name, template, params)
        group = name.partition(".")[0]
        return _join(self.base(group, **params), _format(name, template, params))


@lru_cache(maxsize=1)
def bundled_endpoints() -> EndpointSettings:
    """The shipped catalogue with no overrides (also the dataclass default)."""
    return load_endpoint_settings({})


def load_endpoint_settings(
    env: Mapping[str, str], *, default_chain: str = "ethereum"
) -> EndpointSettings:
    """Build :class:`EndpointSettings` from the bundled catalogue plus ``env``."""
    leaves = dict(_bundled_leaves())
    overlay = _get(env, ENDPOINTS_FILE_ENV)
    if overlay:
        leaves.update(_flatten(_read_catalogue(Path(overlay)), source=overlay))

    bases: dict[str, str] = {}
    paths: dict[str, str] = {}
    for name, template in leaves.items():
        group, _, leaf = name.partition(".")
        if leaf == "base":
            bases[group] = template
        else:
            paths[name] = template

    for group in bases:
        override = _get(env, _base_env_key(group))
        if override is None and group == BLOCKSCOUT_GROUP:
            override = _get(env, _BLOCKSCOUT_TEMPLATE_ENV)
        if override:
            bases[group] = override

    for name in paths:
        override = _get(env, _entry_env_key(name))
        if override:
            paths[name] = override

    settings = EndpointSettings(
        bases={group: value.rstrip("/") for group, value in bases.items()},
        paths=paths,
        blockscout_overrides=_blockscout_overrides(env, default_chain=default_chain),
    )
    _validate(settings)
    return settings


def _validate(settings: EndpointSettings) -> None:
    """Fail at boot on a malformed catalogue entry instead of mid-scan."""
    for group, template in settings.bases.items():
        _check_placeholders(f"{group}.base", template)
        _check_absolute(f"{group}.base", _PLACEHOLDER.sub("1", template))
    for name in settings.paths:
        if not settings.has(name):
            continue
        _check_placeholders(name, settings.paths[name])
        _check_absolute(name, _PLACEHOLDER.sub("1", settings._template(name)))


__all__ = [
    "BLOCKSCOUT_CHAINS",
    "CATALOGUE_PATH",
    "ENDPOINTS_FILE_ENV",
    "EndpointSettings",
    "REQUIRED_ENDPOINTS",
    "bundled_endpoints",
    "load_endpoint_settings",
]
