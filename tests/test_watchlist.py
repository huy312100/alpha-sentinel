"""Watchlist parsing is the front door: it must fail loudly, never guess."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import yaml

from bot.watchlist import (
    DEFAULT_COOLDOWN_HOURS,
    DEFAULT_PRIORITY,
    WatchlistError,
    WatchlistLoader,
    is_evm_address,
    is_solana_address,
    load_watchlists,
    normalise_chain,
    parse_watchlists,
    validate_address,
)
from core.instruments import AssetClass
from tests.conftest import SOL_TOKEN, TOKEN, WATCHLIST_YAML

MULTI_MARKET_YAML = f"""\
version: 1
defaults:
  asset_class: crypto
  chain: ethereum
settings:
  max_tokens_per_cycle: 10
watchlists:
  crypto:
    tokens:
      - address: "{TOKEN}"
        symbol: WETH
  equities:
    asset_class: equity
    venue: nyse
    instruments:
      - AAPL
      - symbol: BRK.B
        provider_symbol: BRK-B
        priority: 20
  fx:
    asset_class: fx
    instruments:
      - symbol: eur/usd
        priority: 30
"""


class TestAddressValidation:
    def test_evm_address_is_lowercased(self):
        assert validate_address("0x" + "AB" * 20, "ethereum") == "0x" + "ab" * 20

    def test_solana_address_keeps_case(self):
        assert validate_address(SOL_TOKEN, "solana") == SOL_TOKEN

    @pytest.mark.parametrize("address", ["", "   ", "0x123", "not-an-address"])
    def test_evm_rejects_garbage(self, address):
        with pytest.raises(WatchlistError):
            validate_address(address, "ethereum")

    def test_solana_rejects_evm_address(self):
        with pytest.raises(WatchlistError):
            validate_address(TOKEN, "solana")

    def test_predicates(self):
        assert is_evm_address(TOKEN)
        assert not is_evm_address(SOL_TOKEN)
        assert is_solana_address(SOL_TOKEN)
        assert not is_solana_address(TOKEN)


class TestChainNormalisation:
    def test_case_and_whitespace_are_folded(self):
        assert normalise_chain("  SOLANA ") == "solana"

    def test_unknown_chain_is_rejected(self):
        with pytest.raises(WatchlistError):
            normalise_chain("dogechain")


class TestParsing:
    def test_defaults_and_ordering(self):
        bundle = parse_watchlists(yaml.safe_load(WATCHLIST_YAML))
        assert bundle.names() == ["core", "degen"]
        assert len(bundle) == 3
        tokens = bundle.tokens()
        assert tokens[0].symbol == "WETH"
        assert tokens[0].priority == 10
        assert tokens[0].chain == "ethereum"
        assert tokens[0].cooldown_hours == DEFAULT_COOLDOWN_HOURS
        solana = bundle.get("degen").tokens[0]
        assert solana.chain == "solana"
        assert solana.tags == ("meme", "solana")

    def test_cap_is_enforced(self):
        bundle = parse_watchlists(yaml.safe_load(WATCHLIST_YAML))
        assert bundle.max_tokens_per_cycle == 3
        assert len(bundle.capped()) == 3
        assert len(bundle.capped(limit=2)) == 2
        assert bundle.capped(limit=0) == []

    def test_duplicate_tokens_across_lists_are_collapsed_case_insensitively(self):
        payload = {
            "watchlists": {
                "a": {"tokens": [{"address": "0x" + "AB" * 20}]},
                "b": {"tokens": [{"address": "0x" + "ab" * 20}]},
            }
        }
        bundle = parse_watchlists(payload)
        assert len(bundle.tokens()) == 1
        assert len(bundle) == 1

    def test_defaults_supply_priority(self):
        payload = {"defaults": {"priority": 7}, "watchlists": {"a": {"tokens": [TOKEN]}}}
        bundle = parse_watchlists(payload)
        assert bundle.tokens()[0].priority == 7
        assert bundle.tokens()[0].cooldown_hours == DEFAULT_COOLDOWN_HOURS

    def test_bare_string_tokens_use_default_chain(self):
        payload = {"defaults": {"chain": "base"}, "watchlists": {"a": {"tokens": [TOKEN]}}}
        bundle = parse_watchlists(payload)
        assert bundle.tokens()[0].chain == "base"
        assert DEFAULT_PRIORITY == 100

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            [],
            {},
            {"watchlists": {}},
            {"watchlists": {"a": {"tokens": "not-a-list"}}},
            {"watchlists": {"a": [{"address": "0xnope"}]}},
            {"watchlists": {"a": [{"address": TOKEN, "cooldown_hours": -1}]}},
            {"watchlists": {"a": [{"address": TOKEN, "priority": "high"}]}},
            {"watchlists": {"a": {}, "A": {}}},
            {"watchlists": {"a": {"tokens": [TOKEN]}}, "settings": {"max_tokens_per_cycle": 0}},
        ],
    )
    def test_invalid_payloads_raise(self, payload):
        with pytest.raises(WatchlistError):
            parse_watchlists(payload)

    def test_tokens_are_snapshots(self):
        bundle = parse_watchlists(yaml.safe_load(WATCHLIST_YAML))
        snapshot = bundle.tokens()[0].to_snapshot()
        assert snapshot.token_address == "0x0000000000000000000000000000000000000001"
        assert snapshot.meta["watchlist_priority"] == 10


class TestLoading:
    def test_missing_file_is_an_error(self, tmp_path: Path):
        with pytest.raises(WatchlistError):
            load_watchlists(tmp_path / "nope.yaml")

    def test_invalid_yaml_is_wrapped(self, tmp_path: Path):
        path = tmp_path / "bad.yaml"
        path.write_text("watchlists: [unclosed\n", encoding="utf-8")
        with pytest.raises(WatchlistError):
            load_watchlists(path)

    def test_file_loads_with_default_chain(self, watchlist_file: Path):
        bundle = load_watchlists(watchlist_file)
        assert bundle.source == str(watchlist_file)
        assert len(bundle) == 3


class TestHotReload:
    def test_loader_caches_until_mtime_changes(self, watchlist_file: Path):
        loader = WatchlistLoader(watchlist_file)
        first = loader.load()
        assert loader.load() is first  # cached, no re-parse

        watchlist_file.write_text(
            WATCHLIST_YAML.replace("max_tokens_per_cycle: 3", "max_tokens_per_cycle: 9"), encoding="utf-8"
        )
        bumped = time.time() + 5
        os.utime(watchlist_file, (bumped, bumped))
        second = loader.load()
        assert second is not first
        assert second.max_tokens_per_cycle == 9

    def test_force_reload_ignores_cache(self, watchlist_file: Path):
        loader = WatchlistLoader(watchlist_file)
        first = loader.load()
        assert loader.load(force=True) is not first


class TestMultiMarketParsing:
    def bundle(self):
        return parse_watchlists(yaml.safe_load(MULTI_MARKET_YAML))

    def test_crypto_entries_are_unchanged(self):
        token = self.bundle().get("crypto").tokens[0]
        assert token.asset_class is AssetClass.CRYPTO
        assert token.token_address == TOKEN.lower()
        assert token.chain == "ethereum"
        assert token.provider_symbol == ""

    def test_section_market_key_selects_the_asset_class(self):
        payload = yaml.safe_load(
            """
