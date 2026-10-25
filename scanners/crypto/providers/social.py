"""Social layer: attention velocity and sentiment (LunarCrush v4, optional).

Social data is the layer most likely to be *missing* rather than wrong, so this
scanner is explicit: with no key it returns ``SKIPPED`` and the weighting engine
simply renormalises the other six layers. It never fabricates a neutral 50.
"""

from __future__ import annotations

from typing import Any

from core.http import request_json, to_float
from core.instruments import AssetClass
from core.models import Layer, SignalStatus, LayerSignal, TokenSnapshot
from scanners.base import BaseScanner, ScanContext, linear, log_scale

LUNARCRUSH_COIN_URL = "https://lunarcrush.com/api4/public/coins/{coin}/v1"


def _as_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        metrics = data.get("metrics")
        return metrics if isinstance(metrics, dict) else data
    return payload


class SocialScanner(BaseScanner):
    layer = Layer.SOCIAL
    weight = 0.7
    asset_classes = frozenset({AssetClass.CRYPTO})

    async def collect(self, token: TokenSnapshot, ctx: ScanContext) -> LayerSignal:  # noqa: C901
        if not self.settings.social.lunarcrush_api_key.is_configured():
            return self.skipped(token, "LUNARCRUSH_API_KEY not configured")

        coin = (token.symbol or "").strip().upper()
        if not coin:
            return self.skipped(token, "token symbol unknown")

        payload = await request_json(
            ctx.session,
            LUNARCRUSH_COIN_URL.format(coin=coin),
            headers={"Authorization": f"Bearer {self.settings.social.lunarcrush_api_key.get_secret_value()}"},
        )
        data = _as_dict(payload)
        if not data:
            return self.unavailable(token, f"LunarCrush returned nothing for {coin}")

        volume = to_float(data.get("social_volume_24h"))
        sentiment = to_float(data.get("sentiment"))
        galaxy = to_float(data.get("galaxy_score"))
        contributors = to_float(data.get("social_contributors"))
        alt_rank = to_float(data.get("alt_rank"))
        if volume is None and sentiment is None and galaxy is None:
            return self.unavailable(token, f"LunarCrush has no social metrics for {coin}")

        flags: list[str] = []
        if sentiment is not None and sentiment < 35.0:
            flags.append("social_sentiment_negative")
        if volume is not None and volume < 25.0:
            flags.append("social_volume_negligible")

        components: list[tuple[float, float]] = []
        if volume is not None:
            components.append((0.40, log_scale(volume, 50.0, 50_000.0)))
        if sentiment is not None:
            components.append((0.30, linear(sentiment, 30.0, 75.0)))
        if galaxy is not None:
            components.append((0.30, linear(galaxy, 20.0, 80.0)))
        total_weight = sum(weight for weight, _ in components) or 1.0
        score = sum(weight * value for weight, value in components) / total_weight

        confidence = 0.75 if sentiment is not None and volume is not None else 0.5
        status = SignalStatus.OK if len(components) == 3 else SignalStatus.DEGRADED
        summary = (
            f"{coin}: social volume {volume if volume is not None else 'n/a'}, "
            f"sentiment {sentiment if sentiment is not None else 'n/a'}, "
            f"galaxy {galaxy if galaxy is not None else 'n/a'}"
            + (f", alt rank #{int(alt_rank)}" if alt_rank else "")
        )
        return self.signal(
            token,
            score=score,
            confidence=confidence,
            summary=summary,
            status=status,
            metrics={
                "social_volume_24h": volume,
                "sentiment": sentiment,
                "galaxy_score": galaxy,
                "social_contributors": contributors,
                "alt_rank": alt_rank,
            },
            flags=flags,
        )


__all__ = ["LUNARCRUSH_COIN_URL", "SocialScanner"]
