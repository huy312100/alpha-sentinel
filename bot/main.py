"""Command-line entry point for the AlphaSentinel bot.

Usage::

    python -m bot --once --dry-run          # one cycle, nothing persisted
    python -m bot                            # daemon loop (SCAN_INTERVAL_SECONDS)
    python -m bot --status                   # what is in the hot store right now
    python -m bot --health                   # is the loop still turning? (docker healthcheck)
    python -m bot --token WETH --chain ethereum --once
    python -m bot --token AAPL --once        # any watchlist ticker works too
    python -m bot --maintenance              # retention + stale-run cleanup

Exit codes are meaningful for cron/CI: ``0`` success, ``1`` the cycle hit errors
(or, for ``--health``, the bot is stalled), ``2`` bad usage (unknown watchlist,
unknown instrument) or an environment that cannot be trusted, so the process
refuses to boot instead of scanning half blind.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from dataclasses import replace
from typing import Sequence

from bot.alerts import TelegramAlerter
from bot.engine import ScanEngine, ScanReport, day_spend_key
from bot.health import evaluate
from bot.watchlist import WatchToken, WatchlistBundle, WatchlistError, WatchlistLoader
from config.errors import ConfigError
from config.settings import Settings
from core.logging import configure_logging, get_logger
from storage.hot_store import HotStore

log = get_logger(__name__)

BOT_VERSION = "1.0.0"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bot",
        description="AlphaSentinel scanner bot: collect, audit, persist, alert.",
    )
    parser.add_argument("--version", action="version", version=f"AlphaSentinel {BOT_VERSION}")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run a single cycle and exit")
    mode.add_argument("--daemon", action="store_true", help="loop forever (default)")
    mode.add_argument("--status", action="store_true", help="print hot-store status and exit")
    mode.add_argument(
        "--health",
        action="store_true",
        help="print a cycle-freshness verdict and exit non-zero when the loop stopped",
    )
    mode.add_argument("--maintenance", action="store_true", help="run retention and exit")

    parser.add_argument(
        "--token",
        action="append",
        metavar="ADDRESS|SYMBOL",
        help="scan only this instrument (repeatable): contract address, ticker or watchlist symbol",
    )
    parser.add_argument(
        "--chain",
        default=None,
        help="restrict instrument lookups to one chain or venue",
    )
    parser.add_argument(
        "--watchlist",
        action="append",
        metavar="NAME",
        help="limit the cycle to these watchlists (repeatable)",
    )
    parser.add_argument("--limit", type=int, default=None, help="override max instruments per cycle")
    parser.add_argument("--force", action="store_true", help="ignore the per-instrument cooldown")
    parser.add_argument("--dry-run", action="store_true", help="collect + audit, persist nothing")
    parser.add_argument("--no-alerts", action="store_true", help="suppress Telegram sends")
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="stop the daemon after N cycles (useful for smoke tests)",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="console log level (default INFO)",
    )
    parser.add_argument("--json-logs", action="store_true", help="emit structured JSON logs")
    return parser


def resolve_tokens(
    bundle: WatchlistBundle, values: Sequence[str], *, chain: str | None = None
) -> list[WatchToken]:
    """Resolve ``--token`` arguments against configured addresses and symbols."""
    pool = bundle.tokens()
    if chain:
        pool = [token for token in pool if token.chain == chain.strip().lower()]
    resolved: list[WatchToken] = []
    missing: list[str] = []
    for raw in values:
        value = (raw or "").strip()
        if not value:
            continue
        lowered = value.lower()
        match = next((token for token in pool if token.token_address.lower() == lowered), None)
        if match is None:
            match = next((token for token in pool if token.symbol.upper() == value.upper()), None)
        if match is None:
            missing.append(value)
        elif match not in resolved:
            resolved.append(match)
    if missing:
        known = ", ".join(f"{token.label}@{token.chain}" for token in pool[:25]) or "none"
        raise WatchlistError(f"not in the watchlists: {', '.join(missing)} (known: {known})")
    return resolved


def format_report(report: ScanReport) -> str:
    """Human-readable cycle summary for cron logs and terminals."""
    mode = " (dry-run)" if report.dry_run else ""
    lines = [
        f"run {report.run_id}{mode} finished in {report.duration_ms:.0f} ms",
        (
            f"  requested={report.requested} collected={report.collected} "
            f"skipped={report.skipped} errors={report.errors}"
        ),
        (
            f"  approved={report.approved} watch={report.watch} "
            f"rejected={report.rejected} vetoed={report.vetoed} alerts={report.alerts_sent}"
        ),
    ]
    for result in report.results:
        if result.skipped:
            lines.append(f"  - {result.token.label:<10} {result.token.chain:<10} skipped: {result.skip_reason}")
            continue
        if result.error:
            lines.append(f"  ! {result.token.label:<10} {result.token.chain:<10} error: {result.error}")
            continue
        verdict = result.verdict
        if verdict is None:
            continue
        alert = " alert=sent" if result.alert is not None and result.alert.sent else ""
        lines.append(
            f"  {verdict.status.value:<8} {result.token.label:<10} {result.token.asset_class.value:<9} "
            f"{result.token.chain:<10} "
            f"score={verdict.final_score:5.1f} risk={verdict.risk.risk_score:5.1f} "
            f"size={verdict.position_multiplier:.2f}x{alert}"
        )
    return "\n".join(lines)


def format_status(settings: Settings, bundle: WatchlistBundle, store: HotStore) -> str:
    """Snapshot of what the bot would do next, straight from the hot store."""
    lines = [
        f"AlphaSentinel {BOT_VERSION}",
        f"  watchlists: {', '.join(bundle.names())} ({len(bundle)} instruments, cap {bundle.max_tokens_per_cycle})",
        f"  markets: {', '.join(sorted({token.asset_class.value for token in bundle.tokens()}))}",
        f"  source: {bundle.source}",
        f"  hot store: {settings.storage.hot_db_path}",
        f"  parquet lake: {settings.storage.parquet_root}",
        f"  demo mode: {settings.runtime.demo_mode}",
        f"  llm: {settings.llm.provider} {settings.llm.base_url} "
        f"({settings.llm.alpha_model} / {settings.llm.risk_model}), "
        f"key {'set' if settings.llm.api_key.is_configured() else 'missing'}",
        f"  alerts configured: {settings.telegram.is_configured()}",
        f"  fundamentals: {'SEC EDGAR enabled' if settings.scanners.sec_user_agent else 'off (SEC_USER_AGENT unset)'}",
        "  llm spend: today {} (day cap {}), cycle cap {}".format(
            _usd(_today_spend(store)),
            _usd(settings.llm.max_usd_per_day),
            _usd(settings.llm.max_usd_per_cycle),
        ),
    ]
    breakdown = store.status_breakdown(since_hours=24.0)
    pretty = ", ".join(f"{status}={count}" for status, count in sorted(breakdown.items())) or "none"
    lines.append(f"  verdicts (24h): {pretty}")
    latest = store.latest_run()
    if latest:
        lines.append(
            "  last run: {run} {status} at {at} ({verdicts} verdicts, {errors} errors)".format(
                run=str(latest.get("id", ""))[:8],
                status=latest.get("status", "unknown"),
                at=latest.get("finished_at") or latest.get("started_at") or "?",
                verdicts=latest.get("verdicts_written", 0),
                errors=latest.get("errors", 0),
            )
        )
    else:
        lines.append("  last run: none yet")
    for row in store.top_scores(limit=5, since_hours=24.0):
        lines.append(
            "  top: {symbol} {score:.1f} {status} ({chain}, {asset_class})".format(
                symbol=row.get("symbol") or str(row.get("token_address", ""))[:10],
                score=float(row.get("final_score") or 0.0),
                status=row.get("status", "?"),
                chain=row.get("chain", "?"),
                asset_class=row.get("asset_class", "crypto"),
            )
        )
    return "\n".join(lines)


def _usd(value: float) -> str:
    """Render a spend figure; a zero ceiling means "no ceiling", not "$0"."""
    return "none" if value <= 0 else f"${value:.4f}"


def _today_spend(store: HotStore) -> float:
    """Today's ledgered LLM spend; a read failure must not hide the rest of status."""
    try:
        return max(float(store.get_state(day_spend_key(), 0.0) or 0.0), 0.0)
    except (TypeError, ValueError, OSError, RuntimeError):
        return 0.0


