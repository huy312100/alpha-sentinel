"""Bot CLI: the boot gate that turns a bad deploy into a crash instead of a dud.

The daemon is what production actually runs, so these tests drive ``main()``
itself rather than ``Settings.validate()``: they pin the contract that a run mode
refuses to start on an invalid environment while ``--status`` (the container
healthcheck) keeps answering.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import bot.main as bot_main
from bot.engine import ScanReport
from config.errors import ConfigError
from config.settings import Settings
from tests.conftest import WATCHLIST_YAML, clear_inherited_settings, settings_env_keys


@pytest.fixture()
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a hermetic environment rooted in ``tmp_path``.

    ``_maybe_load_dotenv`` is stubbed out so a developer's local ``.env`` cannot
    make a test pass by accident, and every setting the loader parses is cleared
    so only this fixture decides what the CLI reads.
    """
    watchlist = tmp_path / "watchlists.yaml"
    watchlist.write_text(WATCHLIST_YAML, encoding="utf-8")
    hot_dir = tmp_path / "data" / "hot"
    hot_dir.mkdir(parents=True)
    clear_inherited_settings(monkeypatch)
    monkeypatch.setattr("config.settings._maybe_load_dotenv", lambda: None)
    monkeypatch.setenv("LOG_JSON", "false")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOT_DB_PATH", str(hot_dir / "alpha_sentinel.db"))
    monkeypatch.setenv("PARQUET_ROOT", str(tmp_path / "data" / "cold"))
    monkeypatch.setenv("WATCHLIST_PATH", str(watchlist))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")

    def _set(**env: str) -> None:
        """Apply one test's overrides on top of the clean environment."""
        for key, value in env.items():
            monkeypatch.setenv(key, value)

    return _set


@pytest.fixture()
def misconfigured_env(environment):
    """A production-shaped environment missing its API key."""
    environment(APP_ENV="production", DEMO_MODE="false")


def test_a_cycle_refuses_to_start_without_the_llm_key(misconfigured_env, capsys):
    """A run mode must die on the gate, before the watchlist or store is touched."""
    exit_code = asyncio.run(bot_main.main(["--once", "--limit", "1"]))

    assert exit_code == bot_main.EXIT_USAGE
    stderr = capsys.readouterr().err
    assert "invalid configuration" in stderr
    assert "LLM_API_KEY" in stderr


def test_the_daemon_refuses_to_start_too(misconfigured_env, capsys):
    """The daemon is the long-running production mode: same gate, same exit code."""
    assert asyncio.run(bot_main.main(["--daemon"])) == bot_main.EXIT_USAGE
    assert "LLM_API_KEY" in capsys.readouterr().err


def test_status_still_answers_for_the_container_healthcheck(misconfigured_env, capsys):
    """Validation is skipped for ``--status``: the Dockerfile HEALTHCHECK runs it."""
    assert asyncio.run(bot_main.main(["--status"])) == bot_main.EXIT_OK
    assert "hot store" in capsys.readouterr().out


def test_an_unparseable_environment_is_reported_not_raised(environment, capsys):
    """A load-time ConfigError must reach the operator as a diagnostic, not a traceback."""
    environment(LLM_PROVIDER="nope")

    exit_code = asyncio.run(bot_main.main(["--once", "--limit", "1"]))

    assert exit_code == bot_main.EXIT_USAGE
    stderr = capsys.readouterr().err
    assert "unknown LLM_PROVIDER 'nope'" in stderr
    assert "Traceback" not in stderr


def test_status_cannot_answer_when_the_environment_does_not_parse(environment, tmp_path, capsys):
    """``--status`` is exempt from *validation*, not from needing usable settings.

    Without settings there is no store path to read, and falling back to defaults
    would inspect - and create - the wrong database, so a clean EXIT_USAGE beats
    a misleading status page. A live container always has a loadable environment,
    because the daemon refuses to boot otherwise.
    """
    environment(ENDPOINTS_FILE=str(tmp_path / "absent-endpoints.yaml"))

    assert asyncio.run(bot_main.main(["--status"])) == bot_main.EXIT_USAGE
    stderr = capsys.readouterr().err
    assert "cannot read endpoint catalogue" in stderr
    assert "Traceback" not in stderr


def test_demo_mode_still_needs_no_key(environment, monkeypatch, capsys):
    """DEMO_MODE is the documented zero-key path: the gate must let a cycle run.

    ``--status`` never reaches the gate, so the cycle is driven with stubbed
    wiring: the point is the run-mode path, not a network round trip.
    """
    environment(APP_ENV="development", DEMO_MODE="true")

    class LocalAlerter:
        """Stand-in for ``TelegramAlerter``: accepts the wiring, sends nothing."""

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class LocalEngine:
        """Stand-in for ``ScanEngine``: returns one local verdict, no provider I/O."""

        cycle_kwargs: dict = {}

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

        def stop(self):
            """Signal-handler hook installed by :func:`bot.main.main`."""

        async def run_cycle(self, **kwargs):
            LocalEngine.cycle_kwargs = kwargs
            return ScanReport(run_id="demo-cycle", dry_run=True, requested=1, collected=1, approved=1)

    monkeypatch.setattr(bot_main, "TelegramAlerter", LocalAlerter)
    monkeypatch.setattr(bot_main, "ScanEngine", LocalEngine)

    exit_code = asyncio.run(bot_main.main(["--once", "--dry-run", "--limit", "1"]))

    assert exit_code == bot_main.EXIT_OK
    assert LocalEngine.cycle_kwargs["dry_run"] is True
    assert LocalEngine.cycle_kwargs["limit"] == 1
    assert "run demo-cycle (dry-run)" in capsys.readouterr().out


def test_the_cleared_set_tracks_the_loader():
    """Guard the discovery: a renamed parse helper must not empty the clear list.

    If ``Settings.load`` stopped routing keys through ``_get``, the derived set
    would collapse to the legacy aliases and the fixture would go back to leaking
    - green on a clean CI runner, red on any machine that exports a setting.
    """
    keys = settings_env_keys()

    assert {"ENDPOINTS_FILE", "LLM_PROVIDER", "R2_REGION", "DEEPSEEK_INPUT_USD_PER_MTOK"} <= keys
    # A collapse to just the seven aliases is the failure this guards against.
    assert len(keys) >= 100


def test_a_setting_inherited_from_the_host_is_cleared(monkeypatch):
    """Regression: a legacy alias exported by the host used to fail the demo run.

    ``DEEPSEEK_INPUT_USD_PER_MTOK=invalid`` is mapped onto ``LLM_INPUT_USD_PER_MTOK``
    by ``Settings.load()``, which then cannot parse it: the CLI would exit 2 before
    demo mode was ever reached.
    """
    monkeypatch.setenv("DEEPSEEK_INPUT_USD_PER_MTOK", "not-a-number")
    monkeypatch.setenv("ENDPOINTS_FILE", "/nonexistent/endpoints.yaml")
    with pytest.raises(ConfigError):
        Settings.load(load_dotenv_file=False)

    clear_inherited_settings(monkeypatch)

    assert "DEEPSEEK_INPUT_USD_PER_MTOK" not in os.environ
    assert "ENDPOINTS_FILE" not in os.environ
    assert Settings.load(load_dotenv_file=False).llm.input_usd_per_mtok > 0
