"""Data collection layers.

Each module owns exactly one layer of the scan stack and exposes a
:class:`~scanners.base.BaseScanner` subclass. :class:`scanners.pipeline.ScanPipeline`
wires them together; nothing here talks to the AI engine or storage.
"""

from __future__ import annotations

from scanners.airdrop import AirdropScanner
from scanners.base import BaseScanner, ScanContext
from scanners.derivatives import DerivativesScanner
from scanners.dex import DexMarket, DexScanner, load_market
from scanners.fundamentals import FundamentalsScanner
from scanners.macro import MacroScanner
from scanners.market import MarketScanner
from scanners.market_data import MarketQuote, load_quote
from scanners.onchain import OnChainScanner
from scanners.pipeline import CollectedSnapshot, ScanPipeline, ScannerHealth
from scanners.security import SecurityScanner
from scanners.series import PriceSeries, load_price_series
from scanners.social import SocialScanner
from scanners.technicals import TechnicalsScanner
from scanners.wash import WashTradingFilter

__all__ = [
    "AirdropScanner",
    "BaseScanner",
    "CollectedSnapshot",
    "DerivativesScanner",
    "DexMarket",
    "DexScanner",
    "FundamentalsScanner",
    "MacroScanner",
    "MarketQuote",
    "MarketScanner",
    "OnChainScanner",
    "PriceSeries",
    "ScanContext",
    "ScanPipeline",
    "ScannerHealth",
    "SecurityScanner",
    "SocialScanner",
    "TechnicalsScanner",
    "WashTradingFilter",
    "load_market",
    "load_price_series",
    "load_quote",
]
