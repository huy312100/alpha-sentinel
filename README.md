# AlphaSentinel

A cross-asset screener with a veto. It collects on-chain, derivatives, social,
DEX/venue microstructure, airdrop, issuer fundamentals, macro and technical
signals for a watchlist — **crypto tokens, equities, ETFs, indices, FX pairs,
commodity futures and rates** — then asks two LLM agents
(DeepSeek, GPT, Claude — any OpenAI-compatible endpoint) to judge it:
**Agent 1 argues the bull case, Agent 2 tries to kill the trade**, and both are
funnelled through a deterministic scorer that has final say. Verdicts are written
to SQLite (hot) and Parquet (cold), alerts go to Telegram, and a Streamlit
dashboard reads both stores.

The design constraint is simple: **the LLM is a judge of narrative, never the sole
source of truth.** Every number that decides a trade is computed in `core/scoring.py`
with pure arithmetic, so a verdict is reproducible and testable without an API key.

---

## Architecture

```
config/watchlists.yaml   (crypto contracts + listed symbols)
        │
        ▼
┌───────────────────┐   one manifest per asset class, every layer isolated
│ scanners.pipeline │──▶ crypto: onchain · derivatives · social · dex ·
│ scanners.registry │           ecosystem · tokenomics · technicals · security
└─────────┬─────────┘   listed: market · fundamentals · macro · technicals
          │                     (+ a routed stub per market: etf_flows,
          │                      index_breadth, fx_carry, …)
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
│ core.scoring      │   1. hard contract-security flag (crypto only)
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
one dead instrument does not abort the run; a Parquet or Telegram hiccup is logged
and the loop keeps going. `--dry-run` collects and audits but persists and sends
nothing.

`scanners.registry` owns the routing table (`ASSET_LAYER_MAP`). A layer never runs
because the pipeline knows its name — it runs because the instrument's `asset_class`
has a manifest entry for it. Three consequences worth knowing:

* **Crypto is eight dedicated layers** under `scanners/crypto/`, one module each.
  Nothing is shared between them except the HTTP client and the indicator maths.
* **Every other market is routed today**, even where its dedicated data is not
  built yet: `etf_flows`, `index_breadth`, `fx_carry`, `commodity_inventory`,
  `equity_ownership` and `rates_curve` are stubs that return `SKIPPED` with zero
  weight, so a real collector can land without touching the pipeline.
* **A failing layer is skipped, never neutral.** An exception, a timeout or an
  `UNAVAILABLE` provider becomes `SKIPPED` with weight 0, so a dead API cannot drag
  a score toward 50. Layer 8 (security) is `always_on` and a critical flag there
  vetoes the verdict regardless of every other score.

---

## Quickstart (no API key required)

```bash
make install                 # venv + pinned deps
cp .env.example .env         # DEMO_MODE=true works as-is
make demo                    # scripted agents: APPROVED verdict, formatted
make honeypot                # scripted agents: VETO with hard security flags
make test                    # hermetic suite, no network
```

Then, with real keys in `.env` (`LLM_API_KEY` — plus `LLM_PROVIDER` if you are not on
DeepSeek — optionally `GOPLUS_APP_KEY`, `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`):

```bash
make status                  # what the hot store knows right now
make health                  # is the scan loop still turning? (exit 1 if not)
make dry-run                 # one cycle, nothing persisted
make once                    # one real cycle
make bot                     # the daemon loop (SCAN_INTERVAL_SECONDS)
make dashboard               # Streamlit on :8501
```

### Markets

One pipeline, seven asset classes: `crypto`, `equity`, `etf`, `index`, `fx`,
`commodity`, `rate`. Scoring, veto hierarchy, storage and dashboard are identical
— only the layer mix changes.

| Layer | Crypto | Equity | ETF / index / FX / commodity / rate |
| --- | --- | --- | --- |
| `onchain` | holder distribution (Blockscout) | — | — |
| `dex` | pool depth, turnover, order flow | — | — |
| `market` | — | dollar volume, 3m trend, 52w position | the same venue read |
| `fundamentals` | — | SEC 10-K facts (needs `SEC_USER_AGENT`) | — |
| `macro` | CoinGecko + Fear & Greed | VIX, DXY, US10Y, S&P, gold | the same cross-asset tape |
| `technicals` | hourly GeckoTerminal candles | daily exchange bars | daily exchange bars |
| `derivatives` | funding / OI / basis | — | — |
| `social` | LunarCrush | — | — |
| `airdrop` | token unlocks | — | — |
| `security` | GoPlus contract screen | — | — |

A layer that does not apply returns `SKIPPED` with **zero weight**, so it neither
helps nor hurts the prior. Missing contract-security data for an equity is not the
same as "safe", and it is not treated as a defect either.

**Identity.** A crypto entry is a *contract address*; every other market is a
*symbol*. The watchlist derives the data-provider symbol, so operators never type
provider syntax:

| `asset_class` | stored identity | derived provider symbol | default venue |
| --- | --- | --- | --- |
| `crypto` | `0x…` / Solana pubkey | *(address is used)* | chain slug |
| `equity` | `AAPL` | `AAPL` (`BRK.B` → `BRK-B`) | `us_equity` |
| `etf` | `SPY` | `SPY` | `us_equity` |
| `index` | `SPX` | `^GSPC` (`VIX` → `^VIX`) | `index` |
| `fx` | `EURUSD` | `EURUSD=X` | `fx` |
| `commodity` | `GC` | `GC=F` | `futures` |
| `rate` | `US10Y` | `^TNX` | `rates` |

```yaml
watchlists:
  equities:
    asset_class: equity          # section-level default
    venue: us_equity
    instruments:                 # "instruments" and "tokens" are interchangeable
      - AAPL                     # bare strings are tickers, addresses are contracts
      - symbol: BRK.B
        provider_symbol: BRK-B   # optional override for anything exotic
  fx:
    asset_class: fx
    instruments: [EURUSD, USDJPY]
  commodities:
    asset_class: commodity
    instruments: [GC, CL, HG]
