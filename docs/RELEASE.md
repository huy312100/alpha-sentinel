# Release and soak runbook

For whoever is at the terminal when something needs deciding. Every command here was
run at least once; where a number is quoted it was measured, not estimated.

## v0.1.0 — what it is

The first operational baseline: the dual-agent pipeline running unattended under
launchd, with a restart-on-unhealthy supervisor, verified hot-store snapshots, and
health that answers two separate questions (is the loop turning / is it producing).

Verify a release candidate before tagging it:

```bash
make ci                     # lint + hermetic tests + byte-compile + smoke
git push origin main
sha=$(git rev-parse origin/main)
gh api "repos/huy312100/alpha-sentinel/commits/$sha/check-runs" \
  --jq '.check_runs[] | "\(.status)  \(.conclusion // "-")  \(.name)"'
```

The checks are queried **by commit**, not with `gh pr checks`: that command resolves a
pull request from the current branch, so on `main` it either finds no PR or reports the
checks of some unrelated one, and a green read-out can be a wrong read-out about the
commit you are about to release.

CI covers what this machine cannot: the image build, ARM64 wheel resolution for every
pin, CodeQL, and CircleCI's boot gates. There is no Docker daemon on the deployment
host, so a local `docker build` is not available as evidence.

## Rehearsing a restore (do this before you need it)

```bash
make backup                      # snapshot the hot store, verify it, prune to BACKUP_KEEP
make backup-verify               # restore newest snapshot into a throwaway dir
```

`backup-verify` is not a checksum. It restores the file, boots `HotStore` on it — so
the same migrations a real restore would run — and fails if any table comes back with
fewer rows than the snapshot recorded. Expected output ends with `"problems": []`.

Rehearsed at v0.1.0: 279 rows, `schema_version: 3`, `problems: []`.

## Rehearsing a rollback

The question a rollback asks is not "does the old code run" but "can the old code
serve the data the new code wrote". Rehearse it in a worktree, never by moving the
deployment checkout, and against a **copy** of a snapshot — not the live database.

```bash
set -euo pipefail
repo=$PWD
prev=/tmp/as-prev
restore=/tmp/rollback-copy.db
git worktree add "$prev" <previous-tag>
install -m 600 "$(ls -1t "$repo"/data/backups/*.db | head -1)" "$restore"
# Without this the block can continue against an older copy from a previous rehearsal -
# or, worse, HotStore.initialize() happily creates an empty store at that path and the
# rehearsal reports a total data loss that never happened.
test -s "$restore"
copy_rows=$(sqlite3 "$restore" 'SELECT COUNT(*) FROM verdicts')
echo "restored copy holds $copy_rows verdicts"

# Export once. A `VAR=x cmd` prefix applies to that one command only, so a second
# command in the same block would silently read the live store instead.
export HOT_DB_PATH="$restore" DATA_DIR=/tmp/rollback-home PARQUET_ROOT=/tmp/rollback-lake
export WATCHLIST_PATH="$prev/config/watchlists.yaml" DEMO_MODE=true
( cd "$prev" && "$repo"/.venv/bin/python -m bot.main --status )
( cd "$prev" && "$repo"/.venv/bin/python -m bot.main --health )
```

Both commands must see `$restore`; if `HOT_DB_PATH` is unset or empty the settings fall
back to the configured live database and the rehearsal inspects production while
reporting on the copy.

What you should see: `--status` prints the snapshot's verdicts, and `--health` reports
`cycle_stale` or `verdicts_stale`. Those are correct answers, not failures — the
restored data genuinely is old, and a check that called it fresh would be the bug.

Rolling the live daemon back afterwards, from the deployment checkout and only there:

```bash
set -euo pipefail
cd "$repo"                         # the worktree above is a different directory
git checkout <previous-tag>        # if this fails, the next line must not run
launchctl kickstart -k "gui/$(id -u)/com.alphasentinel.bot"
```

Leaving the shell inside `$prev` and running `git checkout` there changes the rehearsal
worktree while the daemon keeps running the unmodified deployment code — the two
commands look like they belong together and do not.

`SCHEMA_VERSION` is 3 at v0.1.0 and has not moved, so an older build opens a newer
store. If a future release raises it, the rollback above stops being safe and this
section must say so.

## Restoring for real (after a failure, not a rehearsal)

`backup-verify` restores into a temporary directory and deletes it; it never touches the
live path, by design. The live restore is this, and it is the one procedure here that
can destroy data if the order is wrong:

