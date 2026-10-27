"""Bot CLI: the boot gate that turns a bad deploy into a crash instead of a dud.

The daemon is what production actually runs, so these tests drive ``main()``
itself rather than ``Settings.validate()``: they pin the contract that a run mode
refuses to start on an invalid environment while ``--status`` (the container
healthcheck) keeps answering.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import bot.main as bot_main
from tests.conftest import WATCHLIST_YAML


@pytest.fixture()
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a hermetic environment rooted in ``tmp_path``.

    ``_maybe_load_dotenv`` is stubbed out so a developer's local ``.env`` cannot
    make a test pass by accident.
    """
    watchlist = tmp_path / "watchlists.yaml"
    watchlist.write_text(WATCHLIST_YAML, encoding="utf-8")
    hot_dir = tmp_path / "data" / "hot"
    hot_dir.mkdir(parents=True)
    for key in ("LLM_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("config.settings._maybe_load_dotenv", lambda: None)
    monkeypatch.setenv("LOG_JSON", "false")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOT_DB_PATH", str(hot_dir / "alpha_sentinel.db"))
    monkeypatch.setenv("PARQUET_ROOT", str(tmp_path / "data" / "cold"))
    monkeypatch.setenv("WATCHLIST_PATH", str(watchlist))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")

    def _set(**env: str) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)

    return _set


@pytest.fixture()
def misconfigured_env(environment):
    """A production-shaped environment missing its API key."""
    environment(APP_ENV="production", DEMO_MODE="false")


def test_a_cycle_refuses_to_start_without_the_llm_key(misconfigured_env, capsys):
    exit_code = asyncio.run(bot_main.main(["--once", "--limit", "1"]))

    assert exit_code == bot_main.EXIT_USAGE
    stderr = capsys.readouterr().err
    assert "invalid configuration" in stderr
    assert "LLM_API_KEY" in stderr


def test_the_daemon_refuses_to_start_too(misconfigured_env, capsys):
    assert asyncio.run(bot_main.main(["--daemon"])) == bot_main.EXIT_USAGE
    assert "LLM_API_KEY" in capsys.readouterr().err


def test_status_still_answers_for_the_container_healthcheck(misconfigured_env, capsys):
    # The Dockerfile HEALTHCHECK runs this; it must not fail on a config error.
    assert asyncio.run(bot_main.main(["--status"])) == bot_main.EXIT_OK
    assert "hot store" in capsys.readouterr().out


def test_demo_mode_still_needs_no_key(environment, capsys):
    """DEMO_MODE is the documented zero-key path: the gate must not block it."""
    environment(APP_ENV="development", DEMO_MODE="true")

    assert asyncio.run(bot_main.main(["--status"])) == bot_main.EXIT_OK
    assert "hot store" in capsys.readouterr().out
