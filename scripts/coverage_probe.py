"""Layer coverage: what data an instrument actually reaches the auditor with.

Why this exists rather than a soak. A verdict is not only the agents' judgement - it
is the judgement *on the layers that returned*. ``RISK_SYSTEM_PROMPT`` rule 2 tells
the auditor "if the data is missing, treat it as unverified risk, not as safe", and
severity ``critical`` implies ``veto=true``, so a coverage gap does not produce a
thinner verdict, it produces a VETO that reads like a finding. The first real cycle
this repo ran on a live endpoint returned 24 of 24 VETO with zero errors, because
every asset class had three ``ok`` layers and the rest were skipped for missing keys
or lost to rate limits.

``--status`` answers "did the run finish". This answers "what did it know", per layer,
across a real batch - through ``ScanPipeline.collect_many``, the exact call the engine
makes, with the same session, concurrency and per-host buckets.

It makes live vendor calls and spends free-tier budget, so it is not part of ``ci``:
one run covers the whole watchlist in a few minutes. Run it after adding a provider
key, after changing ``TOKEN_CONCURRENCY``, and before believing a veto.

    make coverage
    python -m scripts.coverage_probe --asset-class crypto --limit 4
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import time
from typing import Iterable, Sequence

from bot.watchlist import WatchlistLoader
from config.settings import Settings
from core.instruments import AssetClass
from core.models import LayerSignal, TokenSnapshot
from scanners.pipeline import ScanPipeline

#: Order matters: it is the report's column order, and "ok" first is the point.
STATUS_COLUMNS: tuple[str, ...] = ("ok", "degraded", "unavailable", "skipped")


def tally(signals: Iterable[LayerSignal]) -> "collections.Counter[tuple[str, str]]":
    """Count layer signals by ``(layer, status)``.

    Split out from the network work so the classification is testable without a
    vendor: a coverage report that miscounts is worse than no report, because the
    table is what an operator acts on.
    """
    counts: collections.Counter = collections.Counter()
    for signal in signals:
        counts[(signal.layer.value, signal.status.value)] += 1
    return counts


def format_table(counts: "collections.Counter[tuple[str, str]]", *, rows: int) -> str:
    """The per-layer matrix, plus the one number the fix is judged on."""
    lines = [f"  {'layer':13s}" + "".join(f"{name:>13s}" for name in STATUS_COLUMNS)]
    for layer in sorted({layer for layer, _ in counts}):
        cells = "".join(f"{counts.get((layer, name), 0):13d}" for name in STATUS_COLUMNS)
        lines.append(f"  {layer:13s}{cells}")
    covered = {layer for (layer, status), n in counts.items() if status == "ok" and n}
    lines.append(f"  layers with any usable data: {len(covered)} of {len({layer for layer, _ in counts})}")
    lines.append(f"  signals tallied: {rows}")
    return "\n".join(lines)


def _reasons(signals: Sequence[LayerSignal]) -> list[str]:
    """One explanation per losing layer - the difference between 'missing' and 'why'."""
    seen: dict[str, str] = {}
    for signal in signals:
        status = signal.status.value
        if status in ("ok", "degraded"):
            continue
        layer = signal.layer.value
        if layer in seen:
            continue
        detail = (signal.error or signal.summary or "").replace(f"{layer} {status}: ", "")
        seen[layer] = f"{layer} [{status}]: {detail[:120]}" or layer
    return sorted(seen.values())


async def collect(
    settings: Settings, tokens: Sequence[TokenSnapshot]
) -> tuple[list[Sequence[LayerSignal]], int, float]:
    """One production batch: same pipeline, session, concurrency and buckets as a cycle.

    Returns the *collected* signal groups, the *requested* instrument count, and the
    wall clock. The two counts are kept apart on purpose: ``collect_many`` drops an
    instrument whose collection fails, so a table built only from what came back
    reports coverage of a smaller batch than the one asked for - and a coverage report
    that silently shrinks its denominator is the one that tells an operator everything
    is fine.
    """
    started = time.perf_counter()
    per_token: list[Sequence[LayerSignal]] = []
    async with ScanPipeline(settings) as pipeline:
        collected = await pipeline.collect_many(list(tokens))
    for item in collected:
        if item is not None:
            per_token.append(item.snapshot.signals)
    return per_token, len(tokens), time.perf_counter() - started


def denominator_note(collected: int, requested: int) -> list[str]:
    """Lines stating what the matrix below is a fraction *of*."""
    lines = [f"  instruments collected: {collected} of {requested}"]
    missing = requested - collected
    if missing > 0:
        lines.append(f"  WARNING: {missing} instrument(s) failed collection and are absent from the table")
    if collected == 0:
        lines.append("  the table below describes nothing; treat it as a failed run, not as zero coverage")
    return lines


async def run(*, asset_classes: Sequence[str] = (), limit: int | None = None) -> int:
    settings = Settings.load()
    bundle = WatchlistLoader().load()
    wanted = {AssetClass.coerce(name).value for name in asset_classes} or None
    tokens = [
        token.to_snapshot()
        for token in bundle.tokens()
        if wanted is None or token.asset_class.value in wanted
    ]
    if limit:
        tokens = tokens[:limit]
    if not tokens:
        print("  nothing to collect - check --asset-class against the watchlist")
        return 1

    print(f"  {len(tokens)} instruments | concurrency {settings.scanners.token_concurrency} "
          f"| LAYER_TIMEOUT_SECONDS {settings.scanners.layer_timeout_seconds:g}s")
    per_token, requested, wall = await collect(settings, tokens)

    counts: collections.Counter = collections.Counter()
    reasons: dict[str, str] = {}
    rows = 0
    for signals in per_token:
        counts.update(tally(signals))
        rows += len(signals)
        for line in _reasons(signals):
            reasons.setdefault(line.split(" [", 1)[0], line)

    print(f"  batch wall {wall:.1f}s")
    for line in denominator_note(len(per_token), requested):
        print(line)
    print()
    print(format_table(counts, rows=rows))
    if reasons:
        print("\n  what the auditor was not told, and why:")
        for line in sorted(reasons.values()):
            print(f"    {line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report the layer coverage a cycle would see.")
    parser.add_argument("--asset-class", action="append", default=[], help="filter (repeatable)")
    parser.add_argument("--limit", type=int, default=None, help="cap instruments")
    args = parser.parse_args(argv)
    return asyncio.run(run(asset_classes=args.asset_class, limit=args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