```bash
set -euo pipefail
uid=$(id -u); repo=/Users/nguyenngocduchuy/Projects/alpha-sentinel
cd "$repo"

# 1. Stop the supervisor first. It kickstarts a bot it considers unhealthy, which
#    mid-restore means a half-copied database served to a live cycle.
launchctl bootout "gui/$uid/com.alphasentinel.watchdog"
launchctl bootout "gui/$uid/com.alphasentinel.bot"

# 2. Keep the broken store; it is the only evidence of what the failure looked like.
#    Skippable on purpose (|| true): a store unreadable enough to copy is the case
#    where this step has nothing to offer.
cp -p data/hot/alpha_sentinel.db "/tmp/failed-store-$(date -u +%Y%m%dT%H%M%SZ).db" 2>/dev/null || true

# 3. Restore the newest verified snapshot, and remove its stale sidecars. A leftover
#    -wal replays *on top of* the file you just restored.
new=$(ls -1t data/backups/*.db | head -1)
test -s "$new"                      # an empty or missing snapshot must not be installed
install -m 600 "$new" data/hot/alpha_sentinel.db
rm -f data/hot/alpha_sentinel.db-wal data/hot/alpha_sentinel.db-shm

# 4. Prove it before it has writers on it.
.venv/bin/python -m bot.main --status

# 5. Bot back, then the supervisor, and only in that order.
launchctl bootstrap "gui/$uid" ~/Library/LaunchAgents/com.alphasentinel.bot.plist
launchctl bootstrap "gui/$uid" ~/Library/LaunchAgents/com.alphasentinel.watchdog.plist
```

Expect to lose at most one `BACKUP_INTERVAL_SECONDS` window of verdicts, plus whatever
`seen_tokens` and the spend ledger recorded since that snapshot. Those two are the
reason the cadence exists: nothing else holds a copy of them.

## Seven-day soak

Pass conditions, all measured from the store — not from uptime vibes. `cd` to the
deployment checkout first; every path below is relative to it:

```bash
cd /Users/nguyenngocduchuy/Projects/alpha-sentinel
.venv/bin/python -m bot.main --health                # exit 0 throughout
.venv/bin/python -m bot.main --status                # cycles advancing, verdicts landing
sqlite3 data/hot/alpha_sentinel.db "SELECT status, COUNT(*) FROM runs GROUP BY status"
sqlite3 data/hot/alpha_sentinel.db "SELECT key, value_json FROM kv"   # backup + productive markers fresh
ls -1t data/backups | head -1                        # newest snapshot < BACKUP_INTERVAL_SECONDS + slack
```

These paths are the checked-in defaults. If `DATA_DIR` or `HOT_DB_PATH` is overridden in
the environment (`.env`, the LaunchAgent, or a volume mount in Docker), read the real
location from the binary rather than guessing it: `sqlite3 "$(.venv/bin/python -m
bot.main --print-backup-dir)/..."` for snapshots, and `--status` prints the hot store
path it resolved.

- no manual restart for the full seven days (a restart resets the watchdog's budget and
  the meaning of "unattended");
- zero `verdicts_stale` / `never_productive` in `--health`, and zero `unhealthy` that
  the supervisor could not explain from a log line;
- `partial` runs only where a real error is recorded — cooldown skips must not appear
  there, that was the point of #37;
- spend ceilings never bind silently: every exhaustion produces an explicit degraded
  verdict, not a missing one. **Not exercisable while `DEMO_MODE=true`** — there is no
  spend to ceiling, so a demo soak proves plumbing and nothing about budget behaviour.
  Equally inert on a self-hosted endpoint, which has no prices: leave the ceilings
  unset rather than setting one over zero prices, because `Settings.validate` rightly
  refuses a ceiling nothing can measure. Budget behaviour needs a priced provider.

These four are plumbing conditions. A soak can satisfy every one of them and still tell
you nothing about whether the verdicts are worth reading — see the coverage condition
below, added because a soak-shaped run on 2026-11-06 passed all four and returned VETO
on 24 of 24 instruments.

```bash
make coverage        # per-layer matrix of what the agents were told
sqlite3 data/hot/alpha_sentinel.db \
  "SELECT status, COUNT(*) FROM verdicts WHERE decided_at > datetime('now','-1 day') GROUP BY status"
```

