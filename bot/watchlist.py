"""Watchlist parsing and hot-reloading.

The scanner never invents its own universe: everything it looks at comes from
``config/watchlists.yaml`` so an operator can add or retire an instrument with a
text edit instead of a deploy. Parsing is strict and fails at *load* time - a typo
in a contract address should be a loud error at boot, not 40 silently dead tokens
at 03:00.

Two identities are supported and every entry must resolve to exactly one:

* **crypto** - a contract ``address`` plus a ``chain`` (tickers collide, contracts
  do not);
* **anything else** - a ``symbol`` plus an ``asset_class`` (equity, ETF, index,
  fx, commodity, rate). Provider syntax is derived, not typed by the operator.

``token_address`` keeps its historical name but holds the *identity*: the contract
for crypto, the canonical symbol otherwise. ``chain`` holds the venue slug
(``ethereum``, ``us_equity``, ``fx``...), so cooldown, history and Parquet dedupe
keys work unchanged for every market.

The file is re-read when its ``mtime`` changes, which makes the daemon
self-configuring without a restart.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from config.settings import PROJECT_ROOT
from core.instruments import (
    AssetClass,
    InstrumentError,
    default_venue,
    normalise_symbol,
    provider_symbol_for,
)
from core.logging import get_logger
from core.models import TokenSnapshot, get_chain_profile

log = get_logger(__name__)

DEFAULT_WATCHLIST_PATH = PROJECT_ROOT / "config" / "watchlists.yaml"
DEFAULT_COOLDOWN_HOURS = 6.0
DEFAULT_PRIORITY = 100
DEFAULT_MAX_TOKENS_PER_CYCLE = 60

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
#: Solana public keys are base58 and case-sensitive, 32-44 characters.
_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class WatchlistError(ValueError):
    """Raised when the watchlist file cannot be parsed into usable tokens."""


def is_evm_address(value: str) -> bool:
    return bool(_EVM_ADDRESS.match(value or ""))


def is_solana_address(value: str) -> bool:
    return bool(_SOLANA_ADDRESS.match(value or ""))


def validate_address(address: str, chain: str) -> str:
    """Validate/normalise an address for ``chain``; raises :class:`WatchlistError`."""
    cleaned = (address or "").strip()
    if not cleaned:
        raise WatchlistError("token address is empty")
    if chain == "solana":
        if not is_solana_address(cleaned):
            raise WatchlistError(f"invalid Solana address: {cleaned!r}")
        return cleaned
    if not is_evm_address(cleaned):
        raise WatchlistError(f"invalid EVM address for chain {chain!r}: {cleaned!r}")
    # EVM addresses are case-insensitive; one canonical form avoids duplicate
    # rows in SQLite and Parquet.
    return cleaned.lower()


def normalise_chain(chain: str) -> str:
    slug = (chain or "").strip().lower()
    try:
        get_chain_profile(slug)
    except ValueError as exc:
        raise WatchlistError(str(exc)) from exc
    return slug


@dataclass(frozen=True, slots=True)
class WatchToken:
    """One scan target, as configured."""

    token_address: str
    chain: str = "ethereum"
    asset_class: AssetClass = AssetClass.CRYPTO
    provider_symbol: str = ""
    symbol: str = ""
    tags: tuple[str, ...] = ()
    priority: int = DEFAULT_PRIORITY
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS

    @property
    def key(self) -> tuple[str, str, str]:
        """Asset class + identity + venue: the cooldown/dedupe key everywhere."""
        return (self.asset_class.value, self.token_address, self.chain)

    @property
    def label(self) -> str:
        return self.symbol or self.token_address[:10]

    @property
    def is_crypto(self) -> bool:
        return self.asset_class is AssetClass.CRYPTO

    def to_snapshot(self) -> TokenSnapshot:
        return TokenSnapshot(
            token_address=self.token_address,
            chain=self.chain,
            asset_class=self.asset_class.value,
            symbol=self.symbol,
            meta={
                "tags": list(self.tags),
                "watchlist_priority": self.priority,
                "asset_class": self.asset_class.value,
                "venue": self.chain,
                "provider_symbol": self.provider_symbol,
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_address": self.token_address,
            "chain": self.chain,
            "asset_class": self.asset_class.value,
            "provider_symbol": self.provider_symbol,
            "symbol": self.symbol,
            "tags": list(self.tags),
            "priority": self.priority,
            "cooldown_hours": self.cooldown_hours,
        }


@dataclass(frozen=True, slots=True)
class Watchlist:
    name: str
    tokens: tuple[WatchToken, ...] = ()
    description: str = ""

    def __len__(self) -> int:
        return len(self.tokens)


@dataclass(frozen=True, slots=True)
class WatchlistBundle:
    """Every configured watchlist, flattened on demand."""

    watchlists: tuple[Watchlist, ...] = ()
    max_tokens_per_cycle: int = DEFAULT_MAX_TOKENS_PER_CYCLE
    source: str = ""
    version: int = 1
    meta: Mapping[str, Any] = field(default_factory=dict)

    def names(self) -> list[str]:
        return [watchlist.name for watchlist in self.watchlists]

    def get(self, name: str) -> Watchlist:
        for watchlist in self.watchlists:
            if watchlist.name == name:
                return watchlist
        raise WatchlistError(f"unknown watchlist {name!r} (known: {self.names()})")

    def tokens(self, names: Sequence[str] | None = None) -> list[WatchToken]:
        """Flatten (and de-duplicate) the selected watchlists, best priority first."""
        selected = [self.get(name) for name in names] if names else list(self.watchlists)
        seen: set[tuple[str, str]] = set()
        merged: list[WatchToken] = []
        for watchlist in selected:
            for token in watchlist.tokens:
                if token.key in seen:
                    continue
                seen.add(token.key)
                merged.append(token)
        return sorted(
            merged, key=lambda token: (token.priority, token.chain, token.symbol or token.token_address)
        )

    def capped(self, names: Sequence[str] | None = None, limit: int | None = None) -> list[WatchToken]:
        cap = max(0, limit if limit is not None else self.max_tokens_per_cycle)
        return self.tokens(names)[:cap]

    def __len__(self) -> int:
        return len({token.key for watchlist in self.watchlists for token in watchlist.tokens})

    def __iter__(self) -> Iterable[WatchToken]:
        return iter(self.tokens())


def _coerce_float(value: Any, *, default: float, label: str) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise WatchlistError(f"{label} must be a number, got {value!r}") from exc
    if parsed < 0:
        raise WatchlistError(f"{label} must be >= 0, got {parsed}")
    return parsed


def _coerce_int(value: Any, *, default: int, label: str) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise WatchlistError(f"{label} must be an integer, got {value!r}") from exc


def _coerce_tags(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip().lower() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip().lower() for item in value if str(item).strip())
    raise WatchlistError(f"tags must be a list or comma-separated string, got {type(value).__name__}")


def _normalise_bare_entry(entry: str) -> Mapping[str, Any]:
    """A bare string is an address when it looks like one, a symbol otherwise."""
    cleaned = entry.strip()
    if is_evm_address(cleaned) or is_solana_address(cleaned):
        return {"address": cleaned}
    return {"symbol": cleaned}


def _resolve_asset_class(entry: Mapping[str, Any], defaults: Mapping[str, Any], where: str) -> AssetClass:
    raw = (
        entry.get("asset_class")
        or entry.get("market")
        or defaults.get("asset_class")
        or defaults.get("market")
        or "crypto"
    )
    try:
        return AssetClass.coerce(raw)
    except InstrumentError as exc:
        raise WatchlistError(f"{where}: {exc}") from exc


def _parse_token(entry: Any, *, defaults: Mapping[str, Any], where: str) -> WatchToken:
    if isinstance(entry, str):
        entry = _normalise_bare_entry(entry)
    if not isinstance(entry, Mapping):
        raise WatchlistError(f"{where}: each instrument must be a mapping or an address/ticker string")

    asset_class = _resolve_asset_class(entry, defaults, where)
    symbol = str(entry.get("symbol") or entry.get("ticker") or "").strip()
    provider_symbol = str(entry.get("provider_symbol") or "").strip()

    if asset_class.is_crypto:
        address = entry.get("address") or entry.get("token_address") or ""
        chain = normalise_chain(str(entry.get("chain") or defaults.get("chain") or "ethereum"))
        token_address = validate_address(str(address), chain)
        provider_symbol = ""
    else:
        raw_symbol = symbol or entry.get("address") or entry.get("token_address") or ""
        try:
            symbol = normalise_symbol(asset_class, str(raw_symbol))
            provider_symbol = provider_symbol or provider_symbol_for(asset_class, symbol)
        except InstrumentError as exc:
            raise WatchlistError(f"{where}: {exc}") from exc
        venue = str(entry.get("venue") or defaults.get("venue") or "").strip().lower()
        chain = venue or default_venue(asset_class)
        token_address = symbol

    return WatchToken(
        token_address=token_address,
        chain=chain,
        asset_class=asset_class,
        provider_symbol=provider_symbol,
        symbol=symbol,
        tags=_coerce_tags(entry.get("tags")),
        priority=_coerce_int(
            entry.get("priority", defaults.get("priority")),
            default=DEFAULT_PRIORITY,
            label=f"{where}.priority",
        ),
        cooldown_hours=_coerce_float(
            entry.get("cooldown_hours", defaults.get("cooldown_hours")),
            default=DEFAULT_COOLDOWN_HOURS,
            label=f"{where}.cooldown_hours",
        ),
    )


def _build_watchlist(name: str, section: Any, defaults: Mapping[str, Any], source: str) -> Watchlist:
    """Normalise one watchlist section (mapping or bare token list)."""
    if isinstance(section, list):
        section = {"tokens": section}
    if not isinstance(section, Mapping):
        raise WatchlistError(f"{source}: watchlist {name!r} must be a mapping or a list")

    raw_tokens = section.get("tokens")
    if raw_tokens is None:
        raw_tokens = section.get("instruments") or []
    if not isinstance(raw_tokens, list):
        raise WatchlistError(f"{source}: watchlist {name!r} 'tokens'/'instruments' must be a list")

    # Section-level defaults let a whole watchlist declare its market once:
    #   equities: {asset_class: equity, venue: us_equity, instruments: [AAPL, MSFT]}
    section_defaults = dict(defaults)
    for key in ("asset_class", "market", "venue", "chain", "priority", "cooldown_hours"):
        if key in section:
            section_defaults[key] = section[key]

    tokens = tuple(
        _parse_token(entry, defaults=section_defaults, where=f"{source}:{name}[{index}]")
        for index, entry in enumerate(raw_tokens)
    )
    return Watchlist(
        name=name,
        tokens=tokens,
        description=str(section.get("description") or "").strip(),
    )


def parse_watchlists(payload: Any, *, source: str = "<memory>") -> WatchlistBundle:
    """Turn a decoded YAML document into a :class:`WatchlistBundle`."""
    if payload is None:
        raise WatchlistError(f"{source}: file is empty")
    if not isinstance(payload, Mapping):
        raise WatchlistError(f"{source}: top-level structure must be a mapping")

    defaults = payload.get("defaults") or {}
    if not isinstance(defaults, Mapping):
        raise WatchlistError(f"{source}: 'defaults' must be a mapping")

    settings_section = payload.get("settings") or {}
    if not isinstance(settings_section, Mapping):
        raise WatchlistError(f"{source}: 'settings' must be a mapping")

    raw_lists = payload.get("watchlists")
    if not isinstance(raw_lists, Mapping) or not raw_lists:
        raise WatchlistError(f"{source}: 'watchlists' must be a non-empty mapping")

    watchlists: list[Watchlist] = []
    seen_names: set[str] = set()
    for name, section in raw_lists.items():
        slug = str(name).strip().lower()
        if not slug:
            raise WatchlistError(f"{source}: watchlist name cannot be empty")
        if slug in seen_names:
            raise WatchlistError(f"{source}: duplicate watchlist name {slug!r}")
        seen_names.add(slug)
        watchlists.append(_build_watchlist(slug, section, defaults, source))

    cap = _coerce_int(
        settings_section.get("max_tokens_per_cycle"),
        default=DEFAULT_MAX_TOKENS_PER_CYCLE,
        label="settings.max_tokens_per_cycle",
    )
    if cap <= 0:
        raise WatchlistError("settings.max_tokens_per_cycle must be > 0")

    bundle = WatchlistBundle(
        watchlists=tuple(watchlists),
        max_tokens_per_cycle=cap,
        source=source,
        version=_coerce_int(payload.get("version"), default=1, label="version"),
        meta=dict(defaults),
    )
    log.debug(
        "watchlists_parsed",
        extra={"source": source, "lists": bundle.names(), "tokens": len(bundle), "cap": cap},
    )
    return bundle


def load_watchlists(
    path: str | Path = DEFAULT_WATCHLIST_PATH, *, default_chain: str = "ethereum"
) -> WatchlistBundle:
    """Read and validate ``path``. A missing file is an operator error, not a default."""
    file_path = Path(path).expanduser()
    if not file_path.is_absolute():
        file_path = (PROJECT_ROOT / file_path).resolve()
    if not file_path.exists():
        raise WatchlistError(f"watchlist file not found: {file_path}")

    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise WatchlistError(f"{file_path}: invalid YAML - {exc}") from exc

    if isinstance(raw, Mapping):
        defaults = raw.get("defaults")
        merged = dict(defaults) if isinstance(defaults, Mapping) else {}
        merged.setdefault("chain", default_chain)
        raw = {**raw, "defaults": merged}
    return parse_watchlists(raw, source=str(file_path))


class WatchlistLoader:
    """Caches a parsed bundle and reloads it when the file changes on disk."""

    def __init__(
        self, path: str | Path = DEFAULT_WATCHLIST_PATH, *, default_chain: str = "ethereum"
    ) -> None:
        self.path = Path(path).expanduser()
        if not self.path.is_absolute():
            self.path = (PROJECT_ROOT / self.path).resolve()
        self.default_chain = default_chain
        self._bundle: WatchlistBundle | None = None
        self._mtime: float | None = None
        self._lock = threading.Lock()

    def _current_mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def load(self, *, force: bool = False) -> WatchlistBundle:
        with self._lock:
            mtime = self._current_mtime()
            if not force and self._bundle is not None and mtime is not None and mtime == self._mtime:
                return self._bundle
            bundle = load_watchlists(self.path, default_chain=self.default_chain)
            if self._bundle is not None:
                log.info(
                    "watchlists_reloaded",
                    extra={"source": bundle.source, "tokens": len(bundle), "cap": bundle.max_tokens_per_cycle},
                )
            self._bundle = bundle
            self._mtime = mtime
            return bundle


__all__ = [
    "DEFAULT_COOLDOWN_HOURS",
    "DEFAULT_MAX_TOKENS_PER_CYCLE",
    "DEFAULT_PRIORITY",
    "DEFAULT_WATCHLIST_PATH",
    "WatchToken",
    "Watchlist",
    "WatchlistBundle",
    "WatchlistError",
    "WatchlistLoader",
    "is_evm_address",
    "is_solana_address",
    "load_watchlists",
    "normalise_chain",
    "parse_watchlists",
    "validate_address",
]
