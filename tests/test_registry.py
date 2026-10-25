"""Routing contract: one manifest per asset class, and failures cost one layer.

These tests pin the architecture itself rather than a provider response: the
registry must know all seven markets, crypto must route exactly eight isolated
layers, and a scanner that explodes must never take the scan with it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import scanners
from config.settings import DEFAULT_ENABLED_LAYERS
from core.instruments import AssetClass, InstrumentError
from core.models import Layer, RiskAudit, SecurityReport, SignalStatus, TokenSnapshot, WashTradingReport
from core.scoring import reconcile
from scanners.base import BaseScanner, ScanContext
from scanners.crypto.layer8_security import (
    CryptoSecurityScanner,
    merge_honeypot_report,
    parse_honeypot_check,
)
from scanners.registry import (
    ASSET_LAYER_MAP,
    CRYPTO_SPECS,
    SCANNER_REGISTRY,
    build_scanners,
    contained_signal,
    known_slugs,
    layer_modules,
    run_isolated,
    specs_for,
    validate_enabled,
)

TOKEN = "0x" + "ab" * 20

#: The only modules allowed at the scanner package root: the layer contract, the
#: routing table and the coordinator. Anything that knows about one market lives
#: in that market's package or in ``shared``.
FRAMEWORK_MODULES = {"__init__", "__main__", "base", "pipeline", "registry"}

#: Modules that used to sit at the root and must never be imported from there again.
RETIRED_ROOT_MODULES = (
    "airdrop",
    "derivatives",
    "dex",
    "fundamentals",
    "macro",
    "market",
    "market_data",
    "onchain",
    "security",
    "series",
    "social",
    "technicals",
    "wash",
)

#: Asset class -> the package that owns its dedicated layers.
MARKET_PACKAGES = {
    AssetClass.EQUITY: "scanners.equities",
    AssetClass.ETF: "scanners.etfs",
    AssetClass.INDEX: "scanners.indices",
    AssetClass.FX: "scanners.forex",
    AssetClass.COMMODITY: "scanners.commodities",
    AssetClass.RATE: "scanners.rates",
}


def crypto_token() -> TokenSnapshot:
    return TokenSnapshot(token_address=TOKEN, chain="ethereum", asset_class="crypto", symbol="TEST")


class ExplodingScanner(BaseScanner):
    """Stands in for a layer whose provider client raised past its own guard."""

    layer = Layer.ONCHAIN

    async def collect(self, token: TokenSnapshot, ctx: ScanContext):
        raise RuntimeError("provider exploded")


class OutageScanner(BaseScanner):
    """A layer that degrades honestly: it reports the outage, it does not raise."""

    layer = Layer.SOCIAL

    async def collect(self, token: TokenSnapshot, ctx: ScanContext):
        return self.unavailable(token, "upstream 503")


class TestManifests:
    def test_every_asset_class_is_routed(self):
        assert set(ASSET_LAYER_MAP) == set(AssetClass)

    def test_crypto_routes_exactly_eight_layers_in_order(self):
        slugs = [spec.slug for spec in CRYPTO_SPECS]
        assert slugs == [
            "onchain",
            "derivatives",
            "social",
            "dex",
            "ecosystem",
            "tokenomics",
            "technicals",
            "security",
        ]

    def test_crypto_layers_are_isolated_in_their_own_package(self):
        modules = layer_modules(AssetClass.CRYPTO)
        assert modules and all(module.startswith("scanners.crypto.") for module in modules)

    @pytest.mark.parametrize("asset_class", sorted(MARKET_PACKAGES, key=lambda item: item.value))
    def test_listed_layers_live_in_their_market_package(self, asset_class):
        package = MARKET_PACKAGES[asset_class]
        modules = layer_modules(asset_class)
        assert modules, f"{asset_class.value} has no manifest"
        assert any(module.startswith(package) for module in modules)

    @pytest.mark.parametrize("asset_class", sorted(MARKET_PACKAGES, key=lambda item: item.value))
    def test_class_specific_stubs_survive_a_narrowed_config(self, asset_class):
        stubs = [spec for spec in specs_for(asset_class) if spec.always_on]
        assert stubs, f"{asset_class.value} lost its forward-compatibility stub"
        assert all(spec.module.startswith(MARKET_PACKAGES[asset_class]) for spec in stubs)

    def test_security_is_always_on(self):
        security = next(spec for spec in CRYPTO_SPECS if spec.slug == "security")
        assert security.always_on

    def test_shared_slugs_belong_to_crypto(self):
        assert SCANNER_REGISTRY["technicals"].__module__ == "scanners.crypto.layer7_technicals"
        assert SCANNER_REGISTRY["airdrop"].__module__ == "scanners.crypto.layer5_ecosystem"

    def test_known_slugs_covers_defaults_and_legacy_aliases(self):
        known = known_slugs()
        assert set(DEFAULT_ENABLED_LAYERS) <= known
        assert {"airdrop", "macro"} <= known


class TestBuildScanners:
    def test_crypto_builds_all_eight_layers(self, settings):
        scanners = build_scanners(settings, AssetClass.CRYPTO)
        assert [scanner.layer for scanner in scanners] == [
            Layer.ONCHAIN,
            Layer.DERIVATIVES,
            Layer.SOCIAL,
            Layer.DEX,
            Layer.ECOSYSTEM,
            Layer.TOKENOMICS,
            Layer.TECHNICALS,
            Layer.SECURITY,
        ]

    def test_narrowing_the_config_never_removes_security(self, settings):
        scanners = build_scanners(settings, "crypto", enabled=["onchain"])
        assert {scanner.layer for scanner in scanners} == {Layer.ONCHAIN, Layer.SECURITY}

    def test_crypto_slugs_do_not_leak_into_a_listed_manifest(self, settings):
        modules = {scanner.__class__.__module__ for scanner in build_scanners(settings, "equity")}
        assert all(not module.startswith("scanners.crypto.") for module in modules)

    def test_unknown_layer_fails_loudly(self):
        with pytest.raises(ValueError) as excinfo:
            validate_enabled(["dex", "nope"])
        assert "nope" in str(excinfo.value)

    def test_unknown_asset_class_fails_loudly(self, settings):
        with pytest.raises(InstrumentError):
            build_scanners(settings, "martian")


class TestIsolation:
    async def test_an_exploding_layer_becomes_skipped_with_zero_weight(self, settings):
        signal = await run_isolated(ExplodingScanner(settings), crypto_token(), ScanContext(None, settings))
        assert signal.status is SignalStatus.SKIPPED
        assert signal.weight == 0.0
        assert signal.confidence == 0.0
        assert "provider exploded" in (signal.error or "")

    async def test_a_provider_outage_is_folded_into_skipped(self, settings):
        signal = await run_isolated(OutageScanner(settings), crypto_token(), ScanContext(None, settings))
        assert signal.status is SignalStatus.SKIPPED
        assert signal.weight == 0.0
        assert signal.metrics["original_status"] == "unavailable"

    async def test_a_non_contributing_signal_cannot_carry_weight(self, settings):
        class NotApplicableScanner(BaseScanner):
            layer = Layer.DEX

            async def collect(self, token: TokenSnapshot, ctx: ScanContext):
                # ``run`` stamps the layer weight onto whatever it returns; the
                # router must strip it again because this layer contributes nothing.
                return self.skipped(token, "no tradable pool for this token")

        signal = await run_isolated(NotApplicableScanner(settings), crypto_token(), ScanContext(None, settings))
        assert signal.status is SignalStatus.SKIPPED
        assert signal.weight == 0.0
        assert signal.contributes is False

    def test_contained_signal_is_neutral_but_weightless(self):
        signal = contained_signal(Layer.DEX, RuntimeError("boom"))
        assert signal.score == 50.0
        assert signal.weight == 0.0
        assert signal.contributes is False


class TestSecurityVeto:
    def test_a_layer8_critical_flag_vetoes_a_perfect_score(self, settings):
        report = merge_honeypot_report(
            SecurityReport(available=True),
            parse_honeypot_check(
                {
                    "honeypotResult": {"isHoneypot": True},
                    "simulationResult": {"buyTax": 5.0, "sellTax": 60.0},
                    "summary": {"riskLevel": 5},
                }
            ),
        )
        assert "honeypot" in report.critical_flags()

        decision = reconcile(
            prior_score=99.0,
            alpha_score=99.0,
            risk=RiskAudit(risk_score=5.0),
            security=report,
            wash=WashTradingReport(),
            thresholds=settings.thresholds,
        )
        assert decision.vetoed
        assert decision.position_multiplier == 0.0
        assert any("hard security flags" in reason for reason in decision.veto_reasons)

    async def test_layer8_marks_the_snapshot_for_the_veto(self, settings, monkeypatch):
        async def no_honeypot_api(*args, **kwargs):
            raise RuntimeError("honeypot.is unavailable")

        async def critical_report(token: TokenSnapshot, ctx: ScanContext) -> SecurityReport:
            return SecurityReport(available=True, flags=["honeypot", "hidden_owner"])

        monkeypatch.setattr("scanners.crypto.layer8_security.request_json", no_honeypot_api)
        scanner = CryptoSecurityScanner(settings)
        monkeypatch.setattr(scanner, "_goplus_report", critical_report)

        token = crypto_token()
        signal = await run_isolated(scanner, token, ScanContext(None, settings))

        assert signal.status is SignalStatus.DEGRADED
        assert token.meta["security_veto"] is True
        assert token.security.critical_flags() == ["honeypot", "hidden_owner"]


class TestPackageLayout:
    """The scanner root is framework only; a market owns its own modules."""

    def test_root_holds_only_framework_modules(self):
        root = Path(scanners.__file__).parent
        assert {path.stem for path in root.glob("*.py")} == FRAMEWORK_MODULES

    def test_crypto_owns_eight_layers_plus_provider_adapters(self):
        crypto = Path(scanners.__file__).parent / "crypto"
        assert sorted(path.stem for path in crypto.glob("layer*.py")) == [
            "layer1_onchain",
            "layer2_derivatives",
            "layer3_social",
            "layer4_dex",
            "layer5_ecosystem",
            "layer6_tokenomics",
            "layer7_technicals",
            "layer8_security",
        ]
        providers = {path.stem for path in (crypto / "providers").glob("*.py")} - {"__init__"}
        assert providers == {"airdrop", "derivatives", "dex", "onchain", "security", "social", "wash"}

    def test_shared_owns_the_cross_market_clients(self):
        shared = Path(scanners.__file__).parent / "shared"
        assert {"http_client.py", "technicals.py", "market_data.py", "series.py"} <= {
            path.name for path in shared.iterdir()
        }
        assert {path.stem for path in (shared / "listed").glob("*.py")} - {"__init__"} == {
            "fundamentals",
            "macro",
            "market",
            "technicals",
        }

    def test_every_market_package_ships_a_routed_layer(self):
        root = Path(scanners.__file__).parent
        for asset_class, package in MARKET_PACKAGES.items():
            modules = [
                path.name
                for path in (root / package.split(".")[-1]).glob("*.py")
                if path.stem != "__init__"
            ]
            assert modules, f"{asset_class.value} has no layer module"

    def test_providers_never_import_a_layer(self):
        providers = Path(scanners.__file__).parent / "crypto" / "providers"
        for path in providers.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith("scanners.crypto.layer"), path.name
                    assert not node.module.startswith("scanners.shared.listed"), path.name

    def test_layers_never_import_each_other(self):
        crypto = Path(scanners.__file__).parent / "crypto"
        for path in crypto.glob("layer*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith("scanners.crypto.layer"):
                        assert node.module.endswith(path.stem), f"{path.name} imports {node.module}"

    def test_no_module_imports_a_retired_root_path(self):
        pattern = re.compile(
            rf"^from scanners\.({'|'.join(RETIRED_ROOT_MODULES)}) import",
            re.MULTILINE,
        )
        root = Path(scanners.__file__).parent.parent
        offenders = [
            str(path.relative_to(root))
            for path in root.rglob("*.py")
            if not {".venv", "__pycache__", "build"} & set(path.parts)
            and pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert not offenders, offenders
