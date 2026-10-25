"""Instrument identity is the contract every market shares: it must be exact."""

from __future__ import annotations

import pytest

from core.instruments import (
    AssetClass,
    Instrument,
    InstrumentError,
    asset_class_for_yahoo_type,
    default_venue,
    normalise_symbol,
    provider_symbol_for,
    provider_symbol_for_snapshot,
)
from core.models import TokenSnapshot


class TestAssetClass:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("crypto", AssetClass.CRYPTO),
            ("token", AssetClass.CRYPTO),
            ("STOCKS", AssetClass.EQUITY),
            ("share", AssetClass.EQUITY),
            ("fund", AssetClass.ETF),
            ("indices", AssetClass.INDEX),
            ("forex", AssetClass.FX),
            ("currencies", AssetClass.FX),
            ("futures", AssetClass.COMMODITY),
            ("bonds", AssetClass.RATE),
            (AssetClass.FX, AssetClass.FX),
        ],
    )
    def test_coerce_aliases(self, raw, expected):
        assert AssetClass.coerce(raw) is expected

    def test_coerce_rejects_garbage_with_helpful_message(self):
        with pytest.raises(InstrumentError) as excinfo:
            AssetClass.coerce("nft-jpegs")
        message = str(excinfo.value)
        assert "unsupported asset_class" in message
        assert "equity" in message  # lists the valid values

    def test_flags(self):
        assert AssetClass.CRYPTO.is_crypto
        assert not AssetClass.EQUITY.is_crypto
        assert AssetClass.EQUITY.has_fundamentals
        assert not AssetClass.ETF.has_fundamentals
        assert "FX" in AssetClass.FX.label


class TestSymbolNormalisation:
    @pytest.mark.parametrize(
        ("asset_class", "raw", "expected"),
        [
            (AssetClass.EQUITY, " aapl ", "AAPL"),
            (AssetClass.EQUITY, "BRK-B", "BRK-B"),
            (AssetClass.ETF, "spy", "SPY"),
            (AssetClass.FX, "eur/usd", "EURUSD"),
            (AssetClass.FX, "EURUSD=X", "EURUSD"),
            (AssetClass.COMMODITY, "gc=f", "GC"),
            (AssetClass.INDEX, "^gspc", "GSPC"),
            (AssetClass.RATE, "^TNX", "TNX"),
        ],
    )
    def test_normalises(self, asset_class, raw, expected):
        assert normalise_symbol(asset_class, raw) == expected

    @pytest.mark.parametrize(
        ("asset_class", "raw"),
        [
            (AssetClass.EQUITY, ""),
            (AssetClass.EQUITY, "AAPL INC"),
            (AssetClass.FX, "EUR"),
            (AssetClass.FX, "EURUS"),
        ],
    )
    def test_rejects_unusable(self, asset_class, raw):
        with pytest.raises(InstrumentError):
            normalise_symbol(asset_class, raw)


class TestProviderSymbols:
    @pytest.mark.parametrize(
        ("asset_class", "symbol", "expected"),
        [
            (AssetClass.EQUITY, "AAPL", "AAPL"),
            (AssetClass.EQUITY, "BRK.B", "BRK-B"),
            (AssetClass.ETF, "SPY", "SPY"),
            (AssetClass.INDEX, "SPX", "^GSPC"),
            (AssetClass.INDEX, "VIX", "^VIX"),
            (AssetClass.INDEX, "CUSTOM", "^CUSTOM"),
            (AssetClass.FX, "EURUSD", "EURUSD=X"),
            (AssetClass.COMMODITY, "GC", "GC=F"),
            (AssetClass.COMMODITY, "GOLD", "GC=F"),
            (AssetClass.COMMODITY, "ZC", "ZC=F"),
            (AssetClass.RATE, "US10Y", "^TNX"),
            (AssetClass.RATE, "US30Y", "^TYX"),
            (AssetClass.CRYPTO, "WETH", ""),
        ],
    )
    def test_mapping(self, asset_class, symbol, expected):
        assert provider_symbol_for(asset_class, symbol) == expected

    def test_default_venues(self):
        assert default_venue(AssetClass.CRYPTO, "base") == "base"
        assert default_venue(AssetClass.CRYPTO) == "ethereum"
        assert default_venue(AssetClass.EQUITY) == "us_equity"
        assert default_venue(AssetClass.FX) == "fx"

    @pytest.mark.parametrize(
        ("yahoo_type", "expected"),
        [
            ("EQUITY", AssetClass.EQUITY),
            ("ETF", AssetClass.ETF),
            ("CURRENCY", AssetClass.FX),
            ("FUTURE", AssetClass.COMMODITY),
            ("INDEX", AssetClass.INDEX),
            ("CRYPTOCURRENCY", AssetClass.CRYPTO),
            ("MUTUALFUND", None),
            (None, None),
        ],
    )
    def test_yahoo_type_mapping(self, yahoo_type, expected):
        assert asset_class_for_yahoo_type(yahoo_type) is expected


class TestInstrument:
    def test_equity_identity_and_key(self):
        instrument = Instrument.build(AssetClass.EQUITY, symbol="aapl")
        assert instrument.identity == "AAPL"
        assert instrument.symbol == "AAPL"
        assert instrument.venue == "us_equity"
        assert instrument.provider_symbol == "AAPL"
        assert instrument.key == ("equity", "us_equity", "AAPL")
        assert instrument.label == "AAPL"
        assert "equity" in instrument.market_label

    def test_provider_symbol_override_wins(self):
        instrument = Instrument.build(AssetClass.INDEX, symbol="SPX", provider_symbol="^GSPC")
        assert instrument.provider_symbol == "^GSPC"

    def test_crypto_identity_is_the_contract(self):
        instrument = Instrument.build(AssetClass.CRYPTO, symbol="WETH", address="0xabc", chain="BASE")
        assert instrument.identity == "0xabc"
        assert instrument.venue == "base"
        assert instrument.provider_symbol == ""

    def test_crypto_requires_address(self):
        with pytest.raises(InstrumentError):
            Instrument.build(AssetClass.CRYPTO, symbol="WETH")

    def test_meta_round_trip(self):
        instrument = Instrument.build(AssetClass.COMMODITY, symbol="GC")
        meta = instrument.to_meta()
        assert meta["asset_class"] == "commodity"
        assert meta["provider_symbol"] == "GC=F"
        assert instrument.to_dict()["venue"] == "futures"


class TestSnapshotProviderSymbol:
    def test_explicit_meta_wins(self):
        snapshot = TokenSnapshot("AAPL", chain="us_equity", asset_class="equity", meta={"provider_symbol": "AAPL.US"})
        assert provider_symbol_for_snapshot(snapshot) == "AAPL.US"

    def test_derived_when_missing(self):
        snapshot = TokenSnapshot("EURUSD", chain="fx", asset_class="fx", symbol="EURUSD")
        assert provider_symbol_for_snapshot(snapshot) == "EURUSD=X"

    def test_crypto_has_none(self):
        snapshot = TokenSnapshot("0xabc", asset_class="crypto")
        assert provider_symbol_for_snapshot(snapshot) == ""
