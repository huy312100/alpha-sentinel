"""Instrument identity for every market this service can scan.

AlphaSentinel was born as a crypto scanner, but nothing in the scoring, audit or
storage pipeline is crypto-specific: what a new market actually needs is a
canonical identity plus a way to ask a data provider for it. This module owns
both, so scanners never have to guess what a ticker means.

Conventions
-----------
``asset_class``
    What the thing *is* (crypto, equity, ETF, index, FX, commodity future, rate).
``symbol``
    Operator-facing ticker, e.g. ``AAPL``, ``EURUSD``, ``GC``, ``SPX``, ``US10Y``.
    Crypto symbols are display hints only - contracts are identified by address.
``address``
    Contract address for crypto; empty for everything else.
``venue``
    Where it trades: chain slug for crypto (``ethereum``), exchange/bucket slug
    otherwise (``us_equity``, ``fx``, ``futures``, ``rates``, ``index``). The
    storage layer keeps this in its ``chain`` column for backwards compatibility.
``provider_symbol``
    Exactly what a data provider expects (``AAPL``, ``EURUSD=X``, ``GC=F``,
    ``^GSPC``, ``^TNX``). Derived from a table, overridable per watchlist entry.

Identity (``Instrument.identity``) is the contract address for crypto and the
canonical symbol for everything else. That is what lands in ``token_address``,
so a verdict for ``AAPL`` reads ``AAPL`` / ``us_equity`` instead of an empty
address - and the cooldown, history and Parquet dedupe keys keep working.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class InstrumentError(ValueError):
    """Raised when an instrument cannot be normalised into a scannable identity."""


class AssetClass(str, Enum):
    """The markets this pipeline knows how to describe."""

    CRYPTO = "crypto"
    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"
    FX = "fx"
    COMMODITY = "commodity"
    RATE = "rate"

    @property
    def is_crypto(self) -> bool:
        return self is AssetClass.CRYPTO

    @property
    def has_fundamentals(self) -> bool:
        """Only single-company listings have SEC company facts."""
        return self is AssetClass.EQUITY

    @property
    def label(self) -> str:
        return {
            AssetClass.CRYPTO: "crypto token",
            AssetClass.EQUITY: "equity",
            AssetClass.ETF: "ETF",
            AssetClass.INDEX: "index",
            AssetClass.FX: "FX pair",
            AssetClass.COMMODITY: "commodity future",
            AssetClass.RATE: "rate",
        }[self]

    @classmethod
    def coerce(cls, value: Any) -> "AssetClass":
        """Accept the enum, its value, or a friendly alias (``stock``, ``forex``...)."""
        if isinstance(value, cls):
            return value
        raw = str(value or "").strip().lower()
        if raw in _ASSET_CLASS_ALIASES:
            return _ASSET_CLASS_ALIASES[raw]
        try:
            return cls(raw)
        except ValueError as exc:
            raise InstrumentError(
                f"unsupported asset_class: {value!r} "
                f"(known: {', '.join(item.value for item in cls)}, "
                f"aliases: {', '.join(sorted(_ASSET_CLASS_ALIASES))})"
            ) from exc


_ASSET_CLASS_ALIASES: dict[str, AssetClass] = {
    "crypto": AssetClass.CRYPTO,
    "token": AssetClass.CRYPTO,
    "coin": AssetClass.CRYPTO,
    "onchain": AssetClass.CRYPTO,
    "equity": AssetClass.EQUITY,
    "equities": AssetClass.EQUITY,
    "stock": AssetClass.EQUITY,
    "stocks": AssetClass.EQUITY,
    "share": AssetClass.EQUITY,
    "etf": AssetClass.ETF,
    "etfs": AssetClass.ETF,
    "fund": AssetClass.ETF,
    "index": AssetClass.INDEX,
    "indices": AssetClass.INDEX,
    "fx": AssetClass.FX,
    "forex": AssetClass.FX,
    "currency": AssetClass.FX,
    "currencies": AssetClass.FX,
    "commodity": AssetClass.COMMODITY,
    "commodities": AssetClass.COMMODITY,
    "future": AssetClass.COMMODITY,
    "futures": AssetClass.COMMODITY,
    "rate": AssetClass.RATE,
    "rates": AssetClass.RATE,
    "bond": AssetClass.RATE,
    "bonds": AssetClass.RATE,
    "yield": AssetClass.RATE,
}

#: Venue used when a watchlist entry does not say where the instrument trades.
DEFAULT_VENUES: dict[AssetClass, str] = {
    AssetClass.CRYPTO: "ethereum",
    AssetClass.EQUITY: "us_equity",
    AssetClass.ETF: "us_equity",
    AssetClass.INDEX: "index",
    AssetClass.FX: "fx",
    AssetClass.COMMODITY: "futures",
    AssetClass.RATE: "rates",
}

#: Provider (Yahoo Finance) symbols for indices operators ask for by house name.
INDEX_PROVIDER_SYMBOLS: dict[str, str] = {
    "SPX": "^GSPC",
    "GSPC": "^GSPC",
    "SP500": "^GSPC",
    "NDX": "^NDX",
    "NASDAQ100": "^NDX",
    "COMP": "^IXIC",
    "IXIC": "^IXIC",
    "DJI": "^DJI",
    "DOW": "^DJI",
    "RUT": "^RUT",
    "VIX": "^VIX",
    "DAX": "^GDAXI",
    "FTSE": "^FTSE",
    "N225": "^N225",
    "HSI": "^HSI",
    "CAC": "^FCHI",
}

#: Front-month futures contracts (the ones a macro book actually watches).
COMMODITY_PROVIDER_SYMBOLS: dict[str, str] = {
    "GC": "GC=F",
    "GOLD": "GC=F",
    "SI": "SI=F",
    "SILVER": "SI=F",
    "PL": "PL=F",
    "PA": "PA=F",
    "HG": "HG=F",
    "COPPER": "HG=F",
    "CL": "CL=F",
    "WTI": "CL=F",
    "BZ": "BZ=F",
    "BRENT": "BZ=F",
    "NG": "NG=F",
    "ZC": "ZC=F",
    "CORN": "ZC=F",
    "ZW": "ZW=F",
    "WHEAT": "ZW=F",
    "ZS": "ZS=F",
    "SOYBEAN": "ZS=F",
}

#: US Treasury yield indices published by CBOE.
RATE_PROVIDER_SYMBOLS: dict[str, str] = {
    "US3M": "^IRX",
    "IRX": "^IRX",
    "US5Y": "^FVX",
    "FVX": "^FVX",
    "US10Y": "^TNX",
    "TNX": "^TNX",
    "US30Y": "^TYX",
    "TYX": "^TYX",
}

#: Yahoo ``instrumentType`` -> our asset class (used to sanity-check provider data).
YAHOO_INSTRUMENT_TYPES: dict[str, AssetClass] = {
    "EQUITY": AssetClass.EQUITY,
    "ETF": AssetClass.ETF,
    "INDEX": AssetClass.INDEX,
    "CURRENCY": AssetClass.FX,
    "FUTURE": AssetClass.COMMODITY,
    "CRYPTOCURRENCY": AssetClass.CRYPTO,
}

_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,15}$")
_FX_PAIR_RE = re.compile(r"^[A-Z]{6}$")
_PROVIDER_SUFFIXES = ("=X", "=F")


def _clean_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def normalise_symbol(asset_class: AssetClass, symbol: str) -> str:
    """Canonical, upper-case symbol for ``asset_class``; raises when unusable."""
    cleaned = _clean_symbol(symbol).lstrip("^")
    if asset_class is AssetClass.FX:
        # Operators write EUR/USD or EUR-USD; providers want EURUSD=X.
        cleaned = cleaned.replace("/", "").replace("-", "").replace(" ", "")
    # Accept provider syntax (``GC=F``, ``EURUSD=X``, ``BRK-B``) as input.
    for suffix in _PROVIDER_SUFFIXES:
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    if not cleaned:
        raise InstrumentError(f"{asset_class.label} requires a symbol")
    if asset_class is AssetClass.FX:
        if not _FX_PAIR_RE.match(cleaned):
            raise InstrumentError(
                f"invalid FX pair {symbol!r}: expected a six-letter pair such as EURUSD"
            )
        return cleaned
    if not _SYMBOL_RE.match(cleaned):
        raise InstrumentError(f"invalid {asset_class.label} symbol: {symbol!r}")
    return cleaned


def provider_symbol_for(asset_class: AssetClass, symbol: str) -> str:
    """The symbol a market-data provider expects for ``symbol``.

    Crypto is identified by contract address, so this returns ``""`` and callers
    must use the address. Every other class maps through a small table with a
    predictable fallback, so a new ticker works before anyone edits code.
    """
    if asset_class is AssetClass.CRYPTO:
        return ""
    canonical = normalise_symbol(asset_class, symbol)
    if asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
        # Yahoo uses dashes for share classes: BRK.B -> BRK-B.
        return canonical.replace(".", "-")
    if asset_class is AssetClass.INDEX:
        return INDEX_PROVIDER_SYMBOLS.get(canonical, f"^{canonical}")
    if asset_class is AssetClass.FX:
        return f"{canonical}=X"
    if asset_class is AssetClass.COMMODITY:
        return COMMODITY_PROVIDER_SYMBOLS.get(canonical, f"{canonical}=F")
    if asset_class is AssetClass.RATE:
        return RATE_PROVIDER_SYMBOLS.get(canonical, f"^{canonical}")
    raise InstrumentError(f"no provider mapping for asset_class {asset_class.value!r}")


def default_venue(asset_class: AssetClass, chain: str = "") -> str:
    """Venue slug for an instrument: the chain for crypto, a bucket otherwise."""
    if asset_class is AssetClass.CRYPTO:
        return (chain or DEFAULT_VENUES[AssetClass.CRYPTO]).strip().lower()
    return DEFAULT_VENUES[asset_class]


def asset_class_for_yahoo_type(instrument_type: Any) -> AssetClass | None:
    """Map Yahoo's ``instrumentType`` onto our taxonomy (``None`` when unknown)."""
    return YAHOO_INSTRUMENT_TYPES.get(str(instrument_type or "").strip().upper())


