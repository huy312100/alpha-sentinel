# AI code review

CodeRabbit is a hosted reviewer with a monthly quota: when it runs dry, PRs go
unreviewed and the quota does not come back until the billing cycle turns over.
This repo now carries four self-hosted reviewers instead. They run on GitHub's
runners against API keys we own, so the only ceiling is our spend and the
provider's rate limit — and each one posts its own independent opinion.

Definitions live in `.github/workflows/ai-review.yml`. Nothing here is a required
status check: the reviewers advise, `Lint + hermetic tests`, CodeQL and CircleCI
still decide whether a PR can merge.

| Reviewer | Workflow job | Secret | Runs on | Cost |
| --- | --- | --- | --- | --- |
| PR-Agent | `pr-agent` | `DEEPSEEK_API_KEY` | every push to a PR, plus `/review`, `/improve`, `/ask`, `/describe` | ~$0.27 / $1.10 per Mtok in/out (DeepSeek) |
| Claude Code | `claude` | `ANTHROPIC_API_KEY` | PR opened / reopened / ready for review, plus `@claude` | pay per token |
| Gemini CLI | `gemini` | `GEMINI_API_KEY` | every push to a PR | free tier from Google AI Studio |
| Codex | `codex` | `OPENAI_API_KEY` | opt-in only (`ENABLE_CODEX_REVIEW=true`) | pay per token |

PR-Agent is the direct CodeRabbit replacement: it writes the PR summary and the
line-level review, and it answers slash commands. The other three are second
opinions from different model families — a quantity of reviewers is not the same
as a diversity of reviewers.

## Setup (one minute of clicking)

1. **Secrets** — Settings → Secrets and variables → Actions → *New repository
   secret*. Add any subset of:

   | Name | Where to get it |
   | --- | --- |
   | `DEEPSEEK_API_KEY` | <https://platform.deepseek.com/api_keys> |
   | `ANTHROPIC_API_KEY` | <https://console.anthropic.com/settings/keys> |
   | `GEMINI_API_KEY` | <https://aistudio.google.com/apikey> (free tier) |
   | `OPENAI_API_KEY` | <https://platform.openai.com/api-keys> (Codex only) |
   | `OPENROUTER_API_KEY` | only if `PR_AGENT_MODEL` is an `openrouter/*` id |

2. **Variables** (same screen, *Variables* tab) — all optional:

   | Name | Default | Meaning |
   | --- | --- | --- |
   | `PR_AGENT_MODEL` | `deepseek/deepseek-chat` | any LiteLLM model id (see below) |
   | `ENABLE_CODEX_REVIEW` | unset | set to `true` to arm the Codex job |


## Switching model, without touching the workflow

`PR_AGENT_MODEL` takes any LiteLLM model id, and the job resolves the provider
key from the prefix — the only requirement is that the matching secret exists:

| `PR_AGENT_MODEL` | Needs |
| --- | --- |
| `deepseek/deepseek-chat` (default), `deepseek/deepseek-reasoner` | `DEEPSEEK_API_KEY` |
| `anthropic/claude-sonnet-*`, `anthropic/claude-opus-*` | `ANTHROPIC_API_KEY` |
| `openai/gpt-*`, `gpt-*` | `OPENAI_API_KEY` |
| `openrouter/<vendor>/<model>` | `OPENROUTER_API_KEY` |

Any other prefix is not wired to a secret and the job skips on purpose; add the
case to the `Resolve model and provider key` step if you want it.

`config.fallback_models` is deliberately empty: the shipped PR-Agent default
fallback is an OpenAI model, so a DeepSeek outage would otherwise quietly spend
GPT tokens, or hard-fail on a key that does not exist.

## On demand

The `issue_comment` trigger means a review can be re-run without a new push:

| Comment | Reviewer | Effect |
| --- | --- | --- |
| `/review` | PR-Agent | line-level review of the current diff |
| `/describe` | PR-Agent | regenerate the PR summary |
| `/improve` | PR-Agent | concrete code suggestions |
| `/ask <question>` | PR-Agent | Q&A over the PR |
| `@claude <request>` | Claude Code | review, or answer the request directly |

All of them are restricted to `OWNER` / `MEMBER` / `COLLABORATOR` commenters.
Without that gate, anyone could spend the repo's tokens by commenting on a
public PR.

## Fork PRs

Fork PRs are **not** reviewed. `pull_request` withholds secrets from forks, and
every job skips rather than failing — this is deliberate, not a bug.

Do not "fix" it with `pull_request_target`. Three of the four reviewers check out
and read the PR tree; `pull_request_target` would read that tree with secrets in
scope, which is the standard exfiltration path for a malicious fork. If fork
coverage ever matters, run PR-Agent alone under `pull_request_target` (it is
API-only and needs no checkout) and leave the other three on `pull_request`.

## Relationship to CodeRabbit

They do not conflict — they comment independently. Two sane arrangements:

* **Keep both.** CodeRabbit covers the first N PRs of the month, these four cover
  the rest, and when the quota resets you get five opinions.
* **Retire CodeRabbit.** Set `reviews.auto_review.enabled: false` in
  `.coderabbit.yaml` and keep `chat.auto_reply` on, so `@coderabbitai` still
  answers questions but costs no automatic reviews.

`PR_REVIEWER.EXTRA_INSTRUCTIONS` in the workflow carries the same path
priorities as `.coderabbit.yaml` (prompt boundary in `ai_engine/`, veto
precedence in `core/scoring.py`, hermetic tests, secret hygiene in `.github/`),
so switching reviewers does not lower the bar.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Job green, `PR-Agent skipped` notice | no secret matching the `PR_AGENT_MODEL` prefix |
| `litellm` cannot identify the model | provider key variable empty or misnamed; the workflow writes exactly one `<PROVIDER>__KEY` to `$GITHUB_ENV` |
| 402 / rate-limit from the provider | provider quota, not CodeRabbit's — top up or switch `PR_AGENT_MODEL` |
| Claude job posts nothing | `@claude` from a non-member, or the mention did not match `@claude` exactly |
| Gemini job fails on `docker` | the review recipe runs the GitHub MCP server in a container; `ubuntu-latest` has it, self-hosted runners need it |

3. Merge. Reviewers start on the next PR. No key, no run: each job prints a
   `::notice::` and exits 0, so the workflow is green before you configure
   anything and stays green if a key is revoked.
