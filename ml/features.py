"""The feature contract: one collected token snapshot -> one flat numeric row.

Why a separate module
---------------------
A model is only as reproducible as its column layout. Everything that decides
*what a row is* lives here and nowhere else, so training and inference cannot
drift apart, and :data:`ml.FEATURE_SCHEMA_VERSION` gives us a way to say "this
artifact was trained on a layout that no longer means the same thing".

Conventions
-----------
* **Missing is ``NaN``, never ``0``.** A provider that failed is not a token
  with zero volume; a tree model can route ``NaN`` down its own branch, and a
  linear model sees an explicit hole instead of a silent zero.
* ``inf`` is also folded into ``NaN``, so a division by a zero market cap cannot
  poison a column.
* Booleans become ``0.0`` / ``1.0``; layer statuses become an ordinal in
  :data:`STATUS_CODES`, where ``skipped`` (-1) is deliberately distinct from
  ``unavailable`` (0) - "not applicable" and "the provider died" are different
  facts and a model should be able to tell them apart.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Iterator, Mapping, Sequence

from core.models import Layer, SignalStatus, TokenSnapshot

#: Re-exported for callers that only import this module.
from ml import FEATURE_SCHEMA_VERSION  # noqa: F401

#: Ordinal encoding for :class:`core.models.SignalStatus`.
#: ``skipped`` is negative on purpose: a layer that does not apply to the asset
#: class is not evidence of a failed provider.
STATUS_CODES: Mapping[str, float] = {
    SignalStatus.OK.value: 1.0,
    SignalStatus.DEGRADED.value: 0.5,
    SignalStatus.UNAVAILABLE.value: 0.0,
    SignalStatus.SKIPPED.value: -1.0,
}

#: Every layer in enum order, so a row has the same keys whatever came back.
LAYERS: tuple[str, ...] = tuple(layer.value for layer in Layer)

#: Per-layer columns, in the order they are emitted.
LAYER_SUFFIXES: tuple[str, ...] = ("score", "confidence", "weight", "status", "contributes", "flag_count")

#: Raw snapshot numbers, kept under their own names.
MARKET_FIELDS: tuple[str, ...] = (
    "price_usd",
    "market_cap_usd",
    "fdv_usd",
    "liquidity_usd",
    "volume_24h_usd",
    "volume_1h_usd",
    "price_change_24h_pct",
    "price_change_1h_pct",
    "holder_count",
    "age_hours",
)

#: Sizes of a position against the token's own structure - the part a model
#: needs that a raw dollar figure cannot express.
DERIVED_FIELDS: tuple[str, ...] = (
    "turnover_24h_ratio",
    "liquidity_to_mcap",
    "fdv_to_mcap",
    "volume_1h_share",
    "mcap_per_holder",
)

#: Present for every row, used for the coverage/confidence estimate. If these
#: are mostly missing the snapshot is too thin to trust any prediction.
CORE_FEATURES: tuple[str, ...] = MARKET_FIELDS + ("prior_score",)

#: A layer may report anything in ``metrics``; only the first N (sorted) keys are
#: flattened so an exotic provider cannot explode the schema.
MAX_METRIC_KEYS_PER_LAYER = 24

_NAN = float("nan")


def _num(value: Any) -> float:
    """Coerce to a finite float, or ``NaN``. Never raises."""
    if value is None or isinstance(value, str):
        return _NAN
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _NAN
    return number if math.isfinite(number) else _NAN


def _ratio(numerator: Any, denominator: Any) -> float:
    """``numerator / denominator`` with both holes and infinities removed."""
    top, bottom = _num(numerator), _num(denominator)
    if math.isnan(top) or math.isnan(bottom) or bottom <= 0:
        return _NAN
    value = top / bottom
    return value if math.isfinite(value) else _NAN


def _metric_key(layer: str, key: str) -> str:
    cleaned = "".join(char if (char.isalnum() or char == "_") else "_" for char in str(key).lower()).strip("_")
    return f"{layer}_{cleaned}" if cleaned else f"{layer}_metric"


def _scalar_metrics(layer: str, metrics: Mapping[str, Any]) -> Iterator[tuple[str, float]]:
    emitted = 0
    for key in sorted(metrics):
        value = metrics[key]
        if isinstance(value, (bool, int, float)):
            number = _num(value)
            if not math.isnan(number):
                yield _metric_key(layer, key), number
                emitted += 1
                if emitted >= MAX_METRIC_KEYS_PER_LAYER:
                    return


def flatten(snapshot: TokenSnapshot) -> dict[str, float]:
    """One snapshot -> the canonical feature row.

    Deterministic: identical snapshots produce identically ordered keys, which is
    what lets two Parquet partitions concatenate cleanly.
    """
    row: dict[str, float] = {}

    for name in MARKET_FIELDS:
        row[name] = _num(getattr(snapshot, name, None))

    row["turnover_24h_ratio"] = _ratio(snapshot.volume_24h_usd, snapshot.market_cap_usd)
    row["liquidity_to_mcap"] = _ratio(snapshot.liquidity_usd, snapshot.market_cap_usd)
    row["fdv_to_mcap"] = _ratio(snapshot.fdv_usd, snapshot.market_cap_usd)
    row["volume_1h_share"] = _ratio(snapshot.volume_1h_usd, snapshot.volume_24h_usd)
    row["mcap_per_holder"] = _ratio(snapshot.market_cap_usd, snapshot.holder_count)

    security = snapshot.security
    row["sec_available"] = 1.0 if security.available else 0.0
    row["sec_honeypot"] = 1.0 if security.honeypot else 0.0
    row["sec_buy_tax_pct"] = _num(security.buy_tax_pct)
    row["sec_sell_tax_pct"] = _num(security.sell_tax_pct)
    row["sec_mintable"] = 1.0 if security.mintable else 0.0
    row["sec_owner_privileges"] = 1.0 if security.owner_privileges else 0.0
    row["sec_hidden_owner"] = 1.0 if security.hidden_owner else 0.0
    row["sec_is_proxy"] = 1.0 if security.is_proxy else 0.0
    row["sec_can_take_back_ownership"] = 1.0 if security.can_take_back_ownership else 0.0
    row["sec_blacklist_enabled"] = 1.0 if security.blacklist_enabled else 0.0
    row["sec_transfer_pausable"] = 1.0 if security.transfer_pausable else 0.0
    row["sec_holder_count"] = _num(security.holder_count)
    row["sec_top10_holder_pct"] = _num(security.top10_holder_pct)
    row["sec_lp_locked_pct"] = _num(security.lp_locked_pct)
    row["sec_flag_count"] = float(len(security.flags))
    row["sec_critical_flag_count"] = float(len(security.critical_flags()))

    wash = snapshot.wash
    row["wash_evaluated"] = 1.0 if wash.evaluated else 0.0
    row["wash_suspicion_score"] = _num(wash.suspicion_score)
    row["wash_trade_count"] = _num(wash.trade_count)
    row["wash_unique_wallets"] = _num(wash.unique_wallets)
    row["wash_avg_trade_usd"] = _num(wash.avg_trade_usd)
    row["wash_median_trade_usd"] = _num(wash.median_trade_usd)
    row["wash_unique_wallet_ratio"] = _num(wash.unique_wallet_ratio)
    row["wash_round_amount_ratio"] = _num(wash.round_amount_ratio)
    row["wash_self_trade_ratio"] = _num(wash.self_trade_ratio)
    row["wash_buy_sell_imbalance"] = _num(wash.buy_sell_imbalance)
    row["wash_flag_count"] = float(len(wash.flags))

    by_layer = {signal.layer.value: signal for signal in snapshot.signals}
    contributing = 0
    for layer in LAYERS:
        signal = by_layer.get(layer)
        if signal is None:
            # Absent is not the same as skipped: leave the score a hole and say
            # so, rather than inventing a neutral 50.
            row[f"{layer}_score"] = _NAN
            row[f"{layer}_confidence"] = _NAN
            row[f"{layer}_weight"] = _NAN
            row[f"{layer}_status"] = _NAN
            row[f"{layer}_contributes"] = 0.0
            row[f"{layer}_flag_count"] = _NAN
            continue
        row[f"{layer}_score"] = _num(signal.score)
        row[f"{layer}_confidence"] = _num(signal.confidence)
        row[f"{layer}_weight"] = _num(signal.weight)
        row[f"{layer}_status"] = STATUS_CODES.get(signal.status.value, _NAN)
        row[f"{layer}_contributes"] = 1.0 if signal.contributes else 0.0
        row[f"{layer}_flag_count"] = float(len(signal.flags))
        if signal.contributes:
            contributing += 1
        for key, value in _scalar_metrics(layer, signal.metrics or {}):
            # A metric name may collide with a structural column; the explicit
            # column wins, because it is the documented contract.
            row.setdefault(key, value)

    row["layers_contributing"] = float(contributing)
    # Filled by the caller (the engine knows the prior); present so
    # :data:`CORE_FEATURES` has a stable home and inference can match training.
    row.setdefault("prior_score", _NAN)
    return row


def align(row: Mapping[str, Any], feature_names: Sequence[str]) -> list[float]:
    """Project ``row`` onto a trained model's column order, ``NaN`` where absent.

    Inference must never blow up because a provider started reporting a new
    metric or stopped reporting an old one; it just loses that input.
    """
    return [_num(row.get(name)) for name in feature_names]


def coverage(row: Mapping[str, Any]) -> float:
    """Share of :data:`CORE_FEATURES` that are actually present, ``0.0``-``1.0``."""
    present = sum(1 for name in CORE_FEATURES if not math.isnan(_num(row.get(name))))
    return present / len(CORE_FEATURES) if CORE_FEATURES else 0.0


def feature_columns(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Union of the feature keys present across ``rows``, in first-seen order."""
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key, None)
    return list(seen)


__all__ = [
    "CORE_FEATURES",
    "DERIVED_FIELDS",
    "FEATURE_SCHEMA_VERSION",
    "LAYERS",
    "LAYER_SUFFIXES",
    "MARKET_FIELDS",
    "MAX_METRIC_KEYS_PER_LAYER",
    "STATUS_CODES",
    "align",
    "coverage",
    "feature_columns",
    "flatten",
]