```

Listed-instrument data comes from the keyless Yahoo Finance chart endpoint
(`scanners/shared/market_data.py`); crypto keeps DexScreener + GeckoTerminal.
One fetch feeds both the `market` layer and `technicals` within a collection.

**Fundamentals are opt-in.** Set `SEC_USER_AGENT="AlphaSentinel you@example.com"`
to enable SEC EDGAR lookups — the SEC's fair-access policy requires a contact.
Without it the layer reports `SKIPPED` and makes no outbound call.

Probe any market from the CLI:

```bash
python -m scanners --symbol AAPL --asset-class equity
python -m scanners --symbol EURUSD --asset-class fx
python -m scanners --symbol US10Y --asset-class rate --json
```

Three honesty notes about the cross-asset reads:

* **No volume, no liquidity score.** FX pairs and yield indices report zero traded
  volume, so the `market` layer keeps liquidity neutral instead of flagging the
  instrument as illiquid.
* **A rate chart is the yield itself.** For `rate`, technicals measure momentum in
  the yield — a rising 10y is *not* bullish for a bond position; the risk agent is
  told the asset class and should read it accordingly.
* **Annual fundamentals only.** SEC 10-K figures need at least two fiscal years; a
  recent IPO or a non-US listing gets `SKIPPED`, never an invented ratio.

### Model providers

The agents only need an OpenAI-compatible `/chat/completions` endpoint, so the model
vendor is configuration, not code. `LLM_PROVIDER` picks a preset; any `LLM_BASE_URL`,
`ALPHA_MODEL`, `RISK_MODEL` or price you set wins over it:

| `LLM_PROVIDER` | Base URL | Default Agent 1 / Agent 2 |
| --- | --- | --- |
| `deepseek` (default) | `https://api.deepseek.com` | `deepseek-chat` / `deepseek-reasoner` |
| `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` / `gpt-4o` |
| `anthropic` | `https://api.anthropic.com/v1` | `claude-sonnet-4-5` / `claude-sonnet-4-5` |
| `openrouter` | `https://openrouter.ai/api/v1` | `deepseek/deepseek-chat` / `deepseek/deepseek-r1` |
| `custom` | your `LLM_BASE_URL` | you must set both models (vLLM, Ollama, Together, Groq, LiteLLM, a gateway) |

```bash
# GPT models (override the preset defaults if you want a different pair)
LLM_PROVIDER=openai LLM_API_KEY=sk-... make dry-run

# Mixed vendors behind one gateway: cheap analyst, strong auditor
LLM_PROVIDER=openrouter LLM_API_KEY=sk-or-... \
  ALPHA_MODEL=deepseek/deepseek-chat RISK_MODEL=anthropic/claude-sonnet-4.5 make dry-run

# Local models through Ollama
LLM_PROVIDER=custom LLM_BASE_URL=http://localhost:11434/v1 \
  ALPHA_MODEL=qwen2.5:32b RISK_MODEL=qwen2.5:32b make dry-run
```

