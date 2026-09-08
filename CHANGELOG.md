# Changelog

## Unreleased

- **Operator console.** The local dashboard now opens on a bounded Overview and navigates through
  Overview, Tasks, Workers, Subscriptions, and Usage. It adds responsive keyboard-accessible
  navigation, a `Ctrl+K`/`Cmd+K` task finder, dark/light/system themes, and packaged local modules
  without a CDN. The old `#/providers` location resolves to Workers.
- **Cached-only dashboard diagnostics.** `GET /api/overview` reports global counts plus bounded
  active and attention lists. Overview and Workers start no provider processes: Workers projects
  cached `native_check` and doctor data, and an absent doctor cache is shown as not run. Explicit
  `/api/doctor` and `/api/doctor?live=1` requests retain their existing diagnostic behavior.
- **Native Grok quota check.** `capabilities(check_providers=["grok"])` and
  `taskspindle providers --check --provider grok` run a session-free native ACP billing check and
  cache its normalized result for five minutes. The installed Grok 1.0.13 path returned a valid
  weekly quota/reset observation in live validation; private values stay in the local evidence
  ledger. No inference, login, browser, direct HTTP, token read, or CLI upgrade is used.
- **Conservative quota gating.** Only a fresh explicit exhausted native quota adds a temporary
  new-task gate. Passed resets, stale observations, unsupported methods, and unknown quota permit
  an ordinary attempt unless task-derived account/model evidence still refuses it. The check never
  selects a provider, moves a task, clears a refusal, or enables a metered fallback.
- **Scoped Grok refusals.** Grok account state now uses terminal structured provider HTTP status,
  plus source-proven terminal xAI retry evidence. Incidental tool errors and unrelated 403 responses
  stay unclassified; stored retry diagnostics exclude provider prose and URLs.
- **Honest MCP annotations.** Default `capabilities()` remains cached-only. The tool is no longer
  marked read-only because its optional provider check writes only the bounded native diagnostic
  cache; task records and provider refusal evidence are unchanged.

## v0.2.0

Adds subscription visibility, subscription-aware worker eligibility, native Antigravity support,
and broader task inspection while keeping provider choice and task ownership explicit.

- **Subscription tracking.** The dashboard can connect a selected normal Chrome profile through
  the Playwright extension and record normalized, account-bound billing observations for ChatGPT,
  Claude, Grok, and Google AI. Credentials, payment details, raw provider payloads, and raw account
  identities are not stored. Unsupported billing channels and unverified dates remain explicit.
- **Lightweight billing follow-up.** Scheduled collection is opt-in and disabled by default.
  Manual Connect and Refresh remain available; fresh confirmed access-end observations expose
  normalized seven-day and one-day warnings without changing worker routing.
- **Subscription-aware worker eligibility.** Account and model refusals, authentication expiry,
  access denial, and elapsed quota resets are projected consistently for coordinators. Successful
  use and stale evidence are recorded without clearing newer sibling failures. TaskSpindle does not
  migrate an existing task or infer CLI eligibility from browser billing dates.

- **Search and compare.** Dashboard search matches literal task ID/prompt text, including
  Unicode case folding. Candidate diffs sit alongside their reviews, finding links retain focus
  through polling, and stale candidate requests are rejected rather than paired with a new diff.
- **Repository labels.** Usage summaries display registered paths while retaining stable IDs
  in JSON and using an ID fallback when a path is unavailable.
- **Discovery probes.** `discover --probe` optionally initializes installed ACP agents without
  authenticating or generating a turn. Per-agent results include useful timeout errors; a failed
  probe does not prevent the remaining probes from running.
- **Continuation verification.** Grok continuation succeeded with per-turn usage in the recorded
  three-turn verification. The earlier intermittent direct ACP reload timeout is historical
  evidence rather than an active release blocker; continuation still uses a fresh unit and the
  stored session, with no automatic replacement conversation.

- **Grok read-only turns use the `read-only` sandbox.** `strict` allowed writes inside the
  worktree and, on WSL, denied the `/etc/resolv.conf` symlink target so the sandboxed agent's
  DNS fell back to `127.0.0.1`: startup settings/catalog fetches always failed (about 45 s of
  startup) and a resumed session's first model request failed about half the time with
  `TURN_TIMEOUT`. `read-only` reads everywhere and kernel-denies every write outside `~/.grok`
  and the temp dirs, the worktree included.
