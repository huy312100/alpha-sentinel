"""The bot package: watchlists, the scan cycle, alerting and the CLI.

Nothing here talks to exchanges directly - collection is owned by
:mod:`scanners.pipeline` and the audit by :mod:`ai_engine.dual_audit_flow`. This
package is the thin orchestration layer that decides *what* to scan, *when* to
alert and *where* results land.
"""

from __future__ import annotations

__version__ = "1.0.0"

from bot.alerts import AlertResult, TelegramAlerter, format_verdict, should_alert  # noqa: E402
from bot.engine import ScanEngine, ScanReport, TokenResult  # noqa: E402
from bot.watchlist import (  # noqa: E402
    WatchToken,
    Watchlist,
    WatchlistBundle,
    WatchlistError,
    WatchlistLoader,
    load_watchlists,
)

__all__ = [
    "AlertResult",
    "ScanEngine",
    "ScanReport",
    "TelegramAlerter",
    "TokenResult",
    "WatchToken",
    "Watchlist",
    "WatchlistBundle",
    "WatchlistError",
    "WatchlistLoader",
    "__version__",
    "format_verdict",
    "load_watchlists",
    "should_alert",
]
