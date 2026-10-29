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
