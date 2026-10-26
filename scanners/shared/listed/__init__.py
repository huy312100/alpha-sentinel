"""Generic non-crypto layers, composed by every placeholder market package.

These four layers are market-agnostic: they read venue quotes (``market``), the
macro backdrop (``macro``), SEC company facts (``fundamentals``) and candles
(``technicals``) for any instrument that is not a DEX pair. They live under
``shared`` because ``equities``, ``etfs``, ``indices``, ``forex``, ``commodities``
and ``rates`` all route to the *same* implementations today.

As a market grows providers of its own, its class-specific layer moves into that
market's package (the way crypto's eight layers already do) and stops being
shared. Nothing outside this package imports the modules directly except the
market manifests and the pipeline.
"""

from __future__ import annotations

from scanners.shared.listed.fundamentals import FundamentalsScanner
from scanners.shared.listed.macro import MacroScanner
from scanners.shared.listed.market import MarketScanner
from scanners.shared.listed.technicals import TechnicalsScanner

__all__ = [
    "FundamentalsScanner",
    "MacroScanner",
    "MarketScanner",
    "TechnicalsScanner",
]
