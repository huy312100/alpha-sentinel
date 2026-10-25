"""Data collection layers.

Layout
------
``base`` / ``registry`` / ``pipeline``
    Framework: the layer contract, the ``asset_class -> layer`` routing table and
    the coordinator that fans layers out and isolates their failures.
``crypto/``
    Eight dedicated layers (``layer1_onchain`` .. ``layer8_security``) plus
    ``crypto/providers/`` - the vendor adapters (DexScreener, GeckoTerminal,
    GoPlus, Coinglass, LunarCrush, ...) that own request shapes and payload
    parsing, never scoring.
``shared/``
    Everything a market does not own by itself: ``http_client`` (retry + per-host
    rate limit), ``technicals`` (indicator maths), ``market_data`` and ``series``
    (quote/candle transport) and ``shared/listed`` - the generic non-crypto
    layers the market placeholders compose.
``equities/ etfs/ indices/ forex/ commodities/ rates/``
    Per-market manifests. Each is routed today; class-specific layers start as
    routed stubs and replace the shared ones as real providers land.

Nothing in this package talks to the AI engine or storage.
"""

from __future__ import annotations

from scanners.base import BaseScanner, ScanContext
from scanners.crypto.providers.airdrop import AirdropScanner
from scanners.crypto.providers.derivatives import DerivativesScanner
from scanners.crypto.providers.dex import DexMarket, DexScanner, load_market
from scanners.crypto.providers.onchain import OnChainScanner
from scanners.crypto.providers.security import SecurityScanner
from scanners.crypto.providers.social import SocialScanner
from scanners.crypto.providers.wash import WashTradingFilter
from scanners.pipeline import CollectedSnapshot, ScanPipeline, ScannerHealth
from scanners.shared.listed.fundamentals import FundamentalsScanner
from scanners.shared.listed.macro import MacroScanner
from scanners.shared.listed.market import MarketScanner
from scanners.shared.listed.technicals import TechnicalsScanner
from scanners.shared.market_data import MarketQuote, load_quote
from scanners.shared.series import PriceSeries, load_price_series

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
