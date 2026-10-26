"""The one configuration exception, shared by the settings and endpoint loaders.

It lives in its own module so :mod:`config.endpoints` can raise it without
importing :mod:`config.settings` (which imports :mod:`config.endpoints`). The old
import path keeps working: ``from config.settings import ConfigError``.
"""

from __future__ import annotations


class ConfigError(RuntimeError):
    """Raised when the environment cannot be parsed into a valid configuration."""


__all__ = ["ConfigError"]