@dataclass(frozen=True, slots=True)
class Instrument:
    """A normalised, scannable instrument."""

    asset_class: AssetClass
    symbol: str
    venue: str = ""
    address: str = ""
    provider_symbol: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.asset_class, AssetClass):
            object.__setattr__(self, "asset_class", AssetClass.coerce(self.asset_class))

    @property
    def identity(self) -> str:
        """Contract address for crypto, canonical symbol for every other market."""
        return self.address if self.asset_class.is_crypto else self.symbol

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.asset_class.value, self.venue, self.identity)

    @property
    def label(self) -> str:
        return self.symbol or (self.address[:10] if self.address else "UNKNOWN")

    @property
    def market_label(self) -> str:
        return f"{self.label} ({self.asset_class.label})"

    def to_meta(self) -> dict[str, Any]:
        """Scanner-facing metadata; ``provider_symbol`` is what market data uses."""
        return {
            "asset_class": self.asset_class.value,
            "venue": self.venue,
            "provider_symbol": self.provider_symbol,
            "instrument_name": self.name,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_class": self.asset_class.value,
            "symbol": self.symbol,
            "venue": self.venue,
            "address": self.address,
            "provider_symbol": self.provider_symbol,
            "name": self.name,
        }

    @classmethod
    def build(
        cls,
        asset_class: Any,
        *,
        symbol: str = "",
        address: str = "",
        chain: str = "",
        venue: str = "",
        provider_symbol: str = "",
        name: str = "",
    ) -> "Instrument":
        """Validate config input and derive the provider symbol.

        Crypto addresses are validated by :mod:`bot.watchlist` (which also owns
        the chain registry); this method only guarantees that non-crypto
        instruments carry a usable symbol.
        """
        kind = AssetClass.coerce(asset_class)
        if kind is AssetClass.CRYPTO:
            if not address:
                raise InstrumentError("crypto instruments require a contract address")
            return cls(
                asset_class=kind,
                symbol=str(symbol or "").strip(),
                venue=default_venue(kind, chain),
                address=address,
                provider_symbol="",
                name=name,
            )
        canonical = normalise_symbol(kind, symbol)
        return cls(
            asset_class=kind,
            symbol=canonical,
            venue=(venue or default_venue(kind)).strip().lower(),
            address="",
            provider_symbol=provider_symbol or provider_symbol_for(kind, canonical),
            name=name,
        )


def provider_symbol_for_snapshot(snapshot: Any) -> str:
    """Resolve the provider symbol for a :class:`core.models.TokenSnapshot`."""
    meta = getattr(snapshot, "meta", None) or {}
    explicit = str(meta.get("provider_symbol") or "").strip()
    if explicit:
        return explicit
    kind = AssetClass.coerce(getattr(snapshot, "asset_class", AssetClass.CRYPTO.value))
    if kind is AssetClass.CRYPTO:
        return ""
    symbol = getattr(snapshot, "symbol", "") or getattr(snapshot, "token_address", "")
    return provider_symbol_for(kind, symbol)


__all__ = [
    "AssetClass",
    "COMMODITY_PROVIDER_SYMBOLS",
    "DEFAULT_VENUES",
    "INDEX_PROVIDER_SYMBOLS",
    "Instrument",
    "InstrumentError",
    "RATE_PROVIDER_SYMBOLS",
    "YAHOO_INSTRUMENT_TYPES",
    "asset_class_for_yahoo_type",
    "default_venue",
    "normalise_symbol",
    "provider_symbol_for",
    "provider_symbol_for_snapshot",
]
