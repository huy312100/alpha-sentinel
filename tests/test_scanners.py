"""Scanner edge cases where a wrong shape used to corrupt a signal."""

from __future__ import annotations

from core.models import SignalStatus, TokenSnapshot
from scanners.base import ScanContext
from scanners.crypto.providers.dex import DexMarket, DexScanner, _gecko_row_is_base, _pick_best_pair
from scanners.crypto.providers.onchain import concentration_from_holders
from scanners.crypto.providers.wash import _epoch_seconds, _parse_trades, self_trade_share
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

    monkeypatch.setattr("scanners.crypto.providers.dex.load_market", fake_load)
    token = TokenSnapshot(token_address=TOKEN, chain="ethereum", asset_class="crypto", symbol="TEST")
    signal = await DexScanner(settings).collect(token, ScanContext(None, settings))

    assert "turnover n/a" in signal.summary
    assert "24h vol n/a" in signal.summary
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
    trades = _parse_trades(payload)
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
    assert self_trade_share(_parse_trades(payload)) == 0.0
