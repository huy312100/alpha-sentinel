# Release and soak runbook

For whoever is at the terminal when something needs deciding. Every command here was
run at least once; where a number is quoted it was measured, not estimated.

## v0.1.0 — what it is

The first operational baseline: the dual-agent pipeline running unattended under
launchd, with a restart-on-unhealthy supervisor, verified hot-store snapshots, and
health that answers two separate questions (is the loop turning / is it producing).

Verify a release candidate before tagging it:

```bash
make ci                       # lint + 791 hermetic tests + byte-compile + smoke
git push origin main && gh pr checks    # CI is the deployment proof, not local runs
```

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
serve the data the new code wrote". Check that with a worktree, never by moving the
deployment checkout:

```bash
git worktree add ../as-prev <previous-tag>
cd ../as-prev
HOT_DB_PATH=$OLD_STORE_COPY \
  python -m bot.main --status && python -m bot.main --health
```

`--health` on a restored store copied from before the current run **should** report
`cycle_stale`: the restored data really is old. That is the check working, not failing.

Rolling the live daemon back:

```bash
git checkout <previous-tag>
launchctl kickstart -k gui/$(id -u)/com.alphasentinel.bot
```

`SCHEMA_VERSION` is 3 at v0.1.0 and has not moved, so an older build opens a newer
store. If a future release raises it, the rollback above stops being safe and this
section must say so.

## Seven-day soak

Pass conditions, all measured from the store — not from uptime vibes:

```bash
.venv/bin/python -m bot.main --health                # exit 0 throughout
.venv/bin/python -m bot.main --status                # cycles advancing, verdicts landing
sqlite3 data/hot/alpha_sentinel.db "SELECT status, COUNT(*) FROM runs GROUP BY status"
sqlite3 data/hot/alpha_sentinel.db "SELECT key, value_json FROM kv"   # backup + productive markers fresh
ls -1t data/backups | head -1                        # newest snapshot < BACKUP_INTERVAL_SECONDS + slack
```

- no manual restart for the full seven days (a restart resets the watchdog's budget and
  the meaning of "unattended");
- zero `verdicts_stale` / `never_productive` in `--health`, and zero `unhealthy` that
  the supervisor could not explain from a log line;
- `partial` runs only where a real error is recorded — cooldown skips must not appear
  there, that was the point of #37;
- spend ceilings never bind silently: every exhaustion produces an explicit degraded
  verdict, not a missing one. **Not exercisable while `DEMO_MODE=true`** — there is no
  spend to ceiling, so a demo soak proves plumbing and nothing about budget behaviour.

## What has to be true before this is live for real

| gap | why it is not cosmetic |
|---|---|
| `DEMO_MODE=true` | both agent answers are scripted, so every verdict is plumbing evidence. A veto in demo mode is a fixture, and it pages you like a finding. |
| no off-site copy | `R2_ENABLED=false` and no `rclone` — snapshots live on the same disk as the store they protect. Every rehearsal passes and a drive failure still takes both. |
| Telegram unset | `alerts` has 0 rows. The bot has run for days with no way to reach anyone. |
| host sleeps | launchd intervals do not fire during sleep. Measured drift: 70–77 min cycles against a 60 min interval. A soak on a napping machine is not seven days of evidence. |

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