- **signal condition, measured rather than assumed:** coverage and verdict mix are
  reported alongside the four above, never instead of them. "`errors=0` and health OK"
  is not evidence the tool is working; it is evidence the loop is working. The first
  live cycle this deployment ran was `completed`, 0 errors, 48/48 responses parsed, and
  24/24 VETO.

## Switching the agents on: what to measure first

`DEMO_MODE=false` is one line, and it is not the whole change. Verified in this order:

```bash
brew install ollama && brew services start ollama          # or point at any hosted provider
ollama pull hermes3:8b
make dry-run                                               # one instrument, real agents, nothing persisted
LOG_LEVEL=DEBUG make dry-run                               # llm_request logs latency_ms + tokens per call
make coverage                                              # what the layers actually delivered
make once                                                  # one persisting cycle
```

What each step is for:

* **Timeout headroom, not just correctness.** The risk call is capped at
  `LLM_MAX_TOKENS`/`RISK_MAX_TOKENS` (2048/4096) against `LLM_TIMEOUT_SECONDS=180`. On a
  local 8B (~9.5 tok/s measured) a 300-token answer takes 30-50s and a 1500-token answer
  does not fit at all. One call is not the distribution: read the `latency_ms` of the
  *worst* verdict, `SELECT MAX(latency_ms) FROM verdicts`.
* **`LLM_MAX_CONCURRENCY=1` on a single local model.** Measured with four identical
  calls: 101.0s wall at parallel=1, 118.7s at 2, 114.3s at 4 — aggregate throughput flat
  at 9.4-9.7 tok/s. Fanning out to one model moves work into a queue and lengthens
  per-request latency, which is the quantity racing the timeout. The default of 4 is
  sized for a hosted API with rate limits, not for one GPU.
* **Cycle wall clock is now the interesting number.** A productive pass over 24
  instruments took 38.5 min on the local endpoint before pacing and **56 min after**
  (free-tier hosts are deliberately slowed). The health window is
  `max(3 x SCAN_INTERVAL_SECONDS, 1800)` = 10800s at a 3600s interval, and the loop
  sleeps the interval *after* a cycle finishes, so two things are bounded by it: a
  running cycle (56 min, margin 124 min) and the gap between finished cycles
  (56 + 60 = 116 min, margin 64 min). Both fit. If the watchlist grows or the model
  slows, raise `SCAN_INTERVAL_SECONDS` — which moves the derived window with it — or
  set `HEALTH_MAX_STALE_SECONDS` explicitly. Do not let the supervisor start
  kickstarting a daemon that is merely working: a restart mid-cycle discards the
  instruments that had not been reached.

### Known limitation: an endpoint outage is indistinguishable from a finding

Measured on 2026-11-06 by stopping Ollama and running one cycle with `--limit 2`:

```
run status=partial  written=2  errors=2   CLI exit=1
WETH  VETO  risk=80.0  "Fail-closed: the risk auditor did not return a usable verdict"
make health -> OK / cycle_fresh
```

Fail-closed is the designed policy and the right one for capital allocation: an
auditor that cannot answer must not wave a trade through. But the artefact it writes is
a CRITICAL veto with a *fabricated* finding, and the two surfaces that would tell you
so disagree: the run row and the CLI exit say "failed", `--health` says OK because
verdicts did land, and `ALERT_ON_VETO=true` would page each one as a discovery.

**Until that is resolved deliberately, do not enable Telegram on a self-hosted
endpoint.** An outage would not inform you, it would alarm you about 24 instruments.
The three candidate fixes, in increasing cost:

1. Suppress the *alert* (not the verdict) when `risk.model == "unavailable"` — cheapest,
   and it leaves the persisted fail-closed verdict intact for the audit trail.
2. A third health reason (`agents_degraded`) that is reported at WARNING and
   deliberately does **not** trigger a restart — restarting the bot cannot fix a vendor,
   and a restart it cannot complete would burn the watchdog's budget.
3. A `runs.degraded_verdicts` column — honest, but it raises `SCHEMA_VERSION` from 3 and
   voids the rollback guarantee in this file, which says an older build can still serve
   the data the newer one wrote.


## What has to be true before this is live for real

State as of 2026-11-06, after the agents were switched on. The two rows that were open
because the agents were scripted are closed; what replaced them is a data question, not
a plumbing one, and it is the reason "live" still needs a decision rather than a flag.

