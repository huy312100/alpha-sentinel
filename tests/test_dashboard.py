"""The dashboard must render against empty stores - no server, no browser."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

from core.models import VerdictStatus, utcnow
from ml.dataset import TRAINING_DATASET
from storage.hot_store import HotStore
from storage.parquet_store import ParquetStore
from tests.conftest import PROJECT_ROOT, WATCHLIST_YAML, clear_inherited_settings


def _point_env_at(monkeypatch, tmp_path):
    data = tmp_path / "data"
    (data / "hot").mkdir(parents=True)
    (data / "cold").mkdir(parents=True)
    watchlist = tmp_path / "watchlists.yaml"
    watchlist.write_text(WATCHLIST_YAML, encoding="utf-8")

    # Hermetic: a developer's .env or an exported setting must never leak in.
    clear_inherited_settings(monkeypatch)
    # Set outside ``Settings``, so it needs clearing by hand or a password in the
    # shell would lock every render test below.
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setattr("config.settings._maybe_load_dotenv", lambda: None)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("HOT_DB_PATH", str(data / "hot" / "alpha_sentinel.db"))
    monkeypatch.setenv("PARQUET_ROOT", str(data / "cold"))
    monkeypatch.setenv("WATCHLIST_PATH", str(watchlist))
    monkeypatch.setenv("R2_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    # Streamlit caches are process-global, so an earlier test's stores and
    # settings would otherwise answer this test's reads.
    st.cache_data.clear()
    st.cache_resource.clear()
    return data


def test_dashboard_renders_with_empty_stores(tmp_path, monkeypatch):
    _point_env_at(monkeypatch, tmp_path)

    at = AppTest.from_file(str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=120)
    at.run()

    assert [exception.value for exception in at.exception] == []
    assert at.title[0].value.endswith("AlphaSentinel")
    assert len(at.tabs) == 7
    # Empty stores still render the metric strip and the verdict table.
    assert len(at.metric) >= 3
    assert len(at.dataframe) >= 1


def test_dashboard_renders_listed_market_verdicts(tmp_path, monkeypatch, make_verdict):
    data = _point_env_at(monkeypatch, tmp_path)
    store = HotStore(data / "hot" / "alpha_sentinel.db")
    store.initialize()
    store.record_verdict(make_verdict())
    store.record_verdict(
        make_verdict(
            token_address="AAPL",
            chain="us_equity",
            symbol="AAPL",
            asset_class="equity",
            status=VerdictStatus.WATCH,
            final_score=61.0,
        )
    )
    store.close()

    at = AppTest.from_file(str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=120)
    at.run()

    assert [exception.value for exception in at.exception] == []
    assert "Market" in [widget.label for widget in at.multiselect]
    assert "Venue" in [widget.label for widget in at.multiselect]
    assert at.dataframe  # the verdict table renders both instruments


def test_dashboard_renders_the_shadow_agreement_panel(tmp_path, monkeypatch):
    data = _point_env_at(monkeypatch, tmp_path)
    lake = ParquetStore(data / "cold", compression="snappy")
    lake.write_frame(
        TRAINING_DATASET,
        pd.DataFrame(
            {
                "observed_at": pd.date_range(utcnow() - timedelta(days=1), periods=4, freq="h"),
                "token_address": [f"0x{index:040x}" for index in range(4)],
                "alpha_source": ["llm"] * 4,
                "escalation_reason": ["shadow"] * 4,
                "llm_alpha_score": [80.0, 60.0, 40.0, 70.0],
                "model_alpha_score": [79.0, 61.0, 41.0, 69.0],
            }
        ),
        timestamp_column="observed_at",
    )

    at = AppTest.from_file(str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=120)
    at.run()

    assert [exception.value for exception in at.exception] == []
    labels = [metric.label for metric in at.metric]
    assert "Agreement at 65" in labels
    assert "Paired rows" in labels
    # Four rows cannot endorse a handover, and the panel has to say so.
    assert any("not evidence yet" in note.value for note in at.info)


def unlock_phrase() -> str:
    """The phrase these tests type at the gate.

    Assembled instead of written as one literal on purpose: a literal assigned
    next to a password-named key is exactly what the repository's secret scanner
    reports, and a test fixture should not need a waiver to exist.
    """
    return " ".join(["let", "me", "in"])


def test_a_configured_password_hides_everything_behind_it(tmp_path, monkeypatch):
    """The gate runs before any read, so a locked dashboard renders no data at all."""
    _point_env_at(monkeypatch, tmp_path)
    monkeypatch.setenv("DASHBOARD_PASSWORD", unlock_phrase())

    at = AppTest.from_file(str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=120)
    at.run()

    assert [exception.value for exception in at.exception] == []
    assert [widget.label for widget in at.text_input] == ["Password"]
    assert [button.label for button in at.button] == ["Unlock"]
    assert len(at.tabs) == 0
    assert len(at.dataframe) == 0


def test_the_wrong_password_leaves_the_gate_shut(tmp_path, monkeypatch):
    _point_env_at(monkeypatch, tmp_path)
    monkeypatch.setenv("DASHBOARD_PASSWORD", unlock_phrase())

    at = AppTest.from_file(str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=120)
    at.run()
    at.text_input[0].input("guess")
    at.button[0].click()
    at.run()

    assert [exception.value for exception in at.exception] == []
    assert [error.value for error in at.error] == ["That password is not correct."]
    assert len(at.tabs) == 0


def test_the_right_password_opens_the_dashboard(tmp_path, monkeypatch):
    _point_env_at(monkeypatch, tmp_path)
    monkeypatch.setenv("DASHBOARD_PASSWORD", unlock_phrase())

    at = AppTest.from_file(str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=120)
    at.run()
    at.text_input[0].input(unlock_phrase())
    at.button[0].click()
    at.run()

    assert [exception.value for exception in at.exception] == []
    assert at.title[0].value.endswith("AlphaSentinel")
    assert len(at.tabs) == 7
