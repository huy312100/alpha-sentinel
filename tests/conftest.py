"""Hermetic test fixtures: temp dirs, scripted agents, no network, no real keys.

Every test in this suite must pass on a laptop with no API keys and no internet.
Anything that talks to a provider is either mocked or driven by ``DEMO_MODE``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # allows `pytest` without `python -m`
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import Settings  # noqa: E402
from core.models import (  # noqa: E402
    AlphaAssessment,
    FinalVerdict,
    Layer,
    LayerSignal,
    RiskAudit,
    SecurityReport,
    Severity,
    SignalStatus,
    TokenSnapshot,
    VerdictStatus,
    WashTradingReport,
    utcnow,
)
from storage.hot_store import HotStore  # noqa: E402

TOKEN = "0x" + "ab" * 20
OTHER_TOKEN = "0x" + "cd" * 20
SOL_TOKEN = "So11111111111111111111111111111111111111112"

WATCHLIST_YAML = """\
version: 1
defaults:
  chain: ethereum
  priority: 100
  cooldown_hours: 6
settings:
  max_tokens_per_cycle: 3
watchlists:
  core:
    description: Blue chips
    tokens:
      - address: "0x0000000000000000000000000000000000000001"
        symbol: WETH
        priority: 10
      - address: "0x0000000000000000000000000000000000000002"
        symbol: USDC
        priority: 20
  degen:
    tokens:
      - address: "So11111111111111111111111111111111111111112"
        symbol: WSOL
        chain: solana
        tags: [meme, solana]
"""


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Settings rooted entirely inside ``tmp_path`` - no writes to the repo."""
    settings = Settings.load(
        {
            "APP_ENV": "test",
            "DEMO_MODE": "true",
            "LOG_JSON": "false",
            "DATA_DIR": str(tmp_path / "data"),
            "HOT_DB_PATH": str(tmp_path / "data" / "hot" / "alpha_sentinel.db"),
            "PARQUET_ROOT": str(tmp_path / "data" / "cold"),
            "WATCHLIST_PATH": str(tmp_path / "watchlists.yaml"),
            "R2_ENABLED": "false",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
        }
    )
    settings.storage.ensure_dirs()
    return settings


@pytest.fixture()
def watchlist_file(settings: Settings) -> Path:
    path = settings.scanners.watchlist_path
    path.write_text(WATCHLIST_YAML, encoding="utf-8")
    return path


@pytest.fixture()
def store(settings: Settings):
    store = HotStore(settings.storage.hot_db_path)
    store.initialize()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture()
def make_verdict():
    """Factory for realistic :class:`FinalVerdict` rows."""

    def _make(**overrides):
        alpha = overrides.pop(
            "alpha",
            AlphaAssessment(
                alpha_score=82.0,
                confidence=0.8,
                thesis="Momentum building <b>fast</b> & float is thin",
                catalysts=["CEX listing", "mainnet upgrade"],
                layer_highlights={"social": "mentions up 3x"},
                red_flags=["unlocked treasury"],
                model="test-alpha",
                usage={"total_tokens": 1200},
            ),
        )
        risk = overrides.pop(
            "risk",
            RiskAudit(
                risk_score=22.0,
                veto=False,
                severity=Severity.LOW,
                reasoning_summary="Liquidity is locked; no mint authority.",
                invalidation_triggers=["LP unlock in 30 days"],
                model="test-risk",
            ),
        )
        defaults = {
            "token_address": TOKEN,
            "chain": "ethereum",
            "symbol": "TEST",
            "status": VerdictStatus.APPROVED,
            "final_score": 81.5,
            "prior_score": 70.0,
            "blended_score": 77.2,
            "alpha": alpha,
            "risk": risk,
            "position_multiplier": 0.75,
            "rationale": "Approved: both agents aligned.",
            "decided_at": utcnow(),
            "latency_ms": 1234.5,
            "security_flags": [],
        }
        defaults.update(overrides)
        return FinalVerdict(**defaults)

    return _make


@pytest.fixture()
def make_snapshot():
    """Factory for a :class:`TokenSnapshot` with deterministic layer signals."""

    def _make(**overrides):
        signals = overrides.pop(
            "signals",
            [
                LayerSignal(layer=Layer.DEX, score=78.0, confidence=0.9, weight=1.0, status=SignalStatus.OK),
                LayerSignal(layer=Layer.SOCIAL, score=64.0, confidence=0.6, weight=1.0, status=SignalStatus.DEGRADED),
                LayerSignal(
                    layer=Layer.DERIVATIVES,
                    score=10.0,
                    confidence=0.0,
                    weight=1.0,
                    status=SignalStatus.UNAVAILABLE,
                ),
            ],
        )
        defaults = {
            "token_address": TOKEN,
            "chain": "ethereum",
            "symbol": "TEST",
            "price_usd": 1.23,
            "liquidity_usd": 250_000.0,
            "security": SecurityReport(available=True),
            "wash": WashTradingReport(evaluated=True, suspicion_score=10.0, trade_count=400),
            "signals": signals,
        }
        defaults.update(overrides)
        return TokenSnapshot(**defaults)

    return _make
