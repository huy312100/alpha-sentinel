"""AlphaSentinel dashboard - a read-only window onto the bot's stores.

Run with::

    streamlit run dashboard/app.py

The dashboard never writes: the SQLite hot store answers "what is true now" and
the Parquet lake answers "how did we get here". Everything is cached for a short
TTL so a 15s auto-refresh stays cheap and SQLite is not hammered by a browser
left open on a wall display.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import quote

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from bot.watchlist import WatchlistError, load_watchlists
from config.settings import Settings
from core.models import utcnow
from storage.hot_store import HotStore
from storage.parquet_store import ParquetStore, StorageError

st.set_page_config(page_title="AlphaSentinel", page_icon="\U0001F6F0\uFE0F", layout="wide")

STATUS_ORDER = ["approved", "watch", "rejected", "vetoed"]
STATUS_COLORS = {
    "approved": "#16a34a",
    "watch": "#0ea5e9",
    "rejected": "#f59e0b",
    "vetoed": "#dc2626",
}
VERDICT_DATASET = "verdicts"
SIGNALS_DATASET = "layer_signals"
RUNS_DATASET = "runs"
TABLE_COLUMNS = [
    "decided_at",
    "symbol",
    "asset_class",
    "chain",
    "status",
    "final_score",
    "prior_score",
    "alpha_score",
    "risk_score",
    "severity",
    "position_multiplier",
]


# --------------------------------------------------------------------------- #
# Data access (cached; the UI thread must never block on disk)
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner=False)
def get_settings() -> Settings:
    return Settings.load()


@st.cache_resource(show_spinner=False)
def get_store() -> HotStore:
    store = HotStore(get_settings().storage.hot_db_path)
    store.initialize()
    return store


@st.cache_resource(show_spinner=False)
def get_cold() -> ParquetStore:
    settings = get_settings()
    return ParquetStore(
        settings.storage.parquet_root,
        r2=settings.storage.r2,
        compression=settings.storage.compression,
        row_group_size=settings.storage.row_group_size,
        local_retention_days=settings.storage.local_retention_days,
        remote_retention_days=settings.storage.remote_retention_days,
        dataset_retention_days=settings.storage.dataset_retention(),
    )


@st.cache_data(ttl=15, show_spinner=False)
def load_verdicts(
    limit: int,
    status: tuple[str, ...] = (),
    chain: tuple[str, ...] = (),
    since_hours: float | None = None,
    asset_class: tuple[str, ...] = (),
) -> pd.DataFrame:
    rows = get_store().recent_verdicts(
        limit,
        status=status[0] if len(status) == 1 else None,
        chain=chain[0] if len(chain) == 1 else None,
        asset_class=asset_class[0] if len(asset_class) == 1 else None,
        since_hours=since_hours,
        include_payload=False,
    )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    if status:
        frame = frame[frame["status"].isin(status)]
    if chain:
        frame = frame[frame["chain"].isin(chain)]
    if asset_class and "asset_class" in frame.columns:
        frame = frame[frame["asset_class"].isin(asset_class)]
    frame["decided_at"] = pd.to_datetime(frame["decided_at"], errors="coerce", utc=True)
    return frame.sort_values("decided_at", ascending=False).reset_index(drop=True)


@st.cache_data(ttl=15, show_spinner=False)
def load_payload(verdict_id: int) -> dict[str, Any]:
    record = get_store().get_verdict(int(verdict_id))
    return (record or {}).get("payload") or {}


@st.cache_data(ttl=20, show_spinner=False)
def load_runs(limit: int = 25) -> pd.DataFrame:
    frame = pd.DataFrame(get_store().recent_runs(limit=limit))
    if not frame.empty:
        frame["started_at"] = pd.to_datetime(frame["started_at"], errors="coerce", utc=True)
    return frame


@st.cache_data(ttl=20, show_spinner=False)
def load_alerts(limit: int = 50) -> pd.DataFrame:
    rows = get_store().recent_alerts(limit=limit)
    for row in rows:
        row.pop("payload_json", None)
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["created_at"] = pd.to_datetime(frame["created_at"], errors="coerce", utc=True)
    return frame


@st.cache_data(ttl=60, show_spinner=False)
def load_cold(dataset: str, days: int) -> pd.DataFrame:
    try:
        frame = get_cold().read_frame(dataset, start=utcnow() - timedelta(days=days))
    except (StorageError, OSError, ValueError):
        return pd.DataFrame()
    if frame.empty:
        return frame
    for column in ("decided_at", "observed_at", "finished_at"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce", utc=True)
    return frame


@st.cache_data(ttl=60, show_spinner=False)
def load_watchlists_cached() -> list[dict[str, Any]]:
    settings = get_settings()
    try:
        bundle = load_watchlists(settings.scanners.watchlist_path, default_chain=settings.runtime.default_chain)
    except WatchlistError as exc:
        return [{"error": str(exc)}]
    rows: list[dict[str, Any]] = []
    for watchlist in bundle.watchlists:
        for token in watchlist.tokens:
            rows.append(
                {
                    "watchlist": watchlist.name,
                    "asset_class": token.asset_class.value,
                    "symbol": token.symbol,
                    "chain": token.chain,
                    "provider_symbol": token.provider_symbol,
                    "token_address": token.token_address,
                    "priority": token.priority,
                    "cooldown_hours": token.cooldown_hours,
                    "tags": ", ".join(token.tags),
                }
            )
    return rows


# --------------------------------------------------------------------------- #
# Charts (Plotly, dark-friendly, no default template noise)
# --------------------------------------------------------------------------- #
def _layout(fig: go.Figure, *, height: int = 300, title: str = "") -> go.Figure:
    fig.update_layout(
        height=height,
        title=title or None,
        margin=dict(l=10, r=10, t=40 if title else 10, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    return fig


def status_chart(frame: pd.DataFrame) -> go.Figure:
    counts = frame["status"].value_counts().reindex(STATUS_ORDER, fill_value=0).reset_index()
    counts.columns = ["status", "count"]
    fig = px.bar(counts, x="status", y="count", color="status", color_discrete_map=STATUS_COLORS, text="count")
    fig.update_traces(textposition="outside")
    return _layout(fig, title="Verdict mix")


def score_timeline(frame: pd.DataFrame) -> go.Figure:
    fig = px.scatter(
        frame,
        x="decided_at",
        y="final_score",
        color="status",
        color_discrete_map=STATUS_COLORS,
        hover_data=["symbol", "asset_class", "chain", "risk_score", "position_multiplier"],
        size="position_multiplier",
        size_max=14,
    )
    fig.add_hline(y=65, line_dash="dot", line_color="#64748b", annotation_text="min score")
    return _layout(fig, height=340, title="Score timeline")


def layer_chart(signals: pd.DataFrame) -> go.Figure:
    grouped = (
        signals.groupby("layer", as_index=False)["score"]
        .mean()
        .sort_values("score", ascending=True)
    )
    fig = px.bar(grouped, x="score", y="layer", orientation="h", range_x=[0, 100])
    return _layout(fig, height=320, title="Mean layer score (cold store)")


def layer_heatmap(signals: pd.DataFrame) -> go.Figure:
    pivot = signals.pivot_table(index="symbol", columns="layer", values="score", aggfunc="mean")
    fig = px.imshow(pivot, aspect="auto", color_continuous_scale="Viridis", zmin=0, zmax=100, text_auto=".0f")
    return _layout(fig, height=max(280, 28 * len(pivot.index) + 120), title="Token x layer score")


def run_chart(runs: pd.DataFrame) -> go.Figure:
    fig = px.bar(
        runs.sort_values("started_at"),
        x="started_at",
        y="duration_ms",
        color="status",
        hover_data=["tokens_scanned", "verdicts_written", "errors"],
    )
    return _layout(fig, height=300, title="Cycle duration (ms)")


def empty_state(message: str, hint: str = "") -> None:
    st.info(message + (f"\n\n`{hint}`" if hint else ""))


# --------------------------------------------------------------------------- #
# Sidebar + filters
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=60, show_spinner=False)
def available_chains() -> list[str]:
    frame = load_verdicts(1000)
    return sorted(frame["chain"].dropna().unique().tolist()) if not frame.empty else []


@st.cache_data(ttl=60, show_spinner=False)
def available_asset_classes() -> list[str]:
    frame = load_verdicts(1000)
    if frame.empty or "asset_class" not in frame.columns:
        return []
    return sorted(frame["asset_class"].dropna().unique().tolist())


def sidebar() -> dict[str, Any]:
    settings = get_settings()
    with st.sidebar:
        st.markdown("### \U0001F6F0\uFE0F AlphaSentinel")
        if st.button("Refresh now", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        st.caption(f"demo mode: `{settings.runtime.demo_mode}` · db `{settings.storage.hot_db_path.name}`")
        st.divider()
        lookback = st.slider("Lookback (hours)", 1, 720, 72, step=1)
        limit = st.slider("Max rows", 50, 2000, 250, step=50)
        statuses = st.multiselect("Status", STATUS_ORDER, default=STATUS_ORDER)
        asset_classes = st.multiselect("Market", available_asset_classes(), default=[])
        chains = st.multiselect("Venue", available_chains(), default=[])
        cold_days = st.slider("Cold store window (days)", 1, 90, 14)
        st.divider()
        st.caption("Auto-refresh every 15s is safe: reads are cached and the DB is opened read-mostly.")
    return {
        "lookback": float(lookback),
        "limit": int(limit),
        "statuses": tuple(statuses),
        "asset_classes": tuple(asset_classes),
        "chains": tuple(chains),
        "cold_days": int(cold_days),
    }


# --------------------------------------------------------------------------- #
# Tab renderers
# --------------------------------------------------------------------------- #
def render_overview(frame: pd.DataFrame, runs: pd.DataFrame, alerts: pd.DataFrame) -> None:
    if frame.empty:
        empty_state(
            "No verdicts in this window yet. Run a cycle to populate the hot store.",
            "python -m bot --once --dry-run   # or: make bot",
        )
        return

    approved = int((frame["status"] == "approved").sum())
    watch = int((frame["status"] == "watch").sum())
    vetoed = int((frame["status"] == "vetoed").sum())
    total = len(frame)
    approval_rate = approved / total * 100 if total else 0.0

    first = st.columns(6)
    first[0].metric("Verdicts", total)
    first[1].metric("Approved", approved)
    first[2].metric("Watch", watch)
    first[3].metric("Vetoed", vetoed)
    first[4].metric("Approval rate", f"{approval_rate:.0f}%")
    first[5].metric("Alerts", 0 if alerts.empty else len(alerts))

    second = st.columns(6)
    second[0].metric("Avg final score", f"{frame['final_score'].mean():.1f}")
    second[1].metric("Avg risk score", f"{frame['risk_score'].mean():.1f}")
    second[2].metric("Avg prior", f"{frame['prior_score'].mean():.1f}")
    second[3].metric("Max size", f"{frame['position_multiplier'].max():.2f}x")
    second[4].metric("Avg latency", f"{frame['latency_ms'].mean():.0f} ms")
    second[5].metric("Venues", frame["chain"].nunique())

    left, right = st.columns([1, 2])
    left.plotly_chart(status_chart(frame), use_container_width=True)
    right.plotly_chart(score_timeline(frame), use_container_width=True)

    st.subheader("Actionable now")
    actionable = frame[frame["status"].isin(["approved", "watch"])].nlargest(10, "final_score")
    if actionable.empty:
        st.caption("Nothing approved or on watch in the selected window.")
    else:
        st.dataframe(
            actionable[TABLE_COLUMNS],
            use_container_width=True,
            hide_index=True,
            column_config={
                "final_score": st.column_config.ProgressColumn(
                    "final", min_value=0, max_value=100, format="%.1f"
                ),
                "risk_score": st.column_config.ProgressColumn(
                    "risk", min_value=0, max_value=100, format="%.1f", color="#dc2626"
                ),
            },
        )

    if not runs.empty:
        st.subheader("Latest cycle")
        latest = runs.iloc[0]
        cols = st.columns(5)
        cols[0].metric("Mode", str(latest.get("mode", "?")))
        cols[1].metric("Status", str(latest.get("status", "?")))
        cols[2].metric("Tokens", int(latest.get("tokens_scanned") or 0))
        cols[3].metric("Duration", f"{float(latest.get('duration_ms') or 0):.0f} ms")
        cols[4].metric("Errors", int(latest.get("errors") or 0))


def render_verdicts(frame: pd.DataFrame) -> None:
    if frame.empty:
        empty_state("No verdicts match the current filters.")
        return
    st.dataframe(
        frame[TABLE_COLUMNS],
        use_container_width=True,
        hide_index=True,
        column_config={
            "decided_at": st.column_config.DatetimeColumn("decided", format="YYYY-MM-DD HH:mm"),
            "final_score": st.column_config.NumberColumn("final", format="%.1f"),
            "prior_score": st.column_config.NumberColumn("prior", format="%.1f"),
            "alpha_score": st.column_config.NumberColumn("alpha", format="%.1f"),
            "risk_score": st.column_config.NumberColumn("risk", format="%.1f"),
            "position_multiplier": st.column_config.NumberColumn("size", format="%.2fx"),
        },
    )

    labels = {
        int(row["id"]): (
            f"{row.get('symbol') or str(row.get('token_address'))[:8]} · {row.get('chain')} · "
            f"{row.get('asset_class') or 'crypto'} · "
            f"{str(row.get('decided_at'))[:16]} · {row.get('status')} {float(row.get('final_score') or 0):.1f}"
        )
        for row in frame.to_dict("records")
    }
    if not labels:
        return
    chosen = st.selectbox("Inspect verdict", options=list(labels), format_func=labels.get)
    if chosen is not None:
        render_detail(int(chosen))


def _link(record: dict[str, Any]) -> str:
    """Chart link that works for every market, not just DEX pairs."""
    asset_class = str(record.get("asset_class") or "crypto")
    address = str(record.get("token_address") or "")
    if asset_class == "crypto":
        return f"https://dexscreener.com/{record.get('chain')}/{address}"
    return f"https://www.tradingview.com/symbols/?search={quote(address)}"


def render_detail(verdict_id: int) -> None:
    """Full rationale for one verdict - the reason a human opens this app."""
    record = get_store().get_verdict(verdict_id)
    if not record:
        st.warning("That verdict is no longer in the hot store (it may have been pruned).")
        return
    payload = load_payload(verdict_id)
    alpha = payload.get("alpha") or {}
    risk = payload.get("risk") or {}
    symbol = record.get("symbol") or str(record.get("token_address"))[:10]

    st.markdown(f"#### {symbol} · {record.get('chain')} · {record.get('asset_class') or 'crypto'}")
    st.caption(
        f"`{record.get('token_address')}` · decided {record.get('decided_at')} · "
        f"alpha `{alpha.get('model', 'n/a')}` · risk `{risk.get('model', 'n/a')}` · "
        f"[open chart]({_link(record)})"
    )

    metrics = st.columns(5)
    metrics[0].metric("Final", f"{float(record.get('final_score') or 0):.1f}")
    metrics[1].metric("Prior", f"{float(record.get('prior_score') or 0):.1f}")
    metrics[2].metric("Alpha", f"{float(record.get('alpha_score') or 0):.1f}")
    metrics[3].metric("Risk", f"{float(record.get('risk_score') or 0):.1f}")
    metrics[4].metric("Size", f"{float(record.get('position_multiplier') or 0):.2f}x")
    st.caption(
        f"status **{record.get('status')}** · severity **{record.get('severity')}** · "
        f"risk veto `{record.get('risk_veto')}` · latency {float(record.get('latency_ms') or 0):.0f} ms"
    )

    alpha_tab, risk_tab, history_tab, raw_tab = st.tabs(["Alpha thesis", "Risk audit", "History", "Raw"])
    with alpha_tab:
        st.write(alpha.get("thesis") or record.get("thesis") or "_no thesis recorded_")
        if alpha.get("catalysts"):
            st.markdown("**Catalysts**")
            # LLM output is untrusted; plain text keeps injected links inert.
            st.text("\n".join(f"- {item}" for item in alpha["catalysts"]))
        highlights = alpha.get("layer_highlights") or {}
        if highlights:
            st.markdown("**Layer highlights**")
            st.dataframe(
                pd.DataFrame(sorted(highlights.items()), columns=["layer", "note"]),
                use_container_width=True,
                hide_index=True,
            )
        if alpha.get("red_flags"):
            st.markdown("**Red flags**")
            st.text("\n".join(f"- {item}" for item in alpha["red_flags"]))
        if payload.get("rationale"):
            st.markdown("**Reconciled rationale**")
            st.write(payload["rationale"])

    with risk_tab:
        st.write(risk.get("reasoning_summary") or record.get("risk_summary") or "_no summary recorded_")
        findings = risk.get("findings") or []
        if findings:
            st.markdown("**Findings**")
            st.dataframe(pd.DataFrame(findings), use_container_width=True, hide_index=True)
        if risk.get("invalidation_triggers"):
            st.markdown("**Invalidation triggers**")
            st.text("\n".join(f"- {item}" for item in risk["invalidation_triggers"]))
        security = [flag for flag in str(record.get("security_flags") or "").split(",") if flag]
        if security:
            st.markdown("**Security flags**: " + ", ".join(f"`{flag}`" for flag in security))

    with history_tab:
        _render_history(record)

    with raw_tab:
        st.json(payload or record)


def _render_history(record: dict[str, Any]) -> None:
    history = pd.DataFrame(
        get_store().verdict_history(str(record.get("token_address")), chain=str(record.get("chain")), limit=100)
    )
    if history.empty:
        st.caption("No history for this token.")
        return
    history["decided_at"] = pd.to_datetime(history["decided_at"], errors="coerce", utc=True)
    plot = history.sort_values("decided_at")
    fig = go.Figure()
    fig.add_scatter(x=plot["decided_at"], y=plot["final_score"], name="final", mode="lines+markers")
    fig.add_scatter(x=plot["decided_at"], y=plot["risk_score"], name="risk", mode="lines+markers")
    fig.add_scatter(x=plot["decided_at"], y=plot["prior_score"], name="prior", mode="lines")
    st.plotly_chart(_layout(fig, height=320, title="Score evolution"), use_container_width=True)
    st.dataframe(
        history[["decided_at", "status", "final_score", "risk_score", "position_multiplier"]],
        use_container_width=True,
        hide_index=True,
    )


def render_signals(cold_days: int) -> None:
    """Layer-level truth from Parquet: where the score actually came from."""
    signals = load_cold(SIGNALS_DATASET, cold_days)
    if signals.empty:
        empty_state(
            "No layer signals in the cold store for this window. The bot writes them on every persisted cycle.",
            "python -m bot --once",
        )
        return
    signals = signals.copy()
    signals["symbol"] = signals["symbol"].fillna("").replace("", "unknown")

    left, right = st.columns([1, 2])
    left.plotly_chart(layer_chart(signals), use_container_width=True)
    grouped = signals.groupby(["layer", "status"]).size().reset_index(name="count")
    fig = px.bar(grouped, x="layer", y="count", color="status", barmode="stack")
    right.plotly_chart(_layout(fig, height=320, title="Layer health (signal status)"), use_container_width=True)

    st.plotly_chart(layer_heatmap(signals), use_container_width=True)

    symbols = sorted(signals["symbol"].unique().tolist())
    layers = sorted(signals["layer"].unique().tolist())
    picked_symbols = st.multiselect("Symbols", symbols, default=symbols[:8])
    picked_layers = st.multiselect("Layers", layers, default=layers)
    view = signals[signals["symbol"].isin(picked_symbols) & signals["layer"].isin(picked_layers)]
    signal_columns = [
        "observed_at",
        "symbol",
        "asset_class",
        "chain",
        "layer",
        "status",
        "score",
        "confidence",
        "weight",
        "summary",
    ]
    st.dataframe(
        view.sort_values("observed_at", ascending=False)[
            [column for column in signal_columns if column in view.columns]
        ],
        use_container_width=True,
        hide_index=True,
    )


def render_runs(runs: pd.DataFrame, alerts: pd.DataFrame) -> None:
    if runs.empty:
        empty_state("No runs recorded yet.", "python -m bot --once")
    else:
        st.plotly_chart(run_chart(runs), use_container_width=True)
        st.dataframe(
            runs[
                [
                    "started_at",
                    "mode",
                    "status",
                    "tokens_scanned",
                    "verdicts_written",
                    "approved",
                    "vetoed",
                    "errors",
                    "duration_ms",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )
        failed = runs[runs["status"] == "failed"]
        if not failed.empty:
            st.warning(f"{len(failed)} failed run(s) - check logs for `cycle_crashed` / `audit_failed`.")

    st.subheader("Alerts sent")
    if alerts.empty:
        st.caption("No alerts recorded. Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable delivery.")
        return
    st.dataframe(alerts, use_container_width=True, hide_index=True)


def render_watchlists() -> None:
    rows = load_watchlists_cached()
    if rows and "error" in rows[0]:
        st.error(rows[0]["error"])
        st.caption("Watchlists fail closed: the bot will not run until the file parses.")
        return
    frame = pd.DataFrame(rows)
    cols = st.columns(3)
    cols[0].metric("Watchlists", frame["watchlist"].nunique())
    cols[1].metric("Instruments", len(frame))
    cols[2].metric("Markets", frame["asset_class"].nunique() if "asset_class" in frame.columns else 1)
    st.dataframe(
        frame.sort_values(["watchlist", "priority", "symbol"]),
        use_container_width=True,
        hide_index=True,
        column_config={
            "token_address": st.column_config.TextColumn("identity", width="medium"),
            "provider_symbol": st.column_config.TextColumn("provider", width="small"),
            "priority": st.column_config.NumberColumn("priority", help="lower scans first"),
            "cooldown_hours": st.column_config.NumberColumn("cooldown (h)", format="%.1f"),
        },
    )
    st.caption(
        f"Source: `{get_settings().scanners.watchlist_path}` - edited files are picked up automatically "
        "on the next cycle (mtime check, no restart)."
    )


def render_system() -> None:
    settings = get_settings()
    store = get_store()
    cold = get_cold()
    st.markdown("**Hot store (SQLite)**")
    st.json(store.stats())
    st.markdown("**Cold store (Parquet)**")
    st.json(cold.health())
    stats = pd.DataFrame([stat.to_dict() for stat in cold.dataset_stats()])
    if not stats.empty:
        st.dataframe(stats, use_container_width=True, hide_index=True)
    with st.expander("Runtime configuration (redacted)"):
        st.json(settings.redacted_summary())
    with st.expander("Operator cheat sheet"):
        st.markdown(
            "\n".join(
                [
                    "- `python -m bot --once --dry-run` - collect + audit, persist nothing.",
                    "- `python -m bot --token WETH --once` - single-instrument cycle.",
                    "- `python -m scanners --symbol AAPL --asset-class equity` - probe any market.",
                    "- `python -m bot --status` - hot-store state without the UI.",
                    "- `python -m bot --maintenance` - prune hot rows, apply retention.",
                    "- `DEMO_MODE=true` - offline deterministic agents, no API key needed.",
                ]
            )
        )


def main() -> None:
    opts = sidebar()
    st.title("\U0001F6F0\uFE0F AlphaSentinel")
    st.caption(
        "Deterministic layers set the prior, two LLM agents audit it, and a hard risk veto overrides "
        "everything. This UI is read-only."
    )

    verdicts = load_verdicts(
        opts["limit"], opts["statuses"], opts["chains"], opts["lookback"], opts["asset_classes"]
    )
    runs = load_runs()
    alerts = load_alerts()

    overview, verdicts_tab, signals_tab, runs_tab, watch_tab, system_tab = st.tabs(
        ["Overview", "Verdicts", "Layer signals", "Runs & alerts", "Watchlists", "System"]
    )
    with overview:
        render_overview(verdicts, runs, alerts)
    with verdicts_tab:
        render_verdicts(verdicts)
    with signals_tab:
        render_signals(opts["cold_days"])
    with runs_tab:
        render_runs(runs, alerts)
    with watch_tab:
        render_watchlists()
    with system_tab:
        render_system()


main()
