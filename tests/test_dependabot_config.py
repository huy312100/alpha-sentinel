"""The dependabot schedule, asserted because a broken one fails silently.

An unknown key or a mistyped interval does not fail a build: dependabot just
stops opening pull requests, and nothing in CI or on the pull request list says
why. These tests are the only place the intended cadence is checked.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CONFIG = Path(__file__).resolve().parents[1] / ".github" / "dependabot.yml"

#: The keys GitHub documents for ``cooldown``; anything else is ignored or rejected.
COOLDOWN_KEYS = {
    "default-days",
    "semver-major-days",
    "semver-minor-days",
    "semver-patch-days",
    "include",
    "exclude",
}

GROUP_UPDATE_TYPES = {"patch", "minor", "major"}


def load() -> dict[str, dict]:
    config = yaml.safe_load(CONFIG.read_text())
    assert config["version"] == 2
    return {entry["package-ecosystem"]: entry for entry in config["updates"]}


def test_pip_is_checked_every_weekday_and_actions_every_week():
    updates = load()

    assert updates["pip"]["schedule"]["interval"] == "daily"
    assert updates["github-actions"]["schedule"]["interval"] == "weekly"


def test_a_weekly_schedule_is_the_only_one_that_names_a_day():
    """``day`` is a weekly-only field; on a daily entry it is a config error."""
    updates = load()

    assert "day" not in updates["pip"]["schedule"]
    assert updates["github-actions"]["schedule"]["day"] == "monday"


def test_every_group_states_the_patterns_it_covers():
    for ecosystem, entry in load().items():
        for name, group in entry.get("groups", {}).items():
            assert group["patterns"], f"{ecosystem}/{name} has no patterns"
            assert set(group["update-types"]) <= GROUP_UPDATE_TYPES, f"{ecosystem}/{name}"


def test_actions_use_only_the_cooldown_keys_they_support():
    """GitHub Actions has no semver, so only ``default-days`` applies to it."""
    updates = load()

    assert set(updates["github-actions"]["cooldown"]) == {"default-days"}
    for entry in updates.values():
        unknown = set(entry.get("cooldown", {})) - COOLDOWN_KEYS
        assert not unknown, f"{entry['package-ecosystem']} has unknown cooldown keys: {unknown}"
