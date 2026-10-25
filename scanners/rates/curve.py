"""Rates routing: venue/macro/technicals plus a curve and auction stub."""

from __future__ import annotations

from core.instruments import AssetClass
from core.models import Layer
from scanners.base import StubScanner
from scanners.macro import MacroScanner
from scanners.market import MarketScanner
from scanners.technicals import TechnicalsScanner


class RatesCurveScanner(StubScanner):
    """Future home of curve slope, auction tails and real-yield decomposition."""

    layer = Layer.FUNDAMENTALS
    asset_classes = frozenset({AssetClass.RATE})
    capability = "rates curve, auction demand and real yields"


RATE_LAYERS = (MarketScanner, RatesCurveScanner, MacroScanner, TechnicalsScanner)

__all__ = ["RATE_LAYERS", "RatesCurveScanner"]
