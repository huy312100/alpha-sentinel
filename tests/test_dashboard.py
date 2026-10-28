"""The dashboard must render against empty stores - no server, no browser."""

from __future__ import annotations

from streamlit.testing.v1 import AppTest

from core.models import VerdictStatus
from storage.hot_store import HotStore
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
    return data


def test_dashboard_renders_with_empty_stores(tmp_path, monkeypatch):
    _point_env_at(monkeypatch, tmp_path)

    at = AppTest.from_file(str(PROJECT_ROOT / "dashboard" / "app.py"), default_timeout=120)
    at.run()

    assert [exception.value for exception in at.exception] == []
    assert at.title[0].value.endswith("AlphaSentinel")
    assert len(at.tabs) == 6
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
