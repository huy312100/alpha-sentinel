"""FX routing: venue/macro/technicals plus a stub for carry and positioning."""

from __future__ import annotations

from core.instruments import AssetClass
from core.models import Layer
from scanners.base import StubScanner
from scanners.shared.listed.macro import MacroScanner
from scanners.shared.listed.market import MarketScanner
from scanners.shared.listed.technicals import TechnicalsScanner


class FxCarryScanner(StubScanner):
    """Future home of rate differentials, CFTC positioning and central-bank dates."""

    layer = Layer.FUNDAMENTALS
    asset_classes = frozenset({AssetClass.FX})
    capability = "FX carry, CFTC positioning and policy calendars"


FX_LAYERS = (MarketScanner, FxCarryScanner, MacroScanner, TechnicalsScanner)

__all__ = ["FX_LAYERS", "FxCarryScanner"]
