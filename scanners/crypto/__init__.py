"""The crypto layer manifest consumed by :mod:`scanners.registry`.

``layer1_onchain`` .. ``layer8_security`` are the eight scoring layers, in
execution order. ``providers/`` holds the vendor adapters they call - request
shapes and payload parsing only, never score arithmetic - and requires no
knowledge of the registry.
"""

from __future__ import annotations

from scanners.crypto.layer1_onchain import CryptoOnChainScanner
from scanners.crypto.layer2_derivatives import CryptoDerivativesScanner
from scanners.crypto.layer3_social import CryptoSocialScanner
from scanners.crypto.layer4_dex import CryptoDexScanner
from scanners.crypto.layer5_ecosystem import CryptoEcosystemScanner
from scanners.crypto.layer6_tokenomics import CryptoTokenomicsScanner
from scanners.crypto.layer7_technicals import CryptoTechnicalsScanner
from scanners.crypto.layer8_security import CryptoSecurityScanner

#: Exactly eight dedicated layers, in execution/display order.
CRYPTO_LAYERS = (
    CryptoOnChainScanner,
    CryptoDerivativesScanner,
    CryptoSocialScanner,
    CryptoDexScanner,
    CryptoEcosystemScanner,
    CryptoTokenomicsScanner,
    CryptoTechnicalsScanner,
    CryptoSecurityScanner,
)

__all__ = [
    "CRYPTO_LAYERS",
    "CryptoDerivativesScanner",
    "CryptoDexScanner",
    "CryptoEcosystemScanner",
    "CryptoOnChainScanner",
    "CryptoSecurityScanner",
    "CryptoSocialScanner",
    "CryptoTechnicalsScanner",
    "CryptoTokenomicsScanner",
]
