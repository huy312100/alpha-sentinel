"""Scanner edge cases where a wrong shape used to corrupt a signal."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from core.models import SignalStatus, TokenSnapshot
from scanners.base import ScanContext
from scanners.crypto.layer1_onchain import concentration_from_holders
from scanners.crypto.layer2_derivatives import CryptoDerivativesScanner, parse_hyperliquid_context
from scanners.crypto.layer3_social import CryptoSocialScanner
from scanners.crypto.layer4_dex import (
    CryptoDexScanner,
    DexMarket,
    _epoch_seconds,
    _gecko_row_is_base,
    _pair_created_at,
    _pick_best_pair,
    parse_trades,
    self_trade_share,
)
from scanners.crypto.layer5_ecosystem import match_protocol
from scanners.crypto.layer6_tokenomics import parse_event_calendar
from scanners.shared.http_client import fetch_provider
from tests.conftest import TOKEN


# --------------------------------------------------------------------------- #
# DEX: only base-token pairs describe the instrument being scanned
# --------------------------------------------------------------------------- #
def test_pick_best_pair_ignores_pools_where_the_token_is_the_quote():
    pairs = [
        {
            "chainId": "ethereum",
            "baseToken": {"address": "0x" + "11" * 20},
            "quoteToken": {"address": TOKEN},
            "liquidity": {"usd": "9000000"},
            "priceUsd": "1.0",
        },
        {
            "chainId": "ethereum",
            "baseToken": {"address": TOKEN},
            "quoteToken": {"address": "0x" + "22" * 20},
            "liquidity": {"usd": "50000"},
            "priceUsd": "0.42",
        },
    ]
    best = _pick_best_pair(pairs, "ethereum", TOKEN)
    assert best is not None
    assert best["baseToken"]["address"] == TOKEN


def test_pick_best_pair_returns_none_when_the_token_is_only_a_quote():
    pairs = [
        {
            "chainId": "ethereum",
            "baseToken": {"address": "0x" + "11" * 20},
            "quoteToken": {"address": TOKEN},
            "liquidity": {"usd": "9000000"},
        }
    ]
    assert _pick_best_pair(pairs, "ethereum", TOKEN) is None


def test_gecko_rows_are_filtered_by_base_token_relationship():
    mine = {"relationships": {"base_token": {"data": {"id": f"eth_{TOKEN}"}}}}
    theirs = {"relationships": {"base_token": {"data": {"id": "eth_0x" + "33" * 20}}}}
    unknown = {"attributes": {"reserve_in_usd": "1"}}
    assert _gecko_row_is_base(mine, TOKEN) is True
    assert _gecko_row_is_base(theirs, TOKEN) is False
    assert _gecko_row_is_base(unknown, TOKEN) is True  # cannot tell -> keep


async def test_dex_summary_survives_a_missing_turnover(settings, monkeypatch):
    market = DexMarket(
        pair_address="0x" + "44" * 20,
        dex_id="uniswap",
        base_symbol="TEST",
        price_usd=1.0,
        liquidity_usd=0.0,
        volume_24h_usd=None,
    )
    assert market.turnover is None

    async def fake_load(token, ctx):
        return market

    monkeypatch.setattr("scanners.crypto.layer4_dex.load_market", fake_load)
    token = TokenSnapshot(token_address=TOKEN, chain="ethereum", asset_class="crypto", symbol="TEST")
    signal = await CryptoDexScanner(settings).collect(token, ScanContext(None, settings))

    assert "24h vol n/a" in signal.summary
    assert signal.metrics["turnover"] is None
    assert signal.status is SignalStatus.DEGRADED


# --------------------------------------------------------------------------- #
# On-chain: concentration is relative to total supply, not to page one
# --------------------------------------------------------------------------- #
def test_concentration_uses_total_supply_as_the_denominator():
    rows = [
        {"address": "0x" + "aa" * 20, "value": "1000"},
        {"address": "0x" + "bb" * 20, "value": "3000"},
        {"address": "0x000000000000000000000000000000000000dEaD", "value": "50000"},
    ]
    pct, inspected = concentration_from_holders(rows, total_supply=1_000_000)

    assert inspected == 3
    assert pct == 0.4  # (1000 + 3000) / 1_000_000, burn address excluded from top


def test_concentration_falls_back_to_the_page_and_excludes_sinks():
    rows = [
        {"address": "0x" + "aa" * 20, "value": "1000"},
        {"address": "0x0000000000000000000000000000000000000000", "value": "9000"},
    ]
    pct, _ = concentration_from_holders(rows)
    assert pct == 10.0


# --------------------------------------------------------------------------- #
# Wash filter: ISO-8601 timestamps are real time, not zero
# --------------------------------------------------------------------------- #
def test_iso_block_timestamps_are_parsed():
    assert _epoch_seconds("2026-10-25T12:00:00Z") == _epoch_seconds("2026-10-25T12:00:00+00:00")
    assert _epoch_seconds("2026-10-25T12:00:00Z") > 1_700_000_000
    assert _epoch_seconds("2026-10-25T12:00:00") is not None  # naive -> UTC
    assert _epoch_seconds("not-a-time") is None
    assert _epoch_seconds(None) is None


def test_trades_without_a_timestamp_are_not_assumed_simultaneous():
    payload = {
        "data": [
            {"attributes": {"volume_in_usd": "10", "tx_from_address": "0xA", "kind": "buy"}},
            {"attributes": {"volume_in_usd": "10", "tx_from_address": "0xA", "kind": "sell"}},
        ]
    }
    trades = parse_trades(payload)
    assert [trade["timestamp"] for trade in trades] == [None, None]
    assert self_trade_share(trades) == 0.0


def test_iso_trades_far_apart_are_not_self_trading():
    payload = {
        "data": [
            {
                "attributes": {
                    "volume_in_usd": "10",
                    "tx_from_address": "0xA",
                    "kind": "buy",
                    "block_timestamp": "2026-10-25T12:00:00Z",
                }
            },
            {
                "attributes": {
                    "volume_in_usd": "10",
                    "tx_from_address": "0xA",
                    "kind": "sell",
                    "block_timestamp": "2026-10-25T13:00:00Z",
                }
            },
        ]
    }
    assert self_trade_share(parse_trades(payload)) == 0.0


# --------------------------------------------------------------------------- #
# On-chain: Blockscout returns a holder's address as a string or an object
# --------------------------------------------------------------------------- #
def test_object_shaped_holder_addresses_still_match_the_sinks():
    rows = [
        {"address": {"hash": "0x000000000000000000000000000000000000dEaD"}, "value": "90000"},
        {"address": {"hash": "0x" + "aa" * 20}, "value": "1000"},
    ]
    pct, inspected = concentration_from_holders(rows, total_supply=1_000_000)

    assert inspected == 2
    assert pct == 0.1  # burn address excluded: 1000 / 1_000_000


# --------------------------------------------------------------------------- #
# DEX: a pair on another chain is a different market, not a fallback
# --------------------------------------------------------------------------- #
def test_pick_best_pair_never_crosses_chains():
    pairs = [{"chainId": "bsc", "baseToken": {"address": TOKEN}, "liquidity": {"usd": "9000000"}}]

    assert _pick_best_pair(pairs, "ethereum", TOKEN) is None
    # No chain on the instrument -> nothing to filter by, keep the deepest pair.
    assert _pick_best_pair(pairs, "", TOKEN) is not None


def test_geckoterminal_pool_age_is_parsed_from_iso():
    created = _pair_created_at("2024-03-08T10:41:47Z")

    assert created == datetime(2024, 3, 8, 10, 41, 47, tzinfo=timezone.utc)
    assert _pair_created_at(1_709_894_507_000) == created  # DexScreener's milliseconds
    assert _pair_created_at("not-a-date") is None


# --------------------------------------------------------------------------- #
# Derivatives: funding units and empty provider reads
# --------------------------------------------------------------------------- #
def test_hyperliquid_hourly_funding_is_reported_per_8h():
    payload = [
        {"universe": [{"name": "PEPE"}]},
        [{"funding": "0.0001", "markPx": "0.00001", "openInterest": "1000", "dayNtlVlm": "5000"}],
    ]
    metrics = parse_hyperliquid_context(payload, ["PEPE"])

    assert metrics["funding_rate"] == pytest.approx(0.0008)
    assert metrics["funding_interval_hours"] == 8


async def test_an_empty_coinglass_read_is_not_a_success(settings, monkeypatch):
    """A dict of ``None`` placeholders would count as ok and overwrite Hyperliquid."""

    async def empty(*_, **__):
        return {}

    monkeypatch.setattr("scanners.crypto.layer2_derivatives.request_json", empty)
    scanner = CryptoDerivativesScanner(settings)
    calls = scanner._coinglass("PEPE", ScanContext(None, settings))

    results = await asyncio.gather(*(fetch_provider(name, coro) for name, coro in calls.items()))

    assert all(not result.ok for result in results)
    assert all(result.metrics == {} for result in results)


# --------------------------------------------------------------------------- #
# Social: a provider that never ran is not "nobody is talking"
# --------------------------------------------------------------------------- #
async def test_a_missing_x_read_does_not_penalise_discussion(settings, monkeypatch):
    async def cryptopanic_only():
        return {
            "news_posts": 40,
            "news_bullish_votes": 12,
            "news_bearish_votes": 4,
            "news_sentiment_ratio": 0.75,
        }

    monkeypatch.setattr(
        CryptoSocialScanner,
        "_provider_calls",
        lambda self, symbol, ctx: {"cryptopanic": cryptopanic_only()},
    )
    token = TokenSnapshot(token_address=TOKEN, chain="ethereum", asset_class="crypto", symbol="TEST")

    signal = await CryptoSocialScanner(settings).collect(token, ScanContext(None, settings))

    assert "thin_discussion" not in signal.flags
    assert "few_authors_dominate" not in signal.flags


async def test_a_measured_but_thin_discussion_is_still_flagged(settings, monkeypatch):
    async def thin():
        return {"mentions_total": 3, "mentions_velocity": 1.0}

    monkeypatch.setattr(CryptoSocialScanner, "_provider_calls", lambda self, symbol, ctx: {"x": thin()})
    token = TokenSnapshot(token_address=TOKEN, chain="ethereum", asset_class="crypto", symbol="TEST")

    signal = await CryptoSocialScanner(settings).collect(token, ScanContext(None, settings))

    assert "thin_discussion" in signal.flags


# --------------------------------------------------------------------------- #
# Ecosystem: DeFiLlama needs the chain, and the address when it has one
# --------------------------------------------------------------------------- #
def test_match_protocol_requires_the_requested_chain():
    rows = [
        {"symbol": "USDC", "name": "Usdc Bsc", "chains": ["Binance"], "tvl": 9e9, "address": "0xb"},
        {"symbol": "USDC", "name": "Usdc Eth", "chains": ["Ethereum"], "tvl": 1e9, "address": "0xe"},
    ]

    assert match_protocol(rows, "USDC", "ethereum")["name"] == "Usdc Eth"
    assert match_protocol(rows, "USDC", "bsc")["name"] == "Usdc Bsc"  # "Binance" alias
    assert match_protocol(rows, "USDC", "solana") is None  # no cross-chain fallback


def test_match_protocol_refuses_another_tokens_tvl():
    rows = [{"symbol": "USDC", "chains": ["Ethereum"], "tvl": 1e9, "address": "0xother"}]

    assert match_protocol(rows, "USDC", "ethereum", address="0xmine") is None
    assert match_protocol(rows, "USDC", "ethereum", address="0xother") is not None
    # A catalogue row without an address cannot contradict the ticker match.
    assert match_protocol(
        [{"symbol": "USDC", "chains": ["Ethereum"], "tvl": 1}], "USDC", "ethereum", address="0xmine"
    )


# --------------------------------------------------------------------------- #
# Tokenomics: one malformed calendar row must not discard the whole calendar
# --------------------------------------------------------------------------- #
def test_a_malformed_calendar_row_is_skipped_not_fatal():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    payload = {
        "events": [
            {"symbol": "TEST", "type": "unlock", "date": float("nan")},
            {"symbol": "TEST", "type": "unlock", "date": 1e300},
            {"symbol": "TEST", "type": "unlock", "date": "2026-01-10T00:00:00Z", "pct_of_supply": 5.0},
        ]
    }

    metrics = parse_event_calendar(payload, "TEST", now=now)

    assert metrics["events_considered"] == 1
    assert metrics["next_unlock_days"] == pytest.approx(9.0)


def test_calendar_rows_without_a_symbol_belong_to_nobody():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    payload = {
        "events": [
            {"type": "unlock", "date": "2026-01-05T00:00:00Z", "pct_of_supply": 10.0},
            {"symbol": "OTHER", "type": "unlock", "date": "2026-01-06T00:00:00Z", "pct_of_supply": 10.0},
            {"symbol": "TEST", "type": "upgrade", "date": "2026-01-07T00:00:00Z"},
        ]
    }

    metrics = parse_event_calendar(payload, "TEST", now=now)

    assert "next_unlock_days" not in metrics
    assert metrics["next_upgrade_days"] == pytest.approx(6.0)