- **Timed-out turns report transport retries.** Grok's `retry_state` updates are kept as a
  bounded, sanitized summary; a `TURN_TIMEOUT` carries the retry count and the last retry's
  attempt, kind and reason so a transport stall is distinguishable from a slow model.
- **Native Antigravity.** Reserved OAuth provider `agy` uses a separately pinned native CLI
  1.1.26 and the existing personal Google login. `setup --provider agy` installs code only;
  `auth agy` checks cached authentication without another ACP browser sign-in. Private CLI
  state, masked inherited controls and filesystem isolation enforce consult/review read-only
  access and declared implementation scopes. Shell commands run only through TaskSpindle's
  external verification step. New tasks select the newest advertised Gemini release, preferring
  Flash and Medium effort, and retain that exact selection and conversation on continuation.
  Observed tool violations stop the worker; cancellation and failures retain partial results.
  Native cumulative usage is converted to per-turn deltas without replaying prior charges.
- **Review independence rechecked.** Any different built-in provider can review a candidate;
  aliases of the author's family are refused. Each new task records an immutable provider family.
  Review, acceptance, manual integration and continuation compare that history with current
  configuration. Old reserved Claude/Grok IDs remain usable; historical configured tasks with
  unknown family fail closed instead of treating today's alias settings as historical evidence.
- **Partial ACP failures retained.** Cancellation, timeouts and failed turns retain partial
  responses, usage and session identifiers. A failed continuation never creates a replacement
  conversation. Database schema 3 adds resolved model, effort and provider family; retain a
  consistent schema-2 backup before activating this code for rollback to an older runtime.
- **Adapter-side delegation denial.** Initial canonical delegation tool calls now produce
  `DELEGATION_ATTEMPT` even when the Claude adapter refuses them before requesting client
  permission. A live Claude implement on adapter 0.70.0 attempted the disabled `Task` tool,
  received its denial, and recorded the warning without creating a subagent; its small candidate
  passed an independent Grok review and was accepted in a throwaway repository.

- **Grok model attribution.** New turns prefer the wire model ID, then the backend `modelUsage`
  key, then the profile model; attribution and usage agree while raw telemetry stays intact.
- **Usage by repository.** `repository_id` grouping is available in MCP, the CLI and dashboard,
  including a null group for repository-free tasks; no migration is needed.
- **Recovery choices.** `continue_task` can ask form-capable MCP clients to retry or cancel
  ambiguous recovery, while preserving recovery and stale-version checks and the existing error fallback.
- **Claude model metadata.** Canonical model ids returned by ACP session creation or loading
  are preferred over the session file. Aliases and display names retain the existing file fallback.
- **Provider availability, reported rather than acted on.** A turn a provider refuses for a
  usage, rate, credit or login reason is classified (`PROVIDER_THROTTLED`, `PROVIDER_AUTH_EXPIRED`)
  with the window and reset time the provider gave, recorded on the task, as a `PROVIDER_LIMIT`
  event and in a new `provider_status` table, and surfaced in `capabilities`, in an advisory
  `doctor` check, and as a `PROVIDER_UNAVAILABLE` refusal of the next `start_task` on that
  provider (`ignore_provider_status` overrides it). Nothing is retried elsewhere: the rule that a
  provider is never substituted is unchanged.
- **Usage.** Token counts are captured per turn from the agents themselves — the Claude adapter's
  prompt response and Grok's `turn_completed` update — into a new `turn_usage` table, with an
  estimated cost at published rates that is labelled an estimate, and the usage windows the Claude
  adapter reports into `provider_windows`. New read-only tool `usage_report`, new command
  `taskspindle usage`, and `task_result` now carries `usage`, `warnings` and structured
  `quota_warnings`.
- **Dashboard.** `taskspindle web` serves a local page for tasks, timelines, transcripts, diffs,
  reviews, provider availability, usage, and subscriptions. The task database is opened read-only;
  the only mutation endpoints are loopback/same-origin/CSRF-protected requests that queue Connect
  or Refresh work in the separate subscription database.
