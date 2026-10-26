"""Data collection layers.

Layout
------
``base`` / ``registry`` / ``pipeline``
    Framework: the layer contract, the ``asset_class -> layer`` routing table and
    the coordinator that fans layers out and isolates their failures.
``crypto/``
    Eight self-contained layers (``layer1_onchain`` .. ``layer8_security``). A
    layer owns its vendor clients - endpoint, payload parsing and scoring live in
    the same module - so there is no separate adapter pen to keep in sync.
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
from scanners.crypto.layer1_onchain import CryptoOnChainScanner
from scanners.crypto.layer2_derivatives import CryptoDerivativesScanner
from scanners.crypto.layer3_social import CryptoSocialScanner
from scanners.crypto.layer4_dex import CryptoDexScanner, DexMarket, WashTradingFilter, load_market
from scanners.crypto.layer5_ecosystem import CryptoEcosystemScanner
from scanners.crypto.layer6_tokenomics import CryptoTokenomicsScanner
from scanners.crypto.layer7_technicals import CryptoTechnicalsScanner
from scanners.crypto.layer8_security import CryptoSecurityScanner
from scanners.pipeline import CollectedSnapshot, ScanPipeline, ScannerHealth
from scanners.shared.listed.fundamentals import FundamentalsScanner
from scanners.shared.listed.macro import MacroScanner
from scanners.shared.listed.market import MarketScanner
from scanners.shared.listed.technicals import TechnicalsScanner
from scanners.shared.market_data import MarketQuote, load_quote
from scanners.shared.series import PriceSeries, load_price_series

__all__ = [
    "BaseScanner",
    "CollectedSnapshot",
    "CryptoDerivativesScanner",
    "CryptoDexScanner",
    "CryptoEcosystemScanner",
    "CryptoOnChainScanner",
    "CryptoSecurityScanner",
    "CryptoSocialScanner",
    "CryptoTechnicalsScanner",
    "CryptoTokenomicsScanner",
    "DexMarket",
    "FundamentalsScanner",
    "MacroScanner",
    "MarketQuote",
    "MarketScanner",
    "PriceSeries",
    "ScanContext",
    "ScanPipeline",
    "ScannerHealth",
    "TechnicalsScanner",
    "WashTradingFilter",
    "load_market",
    "load_price_series",
    "load_quote",
]
