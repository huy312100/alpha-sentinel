"""Asset-class routing: one registry, seven markets, isolated execution.

``ASSET_LAYER_MAP`` is the single source of truth for *which* scanners run for a
given instrument. The pipeline never imports a scanner directly; it asks this
module for the manifest of ``token.asset_class`` and runs each entry through
:func:`run_isolated`, which converts any exception or provider outage into a
zero-weight ``SKIPPED`` signal so one dead API cannot abort a scan.

Adding a market means adding its package's ``*_LAYERS`` tuple to the map - the
pipeline, the scoring engine and the storage schema stay untouched. Every member
of :class:`~core.instruments.AssetClass` must have a route; the map is validated
at import time so a half-wired asset class fails loudly at boot, not silently
during a scan.

Layout::

    scanners/crypto/          eight dedicated crypto layers
    scanners/equities/        venue + SEC EDGAR (+ ownership stub)
    scanners/etfs/            venue + creation/redemption flows stub
    scanners/indices/         venue + constituents/breadth stub
    scanners/forex/           venue + carry/positioning stub
    scanners/commodities/     venue + inventories/curve stub
    scanners/rates/           venue + curve/auctions stub
    scanners/shared/          HTTP client and indicator maths used by every layer
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from config.settings import LEGACY_LAYER_ALIASES, Settings
from core.instruments import AssetClass
from core.logging import get_logger
from core.models import Layer, LayerSignal, SignalStatus, TokenSnapshot
from scanners.base import BaseScanner, ScanContext, StubScanner
from scanners.commodities.inventories import COMMODITY_LAYERS, CommodityInventoryScanner
from scanners.crypto.layer1_onchain import CryptoOnChainScanner
from scanners.crypto.layer2_derivatives import CryptoDerivativesScanner
from scanners.crypto.layer3_social import CryptoSocialScanner
from scanners.crypto.layer4_dex import CryptoDexScanner
from scanners.crypto.layer5_ecosystem import CryptoEcosystemScanner
from scanners.crypto.layer6_tokenomics import CryptoTokenomicsScanner
from scanners.crypto.layer7_technicals import CryptoTechnicalsScanner
from scanners.crypto.layer8_security import CryptoSecurityScanner
from scanners.equities.edgar import EQUITY_LAYERS, EquityOwnershipScanner
from scanners.etfs.flows import ETF_LAYERS, EtfFlowsScanner
from scanners.forex.carry import FX_LAYERS, FxCarryScanner
from scanners.indices.breadth import INDEX_LAYERS, IndexBreadthScanner
from scanners.rates.curve import RATE_LAYERS, RatesCurveScanner
from scanners.shared.listed.fundamentals import FundamentalsScanner
from scanners.shared.listed.macro import MacroScanner
from scanners.shared.listed.market import MarketScanner
from scanners.shared.listed.technicals import TechnicalsScanner


log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScannerSpec:
    """One routable layer: config slug, implementation, class and module path."""

    slug: str
    scanner: type[BaseScanner]
    asset_class: AssetClass
    module: str
    aliases: tuple[str, ...] = ()
    #: Veto-bearing or forward-compatibility layers run even when the operator
    #: narrows ``ENABLED_LAYERS``: security must never be switched off silently.
    always_on: bool = False

    @property
    def layer(self) -> Layer:
        return self.scanner.layer

    def enabled_by(self, enabled: frozenset[str]) -> bool:
        if self.always_on:
            return True
        if self.slug in enabled:
            return True
        return any(alias in enabled for alias in self.aliases)


# ---------------------------------------------------------------------------
# Crypto: eight dedicated layers
# ---------------------------------------------------------------------------
CRYPTO_SPECS: tuple[ScannerSpec, ...] = (
    ScannerSpec("onchain", CryptoOnChainScanner, AssetClass.CRYPTO, "scanners.crypto.layer1_onchain"),
    ScannerSpec("derivatives", CryptoDerivativesScanner, AssetClass.CRYPTO, "scanners.crypto.layer2_derivatives"),
    ScannerSpec("social", CryptoSocialScanner, AssetClass.CRYPTO, "scanners.crypto.layer3_social"),
    ScannerSpec("dex", CryptoDexScanner, AssetClass.CRYPTO, "scanners.crypto.layer4_dex"),
    ScannerSpec(
        "ecosystem",
        CryptoEcosystemScanner,
        AssetClass.CRYPTO,
        "scanners.crypto.layer5_ecosystem",
        aliases=("airdrop",),
    ),
    ScannerSpec(
        "tokenomics",
        CryptoTokenomicsScanner,
        AssetClass.CRYPTO,
        "scanners.crypto.layer6_tokenomics",
        aliases=("macro",),
    ),
    ScannerSpec("technicals", CryptoTechnicalsScanner, AssetClass.CRYPTO, "scanners.crypto.layer7_technicals"),
    ScannerSpec(
        "security",
        CryptoSecurityScanner,
        AssetClass.CRYPTO,
        "scanners.crypto.layer8_security",
        always_on=True,
    ),
)


#: Config slug for every scanner class that appears in a market manifest.
_SLUG_BY_CLASS: dict[type[BaseScanner], str] = {
    MarketScanner: "market",
    FundamentalsScanner: "fundamentals",
    MacroScanner: "macro",
    TechnicalsScanner: "technicals",
    EquityOwnershipScanner: "equity_ownership",
    EtfFlowsScanner: "etf_flows",
    IndexBreadthScanner: "index_breadth",
    FxCarryScanner: "fx_carry",
    CommodityInventoryScanner: "commodity_inventory",
    RatesCurveScanner: "rates_curve",
}


def _market_specs(asset_class: AssetClass, layers: Sequence[type[BaseScanner]]) -> tuple[ScannerSpec, ...]:
    """Build specs for one listed market, marking class-specific stubs ``always_on``.

    Stubs stay on regardless of ``ENABLED_LAYERS`` because their only output is a
    zero-weight ``SKIPPED``: gating them would change nothing except to hide the
    fact that the market's dedicated data is not wired yet.
    """
    specs: list[ScannerSpec] = []
    for scanner in layers:
        slug = _SLUG_BY_CLASS.get(scanner)
        if slug is None:  # pragma: no cover - guards a future unregistered scanner
            raise RuntimeError(f"no registry slug configured for {scanner.__name__}")
        specs.append(
            ScannerSpec(
                slug=slug,
                scanner=scanner,
                asset_class=asset_class,
                module=scanner.__module__,
                always_on=issubclass(scanner, StubScanner),
            )
        )
    return tuple(specs)


ASSET_LAYER_MAP: dict[AssetClass, tuple[ScannerSpec, ...]] = {
    AssetClass.CRYPTO: CRYPTO_SPECS,
    AssetClass.EQUITY: _market_specs(AssetClass.EQUITY, EQUITY_LAYERS),
    AssetClass.ETF: _market_specs(AssetClass.ETF, ETF_LAYERS),
    AssetClass.INDEX: _market_specs(AssetClass.INDEX, INDEX_LAYERS),
    AssetClass.FX: _market_specs(AssetClass.FX, FX_LAYERS),
    AssetClass.COMMODITY: _market_specs(AssetClass.COMMODITY, COMMODITY_LAYERS),
    AssetClass.RATE: _market_specs(AssetClass.RATE, RATE_LAYERS),
}


def _assert_every_class_is_routed() -> None:
    """Fail at import when an asset class has no manifest.

    Forward compatibility is a promise: a new ``AssetClass`` member that nobody
    routed would otherwise scan as an empty snapshot and score a neutral 50.
    """
    missing = [kind.value for kind in AssetClass if not ASSET_LAYER_MAP.get(kind)]
    if missing:
        raise RuntimeError(f"asset class(es) without a scanner manifest: {missing}")


_assert_every_class_is_routed()


#: Backwards-compatible slug -> scanner view (tests, dashboards, old callers).
#: First route wins, so crypto owns the shared slugs (``technicals``).
SCANNER_REGISTRY: dict[str, type[BaseScanner]] = {}
for _specs in ASSET_LAYER_MAP.values():
    for _spec in _specs:
        SCANNER_REGISTRY.setdefault(_spec.slug, _spec.scanner)


#: Legacy slug from the pre-8-layer config; same implementation as ``ecosystem``.
SCANNER_REGISTRY["airdrop"] = CryptoEcosystemScanner


def specs_for(asset_class: AssetClass | str) -> tuple[ScannerSpec, ...]:
    """Return the ordered layer manifest for one asset class."""
    kind = AssetClass.coerce(asset_class)
    return ASSET_LAYER_MAP[kind]


def layer_modules(asset_class: AssetClass | str) -> tuple[str, ...]:
    """Module paths behind one manifest - useful for boot logs and docs."""
    return tuple(spec.module for spec in specs_for(asset_class))


def known_slugs() -> frozenset[str]:
    """Every accepted ``ENABLED_LAYERS`` entry, including legacy aliases."""
    names: set[str] = set(LEGACY_LAYER_ALIASES)
    for specs in ASSET_LAYER_MAP.values():
        for spec in specs:
            names.add(spec.slug)
            names.update(spec.aliases)
    return frozenset(names)


def validate_enabled(enabled: Iterable[str]) -> frozenset[str]:
    """Normalise and validate configured layer names; unknown names fail loudly."""
    names = frozenset(str(name).strip().lower() for name in enabled if str(name).strip())
    unknown = sorted(names - known_slugs())
    if unknown:
        raise ValueError(f"unknown scanner layer(s): {unknown}; known: {sorted(known_slugs())}")
    return names


def build_scanners(
    settings: Settings,
    asset_class: AssetClass | str,
    *,
    enabled: Iterable[str] | None = None,
) -> list[BaseScanner]:
    """Instantiate the enabled scanners for ``asset_class``, in manifest order.

    Layers not applicable to the class are simply absent - a crypto-only slug in
    ``ENABLED_LAYERS`` never breaks an equity scan. Security (crypto) and the
    market stubs are ``always_on`` so a narrowed config cannot switch them off.
    """
    kind = AssetClass.coerce(asset_class)
    names = validate_enabled(enabled if enabled is not None else settings.scanners.enabled_layers)
    return [spec.scanner(settings) for spec in ASSET_LAYER_MAP[kind] if spec.enabled_by(names)]


def contained_signal(layer: Layer, exc: BaseException) -> LayerSignal:
    """Fail-closed signal for an exception no scanner managed to contain."""
    message = f"{type(exc).__name__}: {str(exc)[:200]}"
    log.error("layer_contained", extra={"layer": layer.value, "error": message})
    return LayerSignal(
        layer=layer,
        score=50.0,
        confidence=0.0,
        weight=0.0,
        status=SignalStatus.SKIPPED,
        summary=f"{layer.value} skipped: provider failure",
        metrics={"original_status": "unavailable"},
        error=message,
    )


async def run_isolated(scanner: BaseScanner, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:
    """Run one layer so a failure costs exactly that layer and nothing else.

    * an exception becomes ``SKIPPED`` with weight 0 (never a neutral 50);
    * a provider outage (``UNAVAILABLE``) is folded into ``SKIPPED`` with the
      original error retained, so ``layer_status`` reads as skipped and the
      weighted prior simply renormalises the surviving layers;
    * a non-contributing signal can never carry weight into the prior.
    """
    try:
        signal = await scanner.run(token, ctx)
    except Exception as exc:  # noqa: BLE001 - this is the isolation boundary
        return contained_signal(scanner.layer, exc)

    if signal.status is SignalStatus.UNAVAILABLE:
        signal.status = SignalStatus.SKIPPED
        signal.weight = 0.0
        signal.metrics.setdefault("original_status", "unavailable")
        signal.summary = f"{signal.layer.value} skipped: {signal.error or 'provider unavailable'}"[:500]
        return signal
    if not signal.contributes:
        signal.weight = 0.0
    return signal


__all__ = [
    "ASSET_LAYER_MAP",
    "CRYPTO_SPECS",
    "SCANNER_REGISTRY",
    "ScannerSpec",
    "build_scanners",
    "contained_signal",
    "known_slugs",
    "layer_modules",
    "run_isolated",
    "specs_for",
    "validate_enabled",
]
