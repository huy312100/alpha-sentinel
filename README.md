# AlphaSentinel

A two-agent crypto token screener. It collects on-chain, derivatives, social, DEX,
airdrop, macro and technical signals for a watchlist, then asks two DeepSeek agents
to judge it — **Agent 1 argues the bull case, Agent 2 tries to kill the trade** — and
funnels both through a deterministic scorer that has final say. Verdicts are written
to SQLite (hot) and Parquet (cold), alerts go to Telegram, and a Streamlit dashboard
reads both stores.

The design constraint is simple: **the LLM is a judge of narrative, never the sole
source of truth.** Every number that decides a trade is computed in `core/scoring.py`
with pure arithmetic, so a verdict is reproducible and testable without an API key.

---

## Architecture

```
config/watchlists.yaml
        │
        ▼
┌───────────────────┐   7 layers, each isolated + timeout-bounded
│ scanners.pipeline │──▶ onchain · derivatives · social · dex
│                   │    airdrop · macro · technicals
└─────────┬─────────┘
          │ TokenSnapshot + confidence-weighted prior
          ▼
┌───────────────────────────┐
│ ai_engine.dual_audit_flow │
│  Agent 1  alpha (V3)      │  bull case, catalysts, red flags
│  Agent 2  risk  (R1)      │  VETO power, CRITICAL severity
└─────────┬─────────────────┘
          │ AlphaAssessment + RiskAudit
          ▼
┌───────────────────┐   veto precedence (absolute):
│ core.scoring      │   1. hard GoPlus flag (honeypot, mintable, >10% tax)
│  reconcile()      │   2. Agent 2 veto / CRITICAL
└─────────┬─────────┘   3. risk_score >= MAX_RISK_SCORE
          │             4. wash-trading suspicion >= 85
          ▼
   FinalVerdict  ──▶  HotStore (SQLite, "what is true now")
                 ──▶  ParquetStore (lake, "how did we get here")
                 ──▶  TelegramAlerter (deduped, APPROVED >= ALERT_MIN_SCORE)
                 ──▶  dashboard/app.py (read-only, 15s auto-refresh)
```

Failure isolation is a hard rule: one dead provider degrades a layer, not the cycle;
one dead token does not abort the run; a Parquet or Telegram hiccup is logged and the
loop keeps going. `--dry-run` collects and audits but persists and sends nothing.

---

## Quickstart (no API key required)

```bash
make install                 # venv + pinned deps
cp .env.example .env         # DEMO_MODE=true works as-is
make demo                    # scripted agents: APPROVED verdict, formatted
make honeypot                # scripted agents: VETO with hard security flags
make test                    # hermetic suite, no network
```

