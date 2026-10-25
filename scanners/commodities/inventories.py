"""Commodity routing: venue/macro/technicals plus inventory and curve stubs."""

from __future__ import annotations

from core.instruments import AssetClass
from core.models import Layer
from scanners.base import StubScanner
from scanners.shared.listed.macro import MacroScanner
from scanners.shared.listed.market import MarketScanner
from scanners.shared.listed.technicals import TechnicalsScanner


class CommodityInventoryScanner(StubScanner):
    """Future home of EIA/LME inventories, curve shape and seasonality."""

    layer = Layer.FUNDAMENTALS
    asset_classes = frozenset({AssetClass.COMMODITY})
    capability = "commodity inventories, futures curve and seasonality"


COMMODITY_LAYERS = (MarketScanner, CommodityInventoryScanner, MacroScanner, TechnicalsScanner)

__all__ = ["COMMODITY_LAYERS", "CommodityInventoryScanner"]
