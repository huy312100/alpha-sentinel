"""Index routing: venue/macro/technicals plus a stub for constituents/breadth."""

from __future__ import annotations

from core.instruments import AssetClass
from core.models import Layer
from scanners.base import StubScanner
from scanners.shared.listed.macro import MacroScanner
from scanners.shared.listed.market import MarketScanner
from scanners.shared.listed.technicals import TechnicalsScanner


class IndexBreadthScanner(StubScanner):
    """Future home of constituents, breadth and index-level valuation."""

    layer = Layer.FUNDAMENTALS
    asset_classes = frozenset({AssetClass.INDEX})
    capability = "index constituents, breadth and valuation"


INDEX_LAYERS = (MarketScanner, IndexBreadthScanner, MacroScanner, TechnicalsScanner)

__all__ = ["INDEX_LAYERS", "IndexBreadthScanner"]
