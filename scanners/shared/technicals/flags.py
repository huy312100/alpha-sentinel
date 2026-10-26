"""Shared technical flags and the score ceilings they imply.

Flags are the layer-independent part of a technical read: an exhausted RSI or a
broken structure means the same thing whether the bars came from a DEX pool or
an exchange. :func:`apply_flag_caps` is what makes a flag *bite* - a downtrend
that scores 78 on its components must still not be presented as a strong buy.
"""

from __future__ import annotations

from typing import Sequence

from scanners.base import clamp


def rsi_flags(
    rsi14: float | None,
    *,
    overbought: float = 78.0,
    oversold: float = 25.0,
) -> list[str]:
    """The shared exhaustion flags: identical thresholds in every market."""
    flags: list[str] = []
    if rsi14 is not None and rsi14 >= overbought:
        flags.append("rsi_overbought")
    if rsi14 is not None and rsi14 <= oversold:
        flags.append("rsi_oversold")
    return flags


def structure_flags(price: float | None, ema20: float | None, ema50: float | None) -> list[str]:
    """Price under both EMAs with EMA20 under EMA50 is a downtrend, not a dip."""
    if price is not None and ema20 is not None and ema50 is not None and price < ema20 < ema50:
        return ["downtrend_structure"]
    return []


def apply_flag_caps(
    score: float,
    flags: Sequence[str],
    *,
    downtrend_cap: float = 42.0,
    overbought_cap: float = 60.0,
) -> float:
    """Ceilings implied by the shared flags, applied identically by both layers.

    A downtrend structure or an exhausted RSI must never be allowed to sit in
    "strong buy" territory just because other components are loud.
    """
    if "downtrend_structure" in flags:
        score = min(score, downtrend_cap)
    if "rsi_overbought" in flags:
        score = min(score, overbought_cap)
    return clamp(score)


__all__ = ["apply_flag_caps", "rsi_flags", "structure_flags"]