watchlists:
  stocks:
    market: equity
    venue: nasdaq
    instruments: [AAPL]
"""
        )
        token = parse_watchlists(payload).get("stocks").tokens[0]
        assert token.asset_class is AssetClass.EQUITY
        assert token.chain == "nasdaq"

    def test_section_level_asset_class_and_venue(self):
        aapl, brk = self.bundle().get("equities").tokens
        assert aapl.asset_class is AssetClass.EQUITY
        assert aapl.token_address == "AAPL"
        assert aapl.chain == "nyse"
        assert aapl.provider_symbol == "AAPL"
        assert brk.token_address == "BRK.B"
        assert brk.provider_symbol == "BRK-B"  # explicit override wins
        assert brk.priority == 20

    def test_bare_string_is_a_ticker_when_not_an_address(self):
        aapl = self.bundle().get("equities").tokens[0]
        assert aapl.symbol == "AAPL"

    def test_fx_pair_is_normalised_and_mapped(self):
        pair = self.bundle().get("fx").tokens[0]
        assert pair.token_address == "EURUSD"
        assert pair.provider_symbol == "EURUSD=X"
        assert pair.chain == "fx"  # default venue for the class

    def test_bundle_flattens_every_market(self):
        bundle = self.bundle()
        assert bundle.names() == ["crypto", "equities", "fx"]
        assert len(bundle) == 4
        classes = [token.asset_class.value for token in bundle.tokens()]
        assert classes.count("equity") == 2
        assert "crypto" in classes
        assert "fx" in classes

    def test_snapshot_carries_asset_class_and_provider_symbol(self):
        aapl = self.bundle().get("equities").tokens[0]
        snapshot = aapl.to_snapshot()
        assert snapshot.asset_class == "equity"
        assert snapshot.chain == "nyse"
        assert snapshot.meta["provider_symbol"] == "AAPL"
        assert snapshot.meta["asset_class"] == "equity"

    def test_duplicate_instruments_across_sections_collapse(self):
        payload = {
            "watchlists": {
                "a": {"asset_class": "equity", "instruments": ["AAPL"]},
                "b": {"asset_class": "equity", "instruments": [{"symbol": "aapl"}]},
            }
        }
        bundle = parse_watchlists(payload)
        assert len(bundle.tokens()) == 1

    def test_same_ticker_in_two_markets_is_not_a_duplicate(self):
        payload = {
            "watchlists": {
                "a": {"asset_class": "equity", "instruments": ["GC"]},
                "b": {"asset_class": "commodity", "instruments": ["GC"]},
            }
        }
        bundle = parse_watchlists(payload)
        # Equity GC lives on us_equity, the future on futures: distinct venues.
        assert len(bundle.tokens()) == 2

    @pytest.mark.parametrize(
        "payload",
        [
            {"watchlists": {"a": {"asset_class": "equity", "instruments": [{"priority": 5}]}}},
            {"watchlists": {"a": {"asset_class": "martian", "instruments": ["AAPL"]}}},
            {"watchlists": {"a": {"asset_class": "fx", "instruments": ["EUR"]}}},
            {"watchlists": {"a": {"asset_class": "equity", "instruments": "AAPL"}}},
        ],
    )
    def test_invalid_multi_market_payloads_raise(self, payload):
        with pytest.raises(WatchlistError):
            parse_watchlists(payload)

    def test_crypto_entry_still_requires_an_address(self):
        payload = {"watchlists": {"a": {"tokens": [{"symbol": "WETH"}]}}}
        with pytest.raises(WatchlistError):
            parse_watchlists(payload)