- **Attribution fixed.** `reported_model`, `gateway_host` and the adapter's `agent` name and
  version are now filled in; they were always null before.
- **`task_diff` defaults to 16384 bytes** (the maximum stays 262144), because an MCP client
  truncates tool output at its own token limit and a receipt for a truncated page proved nothing.
- **Orphans can be cleaned.** `cleanup_task` with `force` removes the worktree of a task whose
  repository no longer exists, and `revoke_repository` accepts a `repository_id` for a repository
  that no longer has a path.
- **Read-only turns are enforced by the agents themselves.** A Claude worker is put in the
  adapter's `plan` session mode for a consult or a review and in `default` mode for an implement,
  so the permission gate is consulted instead of the operator's own `bypassPermissions` setting; a
  Grok consult or review runs inside the CLI's `--sandbox read-only` mode, which reads the workspace
  and kernel-denies writes.
  A request to switch mode is refused and recorded as `MODE_SWITCH_ATTEMPT`; an agent that refuses
  the mode fails the turn with `MODE_UNAVAILABLE`.
- **Reviewers are shown the diff.** A review prompt carries the candidate's recorded diff (or the
  snapshot's `git diff`), cut at 96 KiB, so a reviewer that cannot run git still sees the change.
- **`taskspindle discover`** lists the ACP community registry's agents that are installed here and
  prints a `[providers.<id>]` proposal for each; nothing is downloaded, run or written.
- Schema version 2. The migration is applied on the next open.

## v0.1.0

First release.

- **MCP server** (`taskspindle mcp`): sixteen tools over stdio, one envelope for success and
  failure alike, stable error codes, honest `readOnlyHint` annotations, and tracebacks written to
  the state directory rather than into the conversation.
- **Three modes**: `consult`, `review` and `implement`, each in its own detached git worktree.
  Candidate commits live under `refs/taskspindle/<task_id>/rev/<n>` and never on a branch you use.
- **Two first-class providers**, both OAuth-only: `claude` through the pinned
  `@agentclientprotocol/claude-agent-acp` 0.70.0 adapter, and `grok` through the native Grok CLI
  1.0.13 ACP endpoint. Both are launched with delegation, MCP servers and external settings
  disabled.
- **Second-class configured profiles** from `config.toml`: any ACP stdio agent, including an
  API-key or LiteLLM-gateway harness. Never a default, never a fallback, gated behind
  `allow_metered`, with attribution recorded on every task.
- **Acceptance you have to earn**: the whole diff retrieved and receipted, an independent
  reviewer's verdict on that exact candidate, an explicit override for every blocking or critical
  finding, verification passing in the worktree and again in the root repository, and a
  `git merge-tree` probe before anything is applied. Every step is journalled, so an interrupted
  acceptance is recognisable and reversible.
- **Durable workers**: one turn per transient systemd user unit, heartbeats in SQLite, `SIGTERM`
  to ACP `session/cancel` for cancellation, and an explicit `session/load` for every continuation.
- **Recovery that retains work**: a vanished worker leaves an `INTERRUPTED` task with its worktree,
  session and candidate intact; a situation that cannot be settled becomes `RECOVERY_AMBIGUOUS`
  and waits for a person rather than guessing.
- **Violation reporting**: `SCOPE_VIOLATION`, `READ_ONLY_VIOLATION`, `ROOT_MUTATION` and
  `DELEGATION_ATTEMPT` are recorded and block acceptance until resolved or acknowledged.
- **`taskspindle setup`** installs the pinned adapter from a shipped lock file with
  `npm ci --ignore-scripts`, in an environment holding only `PATH`, `HOME` and `LANG`. It never
  logs in, never copies a credential and never edits Codex's configuration.
- **`taskspindle doctor`** checks git, the systemd user manager, a transient unit, Node, the pinned
  adapter, both provider logins, every profile's command and child environment, and the Codex
  registration — one question at a time, so a fresh machine gets the whole list.
- **Documentation**: install, Codex registration, the tool reference, configuration, platforms,
  architecture, recovery and rollback.

Supported on Linux and WSL2 with a systemd user manager. Python 3.12+, Node 22+, git 2.38+.