| gap | status | why it is not cosmetic |
|---|---|---|
| `DEMO_MODE=true` | **closed** | Agents went live on 2026-11-06: local Ollama, `hermes3:8b`, both prompts, both parsers. Two full cycles = 48 instrument audits / 96 LLM calls, `errors=0`, not one response that `extract_json` or the parsers could not read. |
| host sleeps | **closed** | `com.alphasentinel.hold-awake` loaded and asserted (`pmset -g assertions` reads "caffeinate asserting forever"). Measured drift before it: 70-77 min cycles against a configured 60. |
| no off-site copy | **open - needs you** | `R2_ENABLED=false`, no `rclone`. Snapshots and the store they protect are on one disk. Every rehearsal passes and a drive failure still takes both. |
| Telegram unset | **open - needs you, and read the section above first** | `alerts` has 0 rows. On the current endpoint a veto stream of 24/24 would page on every cycle, and an outage pages as if it were a discovery. |
| **data coverage** | **open - needs you** | The largest gap and the least visible. The live cycles recorded **3 `ok` layers for every asset class** (read from `data/cold/layer_signals`, or reproduced per-layer by `make coverage` once #46 lands). `onchain`/`social` skip for want of Arkham/Nansen/Dune/CryptoPanic keys, `fundamentals` skips for want of `SEC_USER_AGENT`, `derivatives`/`tokenomics` arrive degraded. The auditor is instructed to treat missing data as unverified risk, so thin coverage is not a thin verdict - it is a refusal. |
| **verdict usefulness** | **open - decision** | Across both live cycles: **24/24 VETO** each time, risk range 70-90, `alpha_score` taking three distinct values over 24 instruments. The auditor vetoed on its own judgement 10 and 9 times; the deterministic floor `risk >= MAX_RISK_SCORE (70)` decided 14 and 15. Nothing in the plumbing is broken; what is missing is either the data or an auditor whose scores can cross the threshold. |

Fixing coverage is the cheap lever and it is measurable: add a vendor key, run
`make coverage`, run `make once`, and read the veto mix. If risk scores still never
fall below 70, the ceiling was never going to discriminate at that model size and the
decision is the model, not the keys.

Off-site, either path:

```bash
brew install rclone && rclone config        # interactive Google Drive OAuth, one time
make backup-push                            # BACKUP_REMOTE default; snapshots only, never the live DB
# or set R2_* in .env and let the daemon's own maintenance pass upload it
```

Sleep, either path:

```bash
sudo pmset -c sleep 0 displaysleep 0                    # machine-wide, survives reboots
# or the per-job assertion, no sudo and nothing to remember to revert:
launchctl load ~/Library/LaunchAgents/com.alphasentinel.hold-awake.plist
pmset -g assertions | grep -i caffeinate                # "caffeinate asserting forever"
```

`caffeinate -s` holds system sleep only while the job runs and only on AC power, so
`make mac-uninstall` removes the behaviour completely.

## Reloading a unit after editing its plist

`kickstart -k` restarts a process with the config launchd already loaded; it does **not**
re-read the file. To apply an edited plist you must boot out and bootstrap:

```bash
uid=$(id -u)
launchctl bootout   gui/$uid/com.alphasentinel.watchdog
launchctl bootstrap gui/$uid ~/Library/LaunchAgents/com.alphasentinel.watchdog.plist
launchctl print     gui/$uid/com.alphasentinel.watchdog | grep -E "state|pid"
```

Two traps, both hit for real while writing this. Bootstrapping immediately after a
bootout can fail with `Bootstrap failed: 5: Input/output error` — the label is still
draining, so wait a few seconds and retry, and until it succeeds **that unit is not
running**: supervision being off looks exactly like supervision being quiet. Check
`launchctl list | grep alphasentinel` shows every unit you expect, not just the one you
touched. Then confirm any assertion a unit exists to hold:

```bash
pmset -g assertions | grep -i caffeinate     # "caffeinate asserting forever"
```

## Log files

Everything under `~/Library/Logs/alpha-sentinel/` is bounded by the supervisor's
`--log-file` list and trimmed in place at `--max-log-mb` (20), because launchd holds
the inode and a rename would leave it writing to a file that is no longer there. The
invariant is asserted in `tests/test_launch_units.py`: a unit that adds a log path
without adding it to that list fails the suite. It caught one: the supervisor's own
`watchdog.log` was the only file in the directory nothing trimmed.
