"""Pipeline wiring: asset-class routing, gating and snapshot enrichment."""

from __future__ import annotations

import argparse

import pytest

from config.settings import DEFAULT_ENABLED_LAYERS, ScannerSettings, Settings
from core.instruments import AssetClass, InstrumentError
from core.models import Layer, TokenSnapshot
from scanners.base import BaseScanner, ScanContext
from scanners.dex import DexScanner
from scanners.fundamentals import FundamentalsScanner
from scanners.market import MarketScanner
from scanners.market_data import MarketQuote
from scanners.pipeline import SCANNER_REGISTRY, ScanPipeline


def snapshot(asset_class: str = "crypto", **overrides) -> TokenSnapshot:
    if asset_class == "crypto":
        defaults = {"token_address": "0xabc", "chain": "ethereum"}
    else:
        defaults = {"token_address": "AAPL", "chain": "us_equity", "symbol": "AAPL"}
    defaults["asset_class"] = asset_class
    defaults.update(overrides)
    return TokenSnapshot(**defaults)


class TestRegistry:
    def test_every_collection_layer_has_a_scanner(self):
        for layer in Layer.collection_layers():
            assert layer.value in SCANNER_REGISTRY

    def test_market_and_fundamentals_are_on_by_default(self):
        assert "market" in DEFAULT_ENABLED_LAYERS
        assert "fundamentals" in DEFAULT_ENABLED_LAYERS

    def test_unknown_layer_is_rejected(self, settings):
        broken = Settings(
            runtime=settings.runtime,
            llm=settings.llm,
            storage=settings.storage,
            goplus=settings.goplus,
            social=settings.social,
            telegram=settings.telegram,
            scanners=ScannerSettings(
                watchlist_path=settings.scanners.watchlist_path,
                enabled_layers=("dex", "not-a-layer"),
            ),
            thresholds=settings.thresholds,
        )
        with pytest.raises(ValueError):
            ScanPipeline(broken)


class TestGating:
    @pytest.mark.parametrize("asset_class", ["equity", "etf", "index", "fx", "commodity", "rate"])
    def test_crypto_only_scanners_skip_listed_instruments(self, settings, asset_class):
        assert not DexScanner(settings).supports(snapshot(asset_class))

    def test_market_scanner_supports_every_listed_class(self, settings):
        scanner = MarketScanner(settings)
        assert not scanner.supports(snapshot("crypto"))
        for asset_class in ("equity", "etf", "index", "fx", "commodity", "rate"):
            assert scanner.supports(snapshot(asset_class))

    def test_fundamentals_are_equities_only(self, settings):
        scanner = FundamentalsScanner(settings)
        assert scanner.supports(snapshot("equity"))
        assert not scanner.supports(snapshot("etf"))

    async def test_unsupported_run_is_a_zero_weight_skip(self, settings):
        signal = await DexScanner(settings).run(snapshot("equity"), ScanContext(session=None, settings=settings))
        assert signal.status.value == "skipped"
        assert signal.weight == 0.0
        assert signal.confidence == 0.0
        assert "not applicable" in signal.summary

    def test_supports_tolerates_unknown_asset_class(self, settings):
        assert not MarketScanner(settings).supports(snapshot("martian"))

    async def test_missing_price_series_is_a_skip_not_a_failure(self, settings, monkeypatch):
        from scanners.series import SeriesUnavailable
        from scanners.technicals import TechnicalsScanner

        async def boom(token, ctx):
            raise SeriesUnavailable("no pool address available for OHLCV")

        monkeypatch.setattr("scanners.technicals.load_price_series", boom)
        signal = await TechnicalsScanner(settings).collect(snapshot("crypto"), ScanContext(None, settings))
        assert signal.status.value == "skipped"
        assert "no pool" in signal.summary


class TestSnapshotEnrichment:
    def test_apply_quote_fills_market_fields(self):
        token = snapshot("equity")
        quote = MarketQuote(
            symbol="AAPL",
            provider_symbol="AAPL",
            asset_class="equity",
            price=200.0,
            volume=10_000_000.0,
            change_pct=1.5,
            venue="NasdaqGS",
            currency="USD",
            instrument_type="EQUITY",
            name="Apple Inc.",
            fifty_two_week_high=250.0,
            fifty_two_week_low=150.0,
        )
        ScanPipeline._apply_quote(token, quote)
        assert token.price_usd == 200.0
        assert token.volume_24h_usd == 2_000_000_000.0
        assert token.price_change_24h_pct == 1.5
        assert token.name == "Apple Inc."
        assert token.meta["provider_symbol"] == "AAPL"
        assert token.meta["currency"] == "USD"

    def test_apply_quote_keeps_existing_symbol(self):
        token = snapshot("equity", symbol="AAPL")
        ScanPipeline._apply_quote(token, MarketQuote(symbol="AAPL", provider_symbol="AAPL", asset_class="equity"))
        assert token.symbol == "AAPL"
        assert token.price_usd is None


