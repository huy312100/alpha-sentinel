"""Manual smoke test: ``python -m scanners --token 0x.. --chain ethereum``.

Runs the full collection cycle for one instrument and prints every layer signal,
so a provider outage can be diagnosed without touching the bot or the dashboard.
Any market works: pass ``--asset-class equity --symbol AAPL`` (or ``fx`` /
``commodity`` / ``index`` / ``rate``) instead of a contract address.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from config.settings import Settings
from core.instruments import AssetClass, Instrument
from core.logging import configure_logging
from core.models import TokenSnapshot
from scanners.pipeline import ScanPipeline


def build_target(args: argparse.Namespace, settings: Settings) -> TokenSnapshot:
    """Turn CLI arguments into a snapshot for any asset class."""
    kind = AssetClass.coerce(args.asset_class)
    if kind.is_crypto:
        return TokenSnapshot(
            token_address=args.token,
            chain=args.chain or settings.runtime.default_chain,
            symbol=args.symbol,
        )
    instrument = Instrument.build(kind, symbol=args.symbol or args.token, venue=args.venue)
    return TokenSnapshot(
        token_address=instrument.identity,
        chain=instrument.venue,
        asset_class=kind.value,
        symbol=instrument.symbol,
        meta=instrument.to_meta(),
    )


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scanners", description="Collect every layer for one instrument.")
    parser.add_argument("--token", default="", help="contract address (crypto) or ticker")
    parser.add_argument("--symbol", default="", help="symbol hint (required for non-crypto markets)")
    parser.add_argument("--chain", default=None, help="chain slug (crypto; defaults to runtime default)")
    parser.add_argument(
        "--asset-class",
        default="crypto",
        dest="asset_class",
        help="crypto|equity|etf|index|fx|commodity|rate (default: crypto)",
    )
    parser.add_argument("--venue", default="", help="venue slug override (e.g. nyse, cme)")
    parser.add_argument("--json", action="store_true", help="also dump the raw collected payload")
    args = parser.parse_args(argv)

    if not args.token and not args.symbol:
        parser.error("provide --token (crypto address) or --symbol (any market)")

    settings = Settings.load()
    configure_logging(level="WARNING", json_output=False)
    token = build_target(args, settings)

    async with ScanPipeline(settings) as pipeline:
        collected = await pipeline.collect(token)
        security = collected.snapshot.security
        wash = collected.snapshot.wash
        print(f"\n{collected.snapshot.symbol or collected.snapshot.token_address} "
              f"[{collected.snapshot.asset_class}] on {token.chain}")
        print(f"prior={collected.prior_score:.2f} collected in {collected.duration_ms:.0f} ms")
        print(f"security flags={security.flags or 'none'}")
        print(f"wash suspicion={wash.suspicion_score if wash.evaluated else 'n/a'}")
        for signal in collected.snapshot.signals:
            print(f"  {signal.layer.value:<12} {signal.status.value:<12} {signal.score:6.2f}  {signal.summary}")
        if args.json:
            print(json.dumps(collected.to_dict(), indent=2, default=str))
        print("\nhealth:", json.dumps(pipeline.health_report(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    raise SystemExit(asyncio.run(main()))
