"""Crypto vendor adapters: one module per external data source family.

Layers in :mod:`scanners.crypto` own *scoring*; these modules own *talking to
vendors* - request shapes, payload parsing and the deterministic helpers that
turn a raw payload into a number (funding, holder concentration, pool selection).
Keeping the split means a provider swap (DexScreener -> GeckoTerminal, GoPlus ->
another honeypot API) never touches a score.

Nothing here imports a layer, so the dependency arrow only ever points
``layerN -> providers -> shared``. Each module still carries the class its layer
delegates to (``DerivativesScanner``, ``SecurityScanner``, ...), so its docstring
reads like a layer description on purpose.
"""

from __future__ import annotations

__all__ = [
    "airdrop",
    "derivatives",
    "dex",
    "onchain",
    "security",
    "social",
    "wash",
]