class TestCliTarget:
    def test_equity_target(self, settings):
        from scanners.__main__ import build_target

        args = argparse.Namespace(token="", symbol="aapl", chain=None, asset_class="equity", venue="nyse")
        target = build_target(args, settings)
        assert target.asset_class == "equity"
        assert target.token_address == "AAPL"
        assert target.chain == "nyse"
        assert target.meta["provider_symbol"] == "AAPL"

    def test_crypto_target(self, settings):
        from scanners.__main__ import build_target

        args = argparse.Namespace(token="0xabc", symbol="WETH", chain="base", asset_class="crypto", venue="")
        target = build_target(args, settings)
        assert target.asset_class == "crypto"
        assert target.token_address == "0xabc"
        assert target.chain == "base"

    def test_bad_asset_class_is_loud(self, settings):
        from scanners.__main__ import build_target

        args = argparse.Namespace(token="", symbol="X", chain=None, asset_class="martian", venue="")
        with pytest.raises(InstrumentError):
            build_target(args, settings)


class TestMacroCache:
    async def test_prefetch_fetches_each_market_once(self, settings, monkeypatch):
        calls: list[str] = []

        async def fake_crypto(session):
            calls.append("crypto")
            return {"market_cap_change_24h_pct": 1.5, "btc_dominance": 51.0, "fear_greed": 55.0}

        async def fake_cross(session):
            calls.append("cross_asset")
            return {"vix": {"level": 15.0, "change_5d_pct": -2.0}}

        monkeypatch.setattr("scanners.pipeline.fetch_crypto_backdrop", fake_crypto)
        monkeypatch.setattr("scanners.pipeline.fetch_cross_asset_backdrop", fake_cross)
        pipeline = ScanPipeline(settings)

        await pipeline._prefetch_macro([snapshot("crypto"), snapshot("crypto")])
        await pipeline._prefetch_macro([snapshot("crypto")])
        await pipeline._prefetch_macro([snapshot("equity")])

        assert calls == ["crypto", "cross_asset"]

    async def test_unusable_macro_data_is_not_cached(self, settings, monkeypatch):
        async def empty_crypto(session):
            return {"market_cap_change_24h_pct": None, "btc_dominance": None, "fear_greed": None}

        monkeypatch.setattr("scanners.pipeline.fetch_crypto_backdrop", empty_crypto)
        pipeline = ScanPipeline(settings)

        await pipeline._prefetch_macro([snapshot("crypto")])

        assert pipeline._macro_cache == {}

    async def test_a_stale_entry_is_refetched(self, settings, monkeypatch):
        import time

        from scanners.macro import CRYPTO_CACHE_KEY
        from scanners.pipeline import MACRO_CACHE_TTL_SECONDS

        calls: list[str] = []

        async def fake_crypto(session):
            calls.append("crypto")
            return {"market_cap_change_24h_pct": 2.0}

        monkeypatch.setattr("scanners.pipeline.fetch_crypto_backdrop", fake_crypto)
        pipeline = ScanPipeline(settings)
        pipeline._macro_cache[CRYPTO_CACHE_KEY] = (
            time.monotonic() - MACRO_CACHE_TTL_SECONDS - 1.0,
            {"market_cap_change_24h_pct": 9.0},
        )

        await pipeline._prefetch_macro([snapshot("crypto")])

        assert calls == ["crypto"]
        assert pipeline._macro_cache[CRYPTO_CACHE_KEY][1]["market_cap_change_24h_pct"] == 2.0


class TestBaseScannerContract:
    def test_asset_class_set_is_optional(self, settings):
        class Everything(BaseScanner):
            layer = Layer.MACRO

            async def collect(self, token, ctx):  # pragma: no cover - not called
                return self.signal(token, score=50, confidence=0.5, summary="")

        assert Everything(settings).supports(snapshot("commodity"))

    def test_markets_are_declared_explicitly(self, settings):
        scanner = MarketScanner(settings)
        assert scanner.asset_classes is not None
        assert AssetClass.CRYPTO not in scanner.asset_classes
