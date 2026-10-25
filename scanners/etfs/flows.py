"""ETF routing: venue/macro/technicals plus a stub for holdings and flows."""

from __future__ import annotations

from core.instruments import AssetClass
from core.models import Layer
from scanners.base import StubScanner
from scanners.macro import MacroScanner
from scanners.market import MarketScanner
from scanners.technicals import TechnicalsScanner


class EtfFlowsScanner(StubScanner):
    """Future home of creation/redemption flows, holdings and premium/discount."""

    layer = Layer.FUNDAMENTALS
    asset_classes = frozenset({AssetClass.ETF})
    capability = "ETF holdings, creation/redemption flows and premium/discount"


ETF_LAYERS = (MarketScanner, EtfFlowsScanner, MacroScanner, TechnicalsScanner)

__all__ = ["ETF_LAYERS", "EtfFlowsScanner"]