Two things make this portable without per-vendor branches in the code:

* **Wire-format adaptation.** If a model rejects `response_format`, `max_tokens`
  (OpenAI's o-series wants `max_completion_tokens`) or a custom `temperature`, the
  client rewrites the payload once and retries; JSON is then recovered from prose by
  `ai_engine/parsing.py`.
* **Cost ledger is an estimate.** `LLM_INPUT_USD_PER_MTOK` / `LLM_OUTPUT_USD_PER_MTOK`
  default to the preset's price for the *risk* model; set them to your contract rates.
* **Spend ceilings.** `LLM_MAX_USD_PER_CYCLE` / `LLM_MAX_USD_PER_DAY` bound what an
  unattended daemon can bill (`0`/unset = no ceiling). The client refuses a request
  that would cross one and the cycle stops with an explicit `llm_budget_exhausted`
  error on the instruments still queued — no verdict is fabricated and no alert fires,
  because a budget stop is an operator decision, not a provider failure. The daily
  total is persisted per UTC day in the hot store (`llm_spend_usd:<date>`), so a
  restart does not reset today's bill; `--status` prints it. A ceiling configured
  without prices is a boot error, since nothing would be measurable.

Legacy `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL` and `DEEPSEEK_*` tuning variables are
still read whenever the matching `LLM_*` name is absent, so old `.env` files boot
unchanged.

---

## CLI

### `python -m bot.main`

| Flag | Effect |
| --- | --- |
| `--status` | print settings, watchlist and hot-store counts, then exit |
| `--health` | cycle-freshness verdict for the container healthcheck; exit `1` when the loop stopped |
| `--once` | single cycle, then exit |
| `--daemon` | loop forever (default) |
| `--maintenance` | run retention/pruning once and exit |
| `--dry-run` | collect + audit, persist nothing, send nothing |
| `--no-alerts` | run normally but suppress Telegram |
| `--watchlist NAME` | restrict to one or more named lists (repeatable) |
| `--token ADDR` | scan explicit instruments instead of the watchlist (repeatable; accepts a contract address or a ticker) |
| `--chain SLUG` | chain/venue hint for `--token` |
| `--limit N` | override `max_tokens_per_cycle` |
| `--force` | ignore the per-token cooldown |
| `--max-cycles N` | stop after N cycles (daemon mode) |
| `--json` | machine-readable report |
| `--json-logs` | structured logs |

Exit codes: `0` ok, `1` cycle produced nothing (or, for `--health`, the loop is
stalled), `2` usage/config error.

### Other entrypoints

```bash
python -m scanners --token 0x... --chain ethereum [--symbol WETH] [--json]
python -m scanners --symbol AAPL --asset-class equity [--json]
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

## Learned alpha scorer (optional)

`ml/` turns the bot's own audits into a dataset and can replace Agent 1 with a
model trained on it. Off by default: `ALPHA_PREDICTOR=llm` runs exactly the
two-agent flow above, and nothing in `ml/` is imported on that path.

```
cycle ──► training_rows (Parquet)          one row per audit: features + labels
             │
             ├─ make ml-export ──► data/exports/train.parquet   (+ prior baseline)
             └─ make ml-train  ──► models/alpha_model.pkl       (time split, GBM)

model ──► prediction ──► safe? ──yes──► verdict, no alpha call
                          │no
                          └──────► Agent 1 (LLM) ──► verdict
```

The model answers first; the LLM is downgraded to an adjudicator. A token is
escalated to it when any of these holds:

| Trigger | Why |
| --- | --- |
| prediction within `ALPHA_ESCALATE_MARGIN` of 65 or 50 | the verdict could flip, so the cheap path is not safe |
| input trust below `ALPHA_MIN_CONFIDENCE` | too few core features are present to interpolate |
| a core market input left its training range, or 5% of features did | regime drift: the model would be extrapolating |
| no artefact, or the estimator raised | fall back to the LLM, never fail the audit |

Only the alpha agent is replaced. The risk auditor keeps its veto, so a model
score can never outvote a honeypot flag or a CRITICAL finding.

**What this buys, honestly.** Distilling the LLM's *score* cannot make it more
accurate - it reproduces the teacher faster and cheaper: ~1 ms instead of tens of
seconds, deterministic, and no alpha call on the confident majority. That is
roughly a third of the calls, not the whole bill, because the risk reasoner is
the larger line item. If your goal is accuracy rather than cost, train on a
forward-return column instead (`python -m ml.train --label forward_return_7d`) -
that is the only target that can beat the teacher, and it needs weeks of price
history to exist first.

Guardrails: the deterministic veto hierarchy is untouched, a missing or corrupt
artefact degrades to the LLM with a log line instead of failing boot, a model
prediction always carries its id into `FinalVerdict.alpha.model`, and an
artefact whose feature list contains a label column is refused as leakage.

```bash
python -m pip install -r requirements-ml.txt   # optional; the image does not need it
ALPHA_SHADOW=true make bot        # measure agreement on live traffic, LLM still decides
ALPHA_PREDICTOR=model make bot    # let the model answer first
```

---

## Configuration

Everything is environment-driven and read once at process start (`config/settings.py`,
`Settings.load()`). `.env` is loaded automatically for local runs; in Docker, values
come from the compose `env_file`. `.env.example` is the authoritative list; these are
the groups that change behaviour:

| Group | Keys | Notes |
| --- | --- | --- |
| Runtime | `APP_ENV`, `LOG_LEVEL`, `LOG_JSON`, `DEMO_MODE`, `DEFAULT_CHAIN` | `DEMO_MODE=true` swaps in scripted agents; no outbound LLM traffic |
| Agents | `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_BASE_URL`, `ALPHA_MODEL`, `RISK_MODEL`, timeouts, retries, concurrency, per-MTok prices, `LLM_MAX_USD_PER_CYCLE`/`LLM_MAX_USD_PER_DAY` | any OpenAI-compatible endpoint; `RISK_MODEL` is the reasoner with veto power; the spend ceilings need prices to bind |
| Storage | `DATA_DIR`, `HOT_DB_PATH`, `PARQUET_ROOT`, `PARQUET_COMPRESSION`, retention windows | one SQLite file + one Parquet lake |
| Learned alpha | `ALPHA_PREDICTOR`, `ALPHA_MODEL_PATH`, `ALPHA_SHADOW`, `ALPHA_ESCALATE_MARGIN`, `ALPHA_MIN_CONFIDENCE`, `ML_WRITE_TRAINING_ROWS` | optional model scorer; the default `llm` is the historical behaviour |
| R2 | `R2_ENABLED`, `R2_ENDPOINT_URL`, `R2_BUCKET`, keys, prefix | optional mirror of the cold lake |
| Scanners | `GOPLUS_APP_KEY/SECRET`, `ETHERSCAN_API_KEY`, `LUNARCRUSH_API_KEY`, `X_BEARER_TOKEN`, `SEC_USER_AGENT`, timeouts | a missing key degrades that layer to `SKIPPED`, it does not fail the cycle |
| Endpoints | `ENDPOINTS_FILE`, `<VENDOR>_BASE_URL`, `<VENDOR>_<NAME>_URL` | every outbound URL (host **and** path) lives in `config/endpoints.yaml`; mount that shape as a ConfigMap via `ENDPOINTS_FILE` to replace entries in bulk, or move one vendor/one endpoint from `.env`. No scanner contains a URL |
| Explorers | `BLOCKSCOUT_EXPLORER_BASE_URL`, `BLOCKSCOUT_BASE_URL_<CHAIN>` | per-chain override for holder/transfer reads; the template covers the rest |
| Layers | `ENABLED_LAYERS` (default: every layer routed for the instrument's asset class) | narrow globally; crypto and listed markets each keep their own manifest, and `security` cannot be switched off |
| Alerting | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ALERT_MIN_SCORE`, `ALERT_ON_VETO` | empty credentials = alerts silently disabled |
| Loop | `SCAN_INTERVAL_SECONDS`, `TOKEN_CONCURRENCY`, `MAINTENANCE_INTERVAL_SECONDS`, `HEALTH_MAX_STALE_SECONDS` | `HEALTH_MAX_STALE_SECONDS` tunes the `--health` window (0 = 3× the interval, min 30 min) |

`Settings.missing_critical()` names the keys that would make a non-demo run unusable,
and the bot prints them instead of burning API credits on a half-configured stack.

### Vendor endpoints are data

Every outbound URL — host *and* path — lives in [`config/endpoints.yaml`](config/endpoints.yaml),
one entry per vendor resource (`coingecko.contract`, `geckoterminal.pool_ohlcv`,
`sec.company_facts`, ...). Scanner code asks for a name and never sees a URL:

```python
url = ctx.settings.endpoints.url("coingecko.contract", platform=platform, address=token.token_address)
```

A deployment can therefore redirect traffic without touching Python:

```yaml
# /etc/alphasentinel/endpoints.yaml - mounted, merged over the bundled catalogue
coingecko:
  base: https://coingecko-proxy.internal/api/v3
```

```bash
ENDPOINTS_FILE=/etc/alphasentinel/endpoints.yaml   # or, for one vendor:
COINGECKO_BASE_URL=https://coingecko-proxy.internal/api/v3
COINGECKO_CONTRACT_URL=https://other-gateway.internal/coins/{platform}/{address}
```

Boot fails loudly (`ConfigError`) if an entry is missing its scheme/host, uses an
unknown placeholder, or if the effective catalogue cannot satisfy a name the code
calls (`REQUIRED_ENDPOINTS`). A test also asserts that no file under `scanners/`
contains an `http(s)://` literal, so a hardcoded URL cannot creep back in.

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
| `verdicts` | every persisted verdict (dedupe: token, chain, asset class, decided_at) |
| `layer_signals` | every collected signal (dedupe: token, chain, asset class, layer, observed_at) |
| `runs` | one row per finished cycle |

Writes are atomic (`os.replace`), idempotent under the dedupe keys, and optionally
mirrored to R2. `engine.maintenance()` prunes both stores on
`MAINTENANCE_INTERVAL_SECONDS`; `--maintenance` forces one pass.

---

## Tests

```bash
make test                                   # 599 tests, <2s
.venv/bin/python -m pytest tests/ -q -k engine
.venv/bin/python -m flake8 . --count        # must print 0
```

The suite is **hermetic**: `tests/conftest.py` roots every path in `tmp_path`, forces
`DEMO_MODE=true`, blanks Telegram credentials, disables R2, and hands tests scripted
`FakePipeline` / `FakeAudit` / `FakeAlerter` objects. No network, no API keys, no
writes to the repo — which is why CI can run it on every push.

| File | Covers |
| --- | --- |
| `test_settings.py` | provider presets, `LLM_*` parsing, legacy `DEEPSEEK_*` aliases, fail-fast validation, the endpoint catalogue (overrides, missing entries, no URL literals left in scanners) |
| `test_llm_client.py` | request shaping per provider, JSON-mode/`max_tokens`/`temperature` adaptation, cost ledger, error paths |
| `test_instruments.py` | asset classes, symbol normalisation, provider-symbol mapping, identity/venue rules |
| `test_market_data.py` | Yahoo chart parsing (null bars, halted sessions), quote metrics, series intervals, caching |
| `test_macro.py` | cross-asset tape summary, risk-on scoring, partial-data confidence, no-data refusal |
| `test_fundamentals.py` | SEC company facts (annual-only, restatements), quality scoring, gating |
| `test_pipeline.py` | layer registry, per-market gating, snapshot enrichment, CLI target resolution |
| `test_scoring.py` | weighted prior, blending, risk drag, veto precedence, position tiers |
| `test_parsing.py` | JSON scavenging, score/confidence coercion, non-finite rejection, CRITICAL→veto inference, missing risk score |
| `test_dual_audit_flow.py` | repair-then-fail-closed risk parsing, retry-sized watchdog, per-snapshot isolation |
| `test_watchlist.py` | YAML parsing, defaults, caps, crypto contracts + listed symbols, section-level `market:`, validation errors |
| `test_hot_store.py` | schema (+ in-place migration of verdicts/alerts/seen), asset-class filter, cooldown, alert dedupe by venue+status, pruning |
| `test_alerts.py` | message formatting, escaping, market lines, approval-vs-veto dedupe, secret-free failure reasons |
| `test_engine.py` | full cycle, dry-run persists nothing, per-instrument failure isolation, alert/selection guards, veto end-to-end |
| `test_parquet_store.py` | idempotent compaction, `overwrite=False` uploads, compacted-file-then-cleanup, scoped remote retention |
| `test_scanners.py` | base-token pair filtering, supply-based concentration, ISO-8601 wash timestamps |
| `test_logging.py` | secret redaction across `msg`, `extra` and tracebacks, escaped JSON values, metadata keys |
| `test_dashboard.py` | Streamlit `AppTest` render against empty stores (no server, no browser) |

---

## Docker

```bash
cp .env.example .env          # optional: DEMO_MODE works with no keys
docker compose up -d --build  # bot + dashboard
docker compose logs -f bot
docker compose down
```

* The image runs as an unprivileged user (`uid 10001`); its healthcheck is
  `python -m bot.main --health`, which fails when no cycle has finished inside the
  freshness window (`HEALTH_MAX_STALE_SECONDS`, or 3× `SCAN_INTERVAL_SECONDS`, min
  30 min) instead of merely proving the process can print a status page.
* `./data` is bind-mounted: the lake survives container rebuilds.
* `config/watchlists.yaml` is mounted read-only; edit it on the host and the next
  cycle picks it up (the loader caches by mtime).
* Dashboard: <http://localhost:8501> (`DASHBOARD_PORT` to change). It is read-only by
  design — no token, no order, no button that writes.

---

## CI / repo hygiene

| Artifact | Purpose |
| --- | --- |
| `.circleci/config.yml` | `lint` (flake8 + byte-compile), `test` (pytest with JUnit artifacts), `docker-build` (image + `--status`/`--health` smoke, plus the boot-gate check) |
| `.github/workflows/codeql.yml` | CodeQL `security-and-quality` on push/PR/weekly |
| `.github/dependabot.yml` | weekly grouped pip PRs, monthly actions PRs, major bumps of numpy/pandas/pyarrow ignored |
| `.coderabbit.yaml` | review rules per path (prompt boundary, veto precedence, hermetic tests, secrets) |
| `.github/workflows/ai-review.yml` | free AI code review on every PR: PR-Agent against a local Ollama model on the runner, so there is no API key and no per-token bill — see `.github/AI_REVIEW.md` |
| `.github/AI_REVIEW.md` | model sizes, runtime, slash commands and the fork-PR policy for that reviewer |
| `Makefile` | `make ci` = lint + tests + compileall + offline smoke, the same gate CI runs |

---

## Operational notes

* **Nothing here is investment advice.** The output is a research note with a score.
* A `degraded` flag on a verdict means an agent answered but the pair was incomplete;
  the engine counts it as an error so a run with degraded audits reports `partial`.
* Telegram failures never fail a cycle; the alert ledger records only successful sends,
  so a failed send is retried on the next scan of that token.
* Cooldowns are per `(identity, venue)` — contract+chain for crypto, symbol+venue
  for listed instruments — and configurable per watchlist entry.
* Logs are JSON by default (`LOG_JSON=true`) with stable `msg` keys — grep `cycle_finished`,
  `verdict_persist_failed`, `cold_store_write_failed`, `alert_failed`.

---

## Layout

```
ai_engine/    LLM client (any OpenAI-compatible API), prompts, parsers, dual-agent flow, demo client
bot/          watchlist loader (crypto + listed markets), scan engine, Telegram alerter, CLI
config/       settings (env-driven dataclasses), endpoints.yaml (every vendor URL),
              watchlists.yaml
core/         instrument identity, models, deterministic scoring, HTTP + logging helpers
ml/           optional learned alpha scorer: features, training_rows, train, predictor
              + escalation policy (imported only when ALPHA_PREDICTOR != llm)
scanners/     framework only at the root: __init__, base (layer contract),
              registry (asset_class -> layer routing), pipeline, __main__
  crypto/       eight self-contained layers (layer1_onchain .. layer8_security);
                each layer owns the vendor clients it scores (DexScreener,
                GeckoTerminal, GoPlus, Coinglass, LunarCrush, ...)
  shared/       http_client (retry + per-host limit), technicals/ (analysis
                library split into primitives, snapshot, scoring, flags),
                market_data + series (quote/candle transport), listed/ (generic
                non-crypto layers the market packages compose)
  equities/ etfs/ indices/ forex/ commodities/ rates/   routed market manifests
storage/      SQLite hot store, Parquet lake (+ optional R2)
dashboard/    Streamlit read-only UI
tests/        hermetic pytest suite
```

