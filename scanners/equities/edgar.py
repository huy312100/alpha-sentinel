"""Equity routing: SEC EDGAR fundamentals plus a stub for ownership flow."""

from __future__ import annotations

from core.instruments import AssetClass
from core.models import Layer
from scanners.base import StubScanner
from scanners.shared.listed.fundamentals import FundamentalsScanner
from scanners.shared.listed.macro import MacroScanner
from scanners.shared.listed.market import MarketScanner
from scanners.shared.listed.technicals import TechnicalsScanner


class EquityOwnershipScanner(StubScanner):
    """Future home of Form 4 insider trades and 13F institutional changes."""

    layer = Layer.FUNDAMENTALS
    asset_classes = frozenset({AssetClass.EQUITY})
    capability = "insider (Form 4) and institutional (13F) ownership changes"


EQUITY_LAYERS = (MarketScanner, FundamentalsScanner, EquityOwnershipScanner, MacroScanner, TechnicalsScanner)

__all__ = ["EQUITY_LAYERS", "EquityOwnershipScanner"]
