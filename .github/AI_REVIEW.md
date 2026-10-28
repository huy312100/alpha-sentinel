# AI code review

CodeRabbit is a hosted reviewer behind gates this repo does not control. A
monthly quota that runs out mid-quarter and does not return until the billing
cycle turns over — that is what happened on PR #13. And an open-source
eligibility rule: on PR #14 it declined with *"This repository does not receive
automatic reviews because it has fewer than 10 stars"*, leaving only a
"trigger review" checkbox.

The usual fix is another hosted reviewer, which trades those gates for a
per-token bill and an API key to provision, rotate and keep out of a public repo.

This repo does the opposite. `local-review` in `.github/workflows/ai-review.yml`
runs [PR-Agent](https://github.com/qodo-ai/pr-agent) against a small language
model served by [Ollama](https://ollama.com) **on the runner itself**. The weights
are downloaded once and then cached, the review runs on the runner's CPU, and the
comment is posted with the workflow's own `GITHUB_TOKEN`.

| | |
| --- | --- |
| Reviewer | PR-Agent `0.49.0`, installed as `pr-agent[github]` |
| Model | `qwen2.5-coder:7b-instruct-q4_K_M`, or any Ollama tag |
| Key / secret | **none** |
| Cost | **$0** — runner minutes are free for public repositories |
| Runs on | `ubuntu-latest` (4 vCPU / 16 GB), Docker + Python 3.12 |
| Time per review | ~13–16 min for a full-budget diff, less for small ones (see *the two numbers*) |

Nothing here is a required status check: the reviewer advises, and
`Lint + hermetic tests`, CodeQL and CircleCI still decide whether a PR can merge.

## What it is and is not

A 7B model on four CPU cores is not a frontier model over the network, and
pretending otherwise is how teams learn to ignore bot comments.

It is good at the mechanical class of defect that survives a tired review:
unvalidated input at a trust boundary, an exception swallowed so a cycle
"continues" with stale state, a secret or an API key on a log line, a test that
reaches the network, an obvious logic slip inside the changed hunk.

It is not good at cross-file invariants, ordering bugs that need three files in
view at once, or judging whether an abstraction is the right one. It also only
sees what fits the context window (see *the two numbers* below).

So: treat a finding as a prompt to look, not as a verdict, and treat silence as
silence rather than approval.

## Setup

There is nothing to configure, and nothing to add under *Settings → Secrets*.
The workflow is green from the first run.

Two repository variables are optional (*Settings → Secrets and variables →
Actions → Variables*):

| Variable | Default | Meaning |
| --- | --- | --- |
| `AI_REVIEW_MODEL` | `qwen2.5-coder:7b-instruct-q4_K_M` | any Ollama model id |
| `AI_REVIEW_ENABLED` | unset | `false` disables the workflow; anything else leaves it on |

A PR labelled **`skip-ai-review`** is not reviewed — useful for a revert of a
generated file, or for a PR that is only moving fixtures.

## Choosing a model

Set `AI_REVIEW_MODEL` to any tag from the [Ollama library](https://ollama.com/library).
Measured against the standard 4 vCPU runner:

| Tag | Disk | Review time | Notes |
| --- | --- | --- | --- |
| `qwen2.5-coder:7b-instruct-q4_K_M` | 4.7 GB | ~12–15 min | default; best balance here |
| `qwen2.5-coder:3b-instruct-q4_K_M` | 1.9 GB | ~6–8 min | roughly twice the prefill speed, noticeably shallower |
| `qwen2.5-coder:14b-instruct-q4_K_M` | 9.0 GB | ~25 min+ | better, but close to the 16 GB limit and to the job timeout |
| `gpt-oss:20b` | 13 GB | — | needs more than 16 GB RAM; it will thrash or be OOM-killed |

Timings are the whole job on a standard runner with the weights already cached and
a full 10240-token budget. Small PRs are much faster — the cost is dominated by
prompt length divided by prefill speed, not by the number of findings.

Two constraints worth knowing before switching:

* **RAM.** Model plus KV cache has to fit in 16 GB alongside the OS. The 7B Q4
  file is ~4.7 GB and a 16k context adds roughly 1 GB of cache.
* **Time.** Generation is CPU-bound. `timeout-minutes: 30` in the workflow is
  the ceiling, and a cold run also spends ~2 min downloading weights.

Changing the model changes the cache key, so the first run after a switch pays
the download again.

## The two numbers that decide whether the review is honest

Both live in the workflow's `env:` block and they are a pair:

* `PR_AGENT_MAX_TOKENS: 10240` — the budget PR-Agent is willing to send. A local
  model is not in PR-Agent's own token table, so the run aborts unless
  `CONFIG__CUSTOM_MODEL_MAX_TOKENS` is set. Past the budget, PR-Agent prunes the
  diff to the hunks it thinks matter most **and names the files it dropped** at
  the bottom of the comment — read that line.
* `OLLAMA_CONTEXT_LENGTH: 16384` — the model's actual window. Ollama sizes its
  default from available VRAM, which on a CPU-only runner means **4k**. A prompt
  longer than the window is truncated *silently*: the model then reviews the tail
  of the diff and reports confidently on the rest.

The window must clear the send budget, not merely the size of the diff.

Why 10240 and not more: **this model reads a prompt at ~13.7 tokens/second on a
4 vCPU runner** (measured — Ollama's own `prompt eval time`, 73 ms/token). A
budget of 10240 is therefore ~12 minutes of prefill before the first output
token, and `CONFIG__AI_TIMEOUT` is set to 1500s to sit above it. 16384 of budget
(≈20 minutes of prefill) timed out in practice; 8192 was fast and, on the PR that
introduced this workflow, pruned away both files the PR changed while still
reporting "no major issues detected".

If you raise `PR_AGENT_MAX_TOKENS`, raise `OLLAMA_CONTEXT_LENGTH` above it, raise
`CONFIG__AI_TIMEOUT` above the new prefill time, and check that
`timeout-minutes: 60` still leaves room for a retry. The arithmetic is
`budget / 13.7` seconds; a bigger runner moves the 13.7, not the budget.

## On demand

The `issue_comment` trigger re-runs a review without a new push:

| Comment | Effect |
| --- | --- |
| `/review` | line-level review of the current diff |
| `/describe` | regenerate the PR title and summary |
| `/improve` | concrete code suggestions |
| `/ask <question>` | question about the PR; the question must be on one line |

All four are restricted to `OWNER` / `MEMBER` / `COLLABORATOR` commenters.
Without that gate anyone could drive a review by commenting on a public PR,
spending runner minutes on someone else's behalf.

Arguments after the command are ignored — the workflow maps `/review` to
`review`, not to a free-form PR-Agent argument string. To change review settings,
edit the workflow.

## Drafts, forks and other skips

Every skip prints a `::notice::` and leaves the job green. A reviewer that is not
applicable must never be able to block a merge.

* **Draft PRs** are skipped until they are marked ready.
* **Fork PRs are skipped.** On `pull_request`, GitHub hands a fork workflow a
  read-only `GITHUB_TOKEN`, so the review could be computed (the model is local)
  but never posted. This is deliberate, not a bug.
* **Non-command comments**, comments on plain issues, and commenters outside the
  three trusted associations are skipped.

Do not "fix" fork coverage with `pull_request_target`. It would run this job
against an attacker-controlled tree with a write token in scope. If fork coverage
ever matters, run PR-Agent alone there — it is API-only and never checks out or
executes the PR's code — and leave everything else on `pull_request`.

## Relationship to CodeRabbit

They do not conflict; they comment independently. Two sane arrangements:

* **Keep both.** CodeRabbit covers the first N PRs of the month, this covers the
  rest, and when the quota resets you get two opinions.
* **Retire CodeRabbit.** Set `reviews.auto_review.enabled: false` in
  `.coderabbit.yaml` and keep `chat.auto_reply` on, so `@coderabbitai` still
  answers questions but costs no automatic reviews.

`PR_REVIEWER__EXTRA_INSTRUCTIONS` in the workflow carries the same path
priorities as `.coderabbit.yaml` (prompt boundary in `ai_engine/`, veto
precedence in `core/scoring.py`, hermetic tests, secret hygiene in `.github/`),
so switching reviewer does not lower the bar.

## If you ever want a hosted model anyway

Nothing in this repo requires a key, and this file deliberately does not list
one. Two facts for the day it changes:

* The four bring-your-own-key jobs (PR-Agent on DeepSeek, Claude Code, Gemini
  CLI, Codex) exist in git history — see PR #13 and its parent commit. They can
  be restored job by job; each skips itself when its key is absent.
* Google AI Studio's Gemini free tier is genuinely free of charge but still needs
  an API key, which is precisely what this setup avoids.

Keep the local job either way: it is the one that works when a card expires, a
key leaks, or the quota resets at 3 a.m. on a Sunday.

## How it is wired

Each of these was a silent failure once, so each has a guard:

| Detail | Why it matters |
| --- | --- |
| `pr-agent doctor` runs before the review | fails in seconds if the model id is wrong, the server is not up, or a completion cannot be produced — instead of after five minutes of CPU |
| `pr-agent[github]`, not `pr-agent` | PyGithub is an optional extra; without it the provider cannot import and nothing is posted |
| Python 3.12 in this job only | PR-Agent 0.49 declares `requires-python >=3.12`; the app itself still runs on 3.11 |
| `CONFIG__FALLBACK_MODELS: "[]"` | PR-Agent's shipped fallback is a hosted OpenAI model, which would bill us or fail on a key we do not have |
| `GITHUB__USER_TOKEN: ${{ github.token }}` | PR-Agent reads its token from settings (`envvar_prefix=False`, so `SECTION__KEY` works); the CLI has no implicit `GITHUB_TOKEN` fallback |
| `PR_AGENT_VERSION` pinned | the review is parsed as structured JSON, so an upstream format change would degrade to "no findings" rather than an error |
| Model weights cached by tag | `~/.ollama/models`, keyed on the model id; the weights never change for a tag |
| `OLLAMA_NUM_PARALLEL=1`, `MAX_LOADED_MODELS=1` | keeps a 16 GB runner from loading two copies during a fast push/force-push pair |
| `CONFIG__AI_TIMEOUT: "600"` | litellm times out at 120s, and preflighting a full-budget prompt on CPU takes longer; the default produced silent timeouts and a green check |
| `CONFIG__PROPAGATE_TOOL_ERRORS: "true"` | PR-Agent exits 0 when a review fails, so without this a broken model leaves "Failed to review PR" as a comment and a green tick beside it |
| `sudo chmod -R a+rX ~/.ollama` as the last step | the container runs as root and Ollama writes manifests `0600`, so the cache post-step (runner user) hit "Permission denied" and silently never saved the 4.7 GB |
| `concurrency` keyed on event **and** sender type | a force-push cancels the previous push review, but bot chatter cannot cancel anything: CodeRabbit's comment and PR-Agent's own progress comment both arrive as `issue_comment`, and on PR #14 the first of them cancelled the running review |

PR-Agent talks to GitHub over the API and works from the diff it fetches; this
job does not check out the PR tree at all. That is why the wall-clock cost is
CPU, and why a malicious PR cannot get its own code executed by the reviewer.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Job green, `::notice::` says skipped | expected: draft, fork, wrong label, disabled by variable, or a non-command comment — the notice says which |
| `no API key for 'ollama'` from `doctor` | `OLLAMA_API_KEY` was dropped from the step; Ollama needs no real key, but PR-Agent insists a value exists |
| `model completion failed: ... Connection` | the Ollama container is not up or `OLLAMA_API_BASE` does not point at it — the job dumps `docker logs ollama` on failure |
| `is not defined in MAX_TOKENS ... set custom_model_max_tokens` | `CONFIG__CUSTOM_MODEL_MAX_TOKENS` missing; a local model is not in PR-Agent's table |
| `ModuleNotFoundError: No module named 'github'` | installed as `pr-agent` instead of `pr-agent[github]` |
| `requires-python >=3.12` at install | the job's Python pin was lost |
| Review ignores the second half of a big PR | context window smaller than the diff: raise `OLLAMA_CONTEXT_LENGTH` (and keep it above `PR_AGENT_MAX_TOKENS`) |
| Review takes the whole job timeout and is killed | the model is too large for the runner, or `CONFIG__AI_TIMEOUT` was raised without raising `timeout-minutes` |
| `Failed to review PR` on the PR but the job is green | `CONFIG__PROPAGATE_TOOL_ERRORS` was dropped; the job now fails with the same message |
| Comment posted but empty of findings | the model answered outside the JSON shape; try a stronger tag, and check the step log for a parse warning |
| Every run re-downloads 4.7 GB | cache miss: cached weights live in `~/.ollama/models`, keyed on the model id, and the cache step needs the `chmod` step above to be able to read them |