Then, with real keys in `.env` (`DEEPSEEK_API_KEY`, optionally `GOPLUS_APP_KEY`,
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`):

```bash
make status                  # what the hot store knows right now
make dry-run                 # one cycle, nothing persisted
make once                    # one real cycle
make bot                     # the daemon loop (SCAN_INTERVAL_SECONDS)
make dashboard               # Streamlit on :8501
```

---

## CLI

### `python -m bot.main`

| Flag | Effect |
| --- | --- |
| `--status` | print settings, watchlist and hot-store counts, then exit |
| `--once` | single cycle, then exit |
| `--daemon` | loop forever (default) |
| `--maintenance` | run retention/pruning once and exit |
| `--dry-run` | collect + audit, persist nothing, send nothing |
| `--no-alerts` | run normally but suppress Telegram |
| `--watchlist NAME` | restrict to one or more named lists (repeatable) |
| `--token ADDR` | scan explicit addresses instead of the watchlist (repeatable) |
| `--chain SLUG` | chain hint for `--token` |
| `--limit N` | override `max_tokens_per_cycle` |
| `--force` | ignore the per-token cooldown |
| `--max-cycles N` | stop after N cycles (daemon mode) |
| `--json` | machine-readable report |
| `--json-logs` | structured logs |

Exit codes: `0` ok, `1` cycle produced nothing, `2` usage/config error.

### Other entrypoints

```bash
python -m scanners --token 0x... --chain ethereum [--symbol WETH] [--json]
python -m ai_engine.dual_audit_flow --demo [--honeypot]
python -m ai_engine.dual_audit_flow --address 0x... --chain ethereum   # live, needs key
streamlit run dashboard/app.py
```

---

## How a verdict is computed

```
prior   = Σ(score · confidence · weight) / Σ(confidence · weight)   # usable layers only
blended = (1 − 0.6) · prior + 0.6 · alpha_score                      # ALPHA_WEIGHT
final   = blended · (1 − 0.5 · risk_score / 100)                     # risk_drag
```

`UNAVAILABLE` and `SKIPPED` signals carry **zero weight** rather than a neutral 50 —
silence must not look bearish. Thresholds live in `config/settings.py`
(`ThresholdSettings`):

| Boundary | Default | Meaning |
| --- | --- | --- |
| `min_final_score` | 65 | at or above → `APPROVED` |
| `watch_score` | 50 | at or above → `WATCH` |
| `veto_score_ceiling` | 35 | below → `REJECTED` |
| `max_risk_score` | 70 | at or above → `VETOED` |
| `position_tiers` | 85→1.0x, 75→0.75x, 65→0.5x | size multiplier |

Status order: `APPROVED` / `WATCH` / `REJECTED` / `VETOED`. A veto short-circuits
everything — no alpha score can outvote a honeypot flag or a CRITICAL finding.

---

## Configuration

Everything is environment-driven and read once at process start (`config/settings.py`,
`Settings.load()`). `.env` is loaded automatically for local runs; in Docker, values
come from the compose `env_file`. `.env.example` is the authoritative list; these are
the groups that change behaviour:

| Group | Keys | Notes |
| --- | --- | --- |
| Runtime | `APP_ENV`, `LOG_LEVEL`, `LOG_JSON`, `DEMO_MODE`, `DEFAULT_CHAIN` | `DEMO_MODE=true` swaps in scripted agents; no outbound LLM traffic |
| Agents | `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `ALPHA_MODEL`, `RISK_MODEL`, timeouts, retries, concurrency, per-MTok prices | risk model is the reasoner with veto power |
| Storage | `DATA_DIR`, `HOT_DB_PATH`, `PARQUET_ROOT`, `PARQUET_COMPRESSION`, retention windows | one SQLite file + one Parquet lake |
| R2 | `R2_ENABLED`, `R2_ENDPOINT_URL`, `R2_BUCKET`, keys, prefix | optional mirror of the cold lake |
| Scanners | `GOPLUS_APP_KEY/SECRET`, `ETHERSCAN_API_KEY`, `LUNARCRUSH_API_KEY`, `X_BEARER_TOKEN`, timeouts | a missing key degrades that layer to `SKIPPED`, it does not fail the cycle |
| Alerting | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ALERT_MIN_SCORE`, `ALERT_ON_VETO` | empty credentials = alerts silently disabled |
| Loop | `SCAN_INTERVAL_SECONDS`, `TOKEN_CONCURRENCY`, `MAINTENANCE_INTERVAL_SECONDS` | |

`Settings.missing_critical()` names the keys that would make a non-demo run unusable,
and the bot prints them instead of burning API credits on a half-configured stack.

---

## Storage

**Hot (SQLite)** — `data/hot/alpha_sentinel.db`, WAL mode, schema versioned:

| Table | Holds |
| --- | --- |
| `verdicts` | one row per decision, with both agent payloads as JSON |
| `runs` | cycle bookkeeping: requested/collected/verdicts/approved/vetoed/errors |
| `seen_tokens` | last scan time per token, drives the cooldown |
| `alerts` | alert ledger, drives dedupe (`DEDUPE_WINDOW_HOURS`) |
| `kv` | small scalars (schema version, watermarks) |

**Cold (Parquet)** — `data/cold/<dataset>/dt=<YYYY-MM-DD>/part-*.parquet`, zstd:

| Dataset | Written when |
| --- | --- |
| `verdicts` | every persisted verdict (dedupe: token, chain, decided_at) |
| `layer_signals` | every collected signal (dedupe: token, chain, layer, observed_at) |
| `runs` | one row per finished cycle |

Writes are atomic (`os.replace`), idempotent under the dedupe keys, and optionally
mirrored to R2. `engine.maintenance()` prunes both stores on
`MAINTENANCE_INTERVAL_SECONDS`; `--maintenance` forces one pass.

---

## Tests

```bash
make test                                   # 167 tests, ~1s
.venv/bin/python -m pytest tests/ -q -k engine
.venv/bin/python -m flake8 . --count        # must print 0
```

The suite is **hermetic**: `tests/conftest.py` roots every path in `tmp_path`, forces
`DEMO_MODE=true`, blanks Telegram credentials, disables R2, and hands tests scripted
`FakePipeline` / `FakeAudit` / `FakeAlerter` objects. No network, no API keys, no
writes to the repo — which is why CI can run it on every push.

| File | Covers |
| --- | --- |
| `test_scoring.py` | weighted prior, blending, risk drag, veto precedence, position tiers |
| `test_parsing.py` | JSON scavenging, score/confidence coercion, CRITICAL→veto inference, malformed payloads |
| `test_watchlist.py` | YAML parsing, defaults, caps, named-list selection, validation errors |
| `test_hot_store.py` | schema, verdict roundtrip, cooldown, alert dedupe, run lifecycle, pruning |
| `test_alerts.py` | message formatting, escaping, dedupe, disabled-credential paths |
| `test_engine.py` | full cycle, dry-run persists nothing, per-token failure isolation, cold-store failure swallowed, veto end-to-end |
| `test_dashboard.py` | Streamlit `AppTest` render against empty stores (no server, no browser) |

---

## Docker

```bash
cp .env.example .env          # optional: DEMO_MODE works with no keys
docker compose up -d --build  # bot + dashboard
docker compose logs -f bot
docker compose down
```

* The image runs as an unprivileged user (`uid 10001`) with a `--status` healthcheck.
* `./data` is bind-mounted: the lake survives container rebuilds.
* `config/watchlists.yaml` is mounted read-only; edit it on the host and the next
  cycle picks it up (the loader caches by mtime).
* Dashboard: <http://localhost:8501> (`DASHBOARD_PORT` to change). It is read-only by
  design — no token, no order, no button that writes.

---

## CI / repo hygiene

| Artifact | Purpose |
| --- | --- |
| `.circleci/config.yml` | `lint` (flake8 + byte-compile), `test` (pytest with JUnit artifacts), `docker-build` (image + `--status` smoke) |
| `.github/workflows/codeql.yml` | CodeQL `security-and-quality` on push/PR/weekly |
| `.github/dependabot.yml` | weekly grouped pip PRs, monthly actions PRs, major bumps of numpy/pandas/pyarrow ignored |
| `.coderabbit.yaml` | review rules per path (prompt boundary, veto precedence, hermetic tests, secrets) |
| `Makefile` | `make ci` = lint + tests + compileall + offline smoke, the same gate CI runs |

---

## Operational notes

* **Nothing here is investment advice.** The output is a research note with a score.
* A `degraded` flag on a verdict means an agent answered but the pair was incomplete;
  the engine counts it as an error so a run with degraded audits reports `partial`.
* Telegram failures never fail a cycle; the alert ledger records only successful sends,
  so a failed send is retried on the next scan of that token.
* Cooldowns are per `(token_address, chain)` and configurable per watchlist entry.
* Logs are JSON by default (`LOG_JSON=true`) with stable `msg` keys — grep `cycle_finished`,
  `verdict_persist_failed`, `cold_store_write_failed`, `alert_failed`.

---

## Layout

```
ai_engine/    DeepSeek client, prompts, parsers, dual-agent flow, demo client
bot/          watchlist loader, scan engine, Telegram alerter, CLI
config/       settings (env-driven dataclasses), watchlists.yaml
core/         models, deterministic scoring, HTTP + logging helpers
scanners/     seven layer scanners, aggregating pipeline
storage/      SQLite hot store, Parquet lake (+ optional R2)
dashboard/    Streamlit read-only UI
tests/        hermetic pytest suite
```

