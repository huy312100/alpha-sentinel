"""The macOS LaunchAgents, checked as the deployment contract they are.

Two units that reference each other by string are easy to break quietly. Rename
the bot's label and the supervisor keeps "running" while kickstarting a job that
does not exist; change a log path and the size cap silently stops applying to the
file that is actually growing. Neither fails a build, a load, or a cycle, which is
why nothing else in CI would notice.
"""

from __future__ import annotations

import plistlib
import xml.etree.ElementTree as ET
from pathlib import Path

OPS = Path(__file__).resolve().parents[1] / "ops"
BOT_LABEL = "com.alphasentinel.bot"
WATCHDOG_LABEL = "com.alphasentinel.watchdog"


def unit(name: str) -> dict:
    path = OPS / f"{name}.plist"
    # XML first, then plistlib: a comment containing a double hyphen is illegal XML
    # and the honest mistake to make when documenting command-line flags here.
    ET.parse(path)
    return plistlib.loads(path.read_bytes())


def arguments(name: str) -> list[str]:
    return list(unit(name)["ProgramArguments"])


def flagged(name: str, flag: str) -> list[str]:
    """Every value that follows ``flag`` in the unit's argument list."""
    argv = arguments(name)
    return [argv[index + 1] for index, item in enumerate(argv) if item == flag and index + 1 < len(argv)]


def test_each_unit_declares_the_label_its_filename_implies():
    """launchd identifies jobs by label; a mismatch means the file loads as
    something else and the old copy is never replaced."""
    assert unit(BOT_LABEL)["Label"] == BOT_LABEL
    assert unit(WATCHDOG_LABEL)["Label"] == WATCHDOG_LABEL


def test_the_supervisor_kickstarts_the_label_the_bot_actually_uses():
    """The one string that crosses between the two files."""
    assert flagged(WATCHDOG_LABEL, "--target") == [BOT_LABEL]


def test_the_supervisor_bounds_the_logs_the_bot_actually_writes():
    """An unlisted log file grows forever, and the cap looks like it works."""
    bot = unit(BOT_LABEL)
    bounded = set(flagged(WATCHDOG_LABEL, "--log-file"))

    assert {bot["StandardOutPath"], bot["StandardErrorPath"]} <= bounded


def test_both_units_start_with_the_session_and_throttle_a_failing_job():
    """RunAtLoad means *at login*, which is a real limit worth keeping visible.

    A LaunchAgent lives in a user's GUI session: it is not running before someone
    logs in, and logout stops it. Asserting these keys says the units come up with
    that session and relaunch a dying process without hammering - not that the box
    survives a reboot unattended, which needs automatic login or a root-level
    LaunchDaemon, and the second is the wrong privilege for a process holding keys.
    """
    for name in (BOT_LABEL, WATCHDOG_LABEL):
        document = unit(name)
        assert document["RunAtLoad"] is True, name
        assert document["KeepAlive"] is True, name
        # Without a throttle, a job that refuses to boot is restarted as fast as
        # launchd can spawn it, which buries the reason in an endless log.
        assert int(document["ThrottleInterval"]) >= 10, name


def test_the_bot_is_started_as_a_daemon_by_the_project_interpreter():
    argv = arguments(BOT_LABEL)

    assert argv[0] == "__REPO__/.venv/bin/python"
    assert argv[1:] == ["-m", "bot.main", "--daemon"]


def test_the_supervisor_runs_the_watchdog_from_the_repository_root():
    argv = arguments(WATCHDOG_LABEL)

    assert argv[1] == "ops/watchdog.py"
    assert unit(WATCHDOG_LABEL)["WorkingDirectory"] == "__REPO__"
    assert "--backend" in argv and "launchd" in argv


def test_the_log_cap_matches_the_compose_file_ceiling():
    """Two deployment paths, one number, or a soak fills the disk on whichever
    path the operator did not think about."""
    assert float(flagged(WATCHDOG_LABEL, "--max-log-mb")[0]) == 20.0


def test_installing_replaces_every_placeholder():
    """A leftover ``__REPO__`` is a path that cannot exist, discovered at load."""
    for name in (BOT_LABEL, WATCHDOG_LABEL):
        text = (OPS / f"{name}.plist").read_text(encoding="utf-8")
        assert "__HOME__/" in text or "__REPO__/" in text, name

        installed = text.replace("__REPO__", "/Users/dev/alpha-sentinel").replace("__HOME__", "/Users/dev")
        assert "__REPO__" not in installed and "__HOME__" not in installed
        ET.fromstring(installed)