def _install_signal_handlers(engine: ScanEngine) -> None:
    """Stop on Ctrl-C / SIGTERM *after* the cycle in flight completes."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.stop)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows / non-main thread
            try:
                signal.signal(sig, lambda *_: engine.stop())
            except (ValueError, OSError):
                log.warning("signal_handler_unavailable", extra={"signal": sig})


def _report_config_error(exc: ConfigError) -> int:
    """Print an operator-facing diagnostic and return the usage exit code."""
    print(str(exc), file=sys.stderr)
    return EXIT_USAGE


def _refuse_invalid_config(settings: Settings, *, status_only: bool) -> int | None:
    """Return the exit code that must stop this process, or ``None`` to boot.

    A misconfigured deploy must die before it writes a verdict, not on its first
    audit: an unset ``LLM_API_KEY``, production-with-``DEMO_MODE``, a broken
    endpoint catalogue or an unknown layer name are operator errors. ``--status``
    and ``--health`` are exempt because the container healthcheck runs them: a
    check that refuses to answer when config is wrong cannot report on the loop,
    and an invalid environment already kills the daemon at boot with exit ``2``.
    """
    if status_only:
        return None
    try:
        settings.validate_or_raise()
    except ConfigError as exc:
        return _report_config_error(exc)
    return None


async def main(argv: list[str] | None = None) -> int:
    """Load the environment, refuse a bad one, then run, report or maintain."""
    args = build_parser().parse_args(argv)
    configure_logging(level=args.log_level, json_output=args.json_logs)
    try:
        settings = Settings.load()
    except ConfigError as exc:
        # Settings.load() parses strictly, so it can fail before validate() ever
        # runs: an unknown LLM_PROVIDER, a malformed integer or an unreadable
        # ENDPOINTS_FILE used to escape as a traceback with EXIT_ERROR.
        # --status gets no exemption here: without settings there is no store
        # path to read, and inventing defaults would report - and create - the
        # wrong database. A live container always has a loadable environment,
        # because the daemon refuses to boot otherwise.
        return _report_config_error(exc)

    refusal = _refuse_invalid_config(settings, status_only=args.status or args.health)
    if refusal is not None:
        return refusal

    return await _run(args, settings)


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    """Execute the requested mode against a validated environment and one store."""
    if args.health:
        # Deliberately before the watchlist load: an unreadable watchlist is a
        # content problem the daemon reports on its next cycle, not evidence
        # that the loop stopped.
        return _run_health(settings, as_json=args.json)

    loader = WatchlistLoader(
        settings.scanners.watchlist_path, default_chain=settings.runtime.default_chain
    )
    try:
        bundle = loader.load(force=True)
    except WatchlistError as exc:
        print(f"watchlist error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    store = HotStore(settings.storage.hot_db_path)
    try:
        store.initialize()
        if args.status:
            print(format_status(settings, bundle, store))
            return EXIT_OK

        tokens: list[WatchToken] | None = None
        if args.token:
            try:
                tokens = resolve_tokens(bundle, args.token, chain=args.chain)
            except WatchlistError as exc:
                print(f"token error: {exc}", file=sys.stderr)
                return EXIT_USAGE

        telegram = replace(settings.telegram, chat_id="") if args.no_alerts else settings.telegram
        alerter = TelegramAlerter(telegram, endpoints=settings.endpoints, store=store)

        async with ScanEngine(settings, store=store, alerter=alerter, loader=loader) as engine:
            _install_signal_handlers(engine)
            if args.maintenance:
                result = await asyncio.to_thread(engine.maintenance, force=True)
                print(json.dumps(result, default=str))
                return EXIT_OK

            single_cycle = bool(args.once or args.dry_run or args.max_cycles == 1)
            if single_cycle:
                return await _run_single_cycle(args, engine, tokens)

            cycles = await engine.run_forever(
                watchlists=args.watchlist,
                limit=args.limit,
                force=args.force,
                max_cycles=args.max_cycles,
            )
            log.info("bot_stopped", extra={"cycles": cycles, "stopped": engine.stopped})
            return EXIT_OK
    finally:
        store.close()


def _run_health(settings: Settings, *, as_json: bool = False) -> int:
    """Print the freshness verdict. A non-zero exit is what restarts the container."""
    store = HotStore(settings.storage.hot_db_path)
    try:
        store.initialize()
        report = evaluate(store, settings)
    finally:
        store.close()
    print(json.dumps(report.to_dict(), indent=2, default=str) if as_json else report.format())
    return EXIT_OK if report.healthy else EXIT_ERROR


async def _run_single_cycle(
    args: argparse.Namespace, engine: ScanEngine, tokens: list[WatchToken] | None
) -> int:
    """Print one cycle's report and translate it into the exit code cron sees."""
    report = await engine.run_cycle(
        watchlists=args.watchlist,
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        tokens=tokens,
    )
    payload = json.dumps(report.to_dict(), indent=2, default=str) if args.json else format_report(report)
    print(payload)
    # A cycle with any error is not a success: cron and CI must see it.
    if report.errors or not (report.verdicts or report.skipped):
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130) from None
