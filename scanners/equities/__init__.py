"""Equity layers: venue, SEC fundamentals, ownership, macro and technicals.

``scanners.fundamentals`` already reads SEC EDGAR company facts, so equities have a
real fundamentals layer today. Insider/institutional ownership (Form 4, 13F) is a
routed stub - ``SKIPPED`` with zero weight - until that collector lands.
"""

from __future__ import annotations

from scanners.equities.edgar import EQUITY_LAYERS, EquityOwnershipScanner

__all__ = ["EQUITY_LAYERS", "EquityOwnershipScanner"]
