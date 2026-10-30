"""The stack hardening that no other gate would notice.

Both settings asserted here fail silently. Unbounded ``json-file`` logs only make
themselves known when the disk fills, and a ``0.0.0.0`` publication only makes
itself known when someone else finds the dashboard. Neither breaks a build, so
nothing else in CI would catch a regression.
"""

from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def services() -> dict[str, dict]:
    """The compose services, with the shared ``x-service`` anchor already merged.

    ``yaml.safe_load`` resolves ``<<`` for mapping anchors, so a service that
    inherits ``logging`` from the anchor still reports it here. If that ever
    stops holding, the first test fails instead of silently passing.
    """
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return document["services"]


def test_every_service_bounds_its_json_logs():
    """A soak must not be able to fill the host disk with its own log file."""
    for name, service in services().items():
        logging = service["logging"]
        assert logging["driver"] == "json-file", f"{name} uses an unexpected log driver"
        assert logging["options"]["max-size"] == "20m", f"{name} has no log size cap"
        assert logging["options"]["max-file"] == "5", f"{name} has no log file cap"


def test_the_dashboard_is_published_on_loopback_by_default():
    """Exposing the dashboard has to be an explicit edit, never the default."""
    port = services()["dashboard"]["ports"][0]
    assert port.startswith("${DASHBOARD_BIND:-127.0.0.1}:"), port


def test_the_container_still_listens_on_every_interface():
    """The publish address protects the host; the process inside must still bind
    all interfaces, or the docker port map has nothing to forward to."""
    command = services()["dashboard"]["command"]
    assert "--server.address=0.0.0.0" in command
