# Changelog

## v0.6.2

- **`task_result` and `task_status` hand over the worker's session.** Both now carry
  `session_id` and a `resume` handle: for a Claude or Grok task the native `claude --resume <id>`
  or `grok -r <id>` invocation plus the directory and environment to run it from, so a human can
  pick up a near-miss candidate by hand instead of rejecting and re-dispatching; for an
  Antigravity task, or an unknown family, a note explaining why no shell command exists. The
  handle follows the task's recorded provider family, never the live profile.
- **Reviews have a kind.** `start_task` takes `review_kind` (`standard`, the default, or
  `adversarial`) on a review task. An adversarial review briefs the reviewer to assume the change
  is wrong and hunt for defects, security holes and untested claims; the output contract, the
  parser and the acceptance rules are unchanged. The kind is recorded on the task, its review row
  and the `REVIEW_RECORDED` event, and shown on the dashboard. Schema 13 adds
  `tasks.review_kind` and `reviews.kind`; rows written before it read as `standard`. The shared
  review rules now also tell the reviewer to stay inside the checkout and not follow `.git`'s
  gitdir pointer: an adversarial Antigravity reviewer did exactly that, hit the sandbox's
  refusal, and ended its turn with no output (`REVIEW_MALFORMED`).
- **A coordinator can hand files to a worker.** `start_task` takes `context_files`, absolute
  paths the server reads under a new opt-in `[context_files]` allowlist and appends to the first
  turn, each framed with its path and size under a header that marks it as reference material.
  The text is kept as the task's `context_files` artifact and the paths on the task. Reads
  refuse symlinks, hard links, non-regular files, binary content and anything over the
  configured caps, before any task row exists. Without the table the request is refused with
  `CONTEXT_FILES_DISABLED`. Schema 14 adds `tasks.context_files`.

## v0.6.1

- **A turn that reported no usage says so.** The worker log names the provider and revision
  instead of leaving an unexplained gap in the usage report, and a store failure while recording
  usage is logged rather than swallowed.
- **Fix: an OAuth token refresh no longer looks like a different login.** `auth_context.fingerprint`
  now hashes only a credential file's resolved location and identity (device and inode), not its
  size or timestamps, so a routine token refresh (which rewrites the file in place) no longer
  changes the fingerprint. This stops a task queued before a refresh and dispatched after it from
  dying with a non-retryable `AUTH_CONTEXT_CHANGED`. The fingerprint format gains a version prefix
  (`"2:<64 hex>"`); a stored value from a retired version is treated as unrecorded rather than
  compared, since `set_task_auth_context` is immutable and cannot be re-stamped. `locator_digest` is
  removed; `validate_contexts` now uses `fingerprint` directly.
- **`taskspindle reprice` brings stored usage rows back in line with the current price table.** It
  re-runs `estimate_cost` over the counts already on each `turn_usage` row and writes back only
  `cost_estimate_usd` and `price_table_version`, leaving `captured_at`, `raw` and `source` alone;
  `--dry-run`, `--since` and `--provider` all apply. A row with no model recorded (typically
  Antigravity, whose picker ID is not attribution) is reported as unpriced unless the new
  `--use-selected-model` flag opts in to pricing it from the task's `resolved_model`, without
  writing that model as attribution.
- **Cost estimates cover all three providers.** The price table gains `grok-4.6` and the Gemini
  models Antigravity selects (`gemini-3.1-pro`, `gemini-3.8-flash`), each carrying the date its
  rates were read from the vendor's own pricing page in `price_table_version`. A Grok turn's
  estimate reproduces Grok's own reported `costUsdTicks` exactly on real turns; that raw figure is
  still never converted. An Antigravity turn is priced as the model the task selected, while
  `reported_model` keeps recording only what the backend itself said. Both vendors bill double
  above a 200k-token prompt, so an estimate for a long turn is a floor.

## v0.6.0

- **Provider refusal, quota, recovery, and native-overage machinery collapses to one rule.**
  `provider_status` (per provider: `ok | throttled | auth_expired | access_denied |
  model_unavailable`, `reset_at`, `observed_at`, `reason`, `source`, `last_success_at`) is written
  by `limits.classify_acp_error` and `runner._record_provider_limit` after a refused turn, and
  cleared by a successful one. `start_task` refuses a provider that is not `ok`
  (`PROVIDER_UNAVAILABLE`) until the provider's own `reset_at`, or — when it gave none — fifteen
  minutes after the refusal was observed; after that it is simply eligible again, one ordinary
  attempt. `start_task` gains an explicit coordinator override, `ignore_provider_status: bool =
  false`, replacing the retired `LEGACY_OVERRIDE_RETIRED` flag of the same name.
  `capabilities.providers[].availability` is now exactly `{state, reset_at, eligible_at, reason}`.
  A model-scoped refusal collapses into the same provider-level status, with the model named in
  `reason`, instead of a separate per-model row.
- **Deleted entirely:** manual recovery permits (`provider_recovery.py`, the `provider_recovery`
  MCP tool), hybrid recovery (`hybrid_recovery.py`, its cooldown/trial/held states and
  `[provider_recovery]` config), quota windows/restrictions/retries (`quota.py`,
  `QUOTA_RETRY_PENDING`), and native extra usage (`native_overage.py`, `[native_overage]` config,
  the `native_overage` blocks in `task_status`/`task_result`/`capabilities`, and the quota-failed
  `continue_task` continuation path — a `FAILED` task can no longer be continued; start a new task
  once its provider is eligible again). `provider_model_status` and `provider_auth_context` are
  gone along with them. Native checks (`access_checks`, `doctor`, `capabilities(check_providers=
  [...])`) are unchanged: they remain the on-demand, five-minute-cached "is this CLI logged in"
  probe and never gate admission.
- **Schema 12** drops the eleven now-dead tables (`provider_model_status`,
  `provider_recovery_permits`, `provider_auth_context`, `provider_quota_restrictions`,
  `provider_quota_retry_attempts`, `native_overage_attempts`, `native_overage_observations`,
  `recovery_episodes`, `recovery_claims`, `recovery_native_semantics`, `recovery_evidence`) and
  adds `tasks.ignore_provider_status` and `provider_status.affected_model`. `provider_status`,
  `provider_windows`, tasks, and grants are unaffected; the data in the dropped tables was disposable
  intermediate state, not the fact it led to. `[provider_recovery]` and `[native_overage]` in
  `config.toml` are now accepted and ignored, with one warning line, so an existing config file
  still loads.
- The MCP server now exposes eighteen tools (`provider_recovery` removed). The CLI's
  `providers --retry-next`/`--revoke-retry` flags and the `LEGACY_OVERRIDE_RETIRED` code are gone;
  `providers` reports the same four-field availability. The dashboard's Workers page shows
  provider status, reset/eligible time, and the last native check in place of the old quota/
  recovery projections; the Policy page's file-managed panel drops the retired `[native_overage]`/
  `[provider_recovery]` rows.

## v0.5.2

- **Vendor versions are minimums, not pins.** Antigravity runs from PATH (or
  `TASKSPINDLE_AGY_SOURCE`) and must be at least `AGY_MIN_VERSION`; builds newer than
  `AGY_TESTED_MAX` are advisory. The private CLI copy and its digest are gone. The Claude ACP
  adapter's setup and doctor checks accept an installed version at or above the pin and no
  longer reject a symlinked launcher that resolves to an executable. A new vendor release is a
  one-line constant change instead of an outage.

- **Acceptance gates that are not about repository safety are opt-in, off by default.**
  `accept_task` and `record_integration` keep the merge-tree probe, the integration journal, the
  `path_prefixes` scope check and `state_version` mandatory, but retrieving the whole diff
  (`require_diff_receipts`), an independent review with every blocking finding disposed
  (`require_review`), rerunning verification in the root instead of trusting the worker's own
  `check_summary` (`rerun_verification`), and refusing an unacknowledged `ROOT_MUTATION`
  (`require_root_stability`) are now each their own flag, all defaulting to `false`. A named
  `review_task_id` is still always checked as bound to the candidate and independent of its
  author, whether or not `require_review` asked for one; its undisposed blocking findings become
  a warning instead of a refusal when it did not.
- **A sibling task's own accept no longer looks like a root mutation.** `ROOT_MUTATION` no longer
  fires for a root HEAD move that consists entirely of commits TaskSpindle itself landed in that
  repository (an accepted task's `target_head`), bounded to a 50-commit chain; an operator's own
  commit, a reset, or a longer or unresolvable chain is still reported. Working-tree dirtiness is
  unaffected.

## v0.5.1

Fixes from the 2026-09-15 audit (`docs/audit-2026-09-15.md`) and the provider readiness hotfix.

- **Read-only tools are read-only.** `list_tasks`, `task_status`, `task_result`, `usage_report`
  and `list_repository_policies` reconcile but never dispatch, and a tool's own error is no
  longer replaced by a dispatch failure.
- **`state_version` moves only on transitions.** Unit assignment, cleanup state and the
  worker's own bookkeeping no longer bump it, so a version read from `task_status` stays valid
  for `cancel_task` and `continue_task`.
- **Interrupted tasks resume.** A failed worker unit left by a self-settled interrupt is reset
  before the task is relaunched instead of colliding with `UNIT_START_FAILED`.
- **Failures say why.** A spawn or handshake failure carries a redacted tail of the agent's
  stderr and uses its first line as the message; an authentication-context change during a
  refusal is recorded beside the refusal instead of replacing it. `doctor` warns when a Grok
  hook source is a symlink, which Grok refuses.
- **Worker liveness.** The worker writes `tasks/<id>/progress.json` (phase and monotonic
  counters) and `task_status` reports it with `elapsed_s`, `heartbeat_age_s` and
  `progress_age_s`, so a coordinator can tell a stuck worker from a slow one.
- **Stricter permission gate.** A consult or review turn may use only the `read`, `fetch`,
  `search` and `think` tool kinds; every other kind, including an adapter's unclassified
  `other`, is refused. Delegation is recognised by tool name or by plain words, never by a file
  name such as `agent.py`.
- **Dashboard hardening.** Every route requires a loopback `Host` (DNS rebinding is refused;
  a deliberate `--host 0.0.0.0` bind still works), and responses carry `nosniff`,
  `no-referrer`, `DENY` framing and, for the page, a nonce-scoped content security policy.
- **Secrets off argv.** Worker environment variables that are not public go to a 0600
  `EnvironmentFile` in the task directory instead of `systemd-run --setenv`, and
  `cleanup_task` removes it.
- **Provider readiness.** Grok's version label is informational and readiness is probed through
  its real read-only sandbox launch; Claude authentication is read with `claude auth status
  --json`.
- **Housekeeping.** The smoke test derives its tool count from the server; the dashboard's
  JavaScript tests run in CI; stale `@v0.1.0`/`@v0.2.0` install pins are gone; `usage
  --group-by role` renders; `[provider_recovery]` and `[ai_policy]` appear in the example
  configuration.

## v0.5.0

- **Dispatch policy.** A new `dispatch_policy` document (schema 11, with history) lets the
  operator set target shares, budgets with optional enforcement, per-role provider preference
  and model/effort selections, a pause per provider, and the advertised model/effort value lists.
  It is read fresh on every `capabilities()` call, every `dispatch_policy` call and every
  `start_task` admission — an edit takes effect without restarting the MCP server.
- **Dashboard Policy page.** The dashboard's first write path: loopback peer and `Host`,
  same-origin `Origin`, a per-process `X-TaskSpindle-CSRF` header, a bounded JSON body, and a
  SQLite authorizer that limits the write connection to the two policy tables, so task tables
  stay read-only.
- **`dispatch_policy` tool and `capabilities` blocks.** A new read-only `dispatch_policy` tool
  (`get`/`status`) and a `dispatch_policy` block plus per-provider `policy` block in
  `capabilities()` project the policy and observed status to Codex.
- **`role` on `start_task` and `usage_report(group_by="role")`.** `start_task` accepts an
  optional `role`, recorded on the task for reporting; `usage_report` can roll usage up by
  provider and role.
- **`POLICY_BUDGET_EXHAUSTED`.** `start_task` refuses (retryably) admission on a provider whose
  enforced budget is exhausted, until the window rolls.
- **`taskspindle policy` CLI.** `show`, `export`, `import`, `set`, and `reset`, against the same
  document and revision the dashboard and MCP tool read.
- **Upgrade note.** The runtime directory is versioned by the package version, so re-run
  `taskspindle setup` after upgrading and restart `taskspindle web`.

## v0.4.0

- **Browser billing retired.** The subscription collector, browser helper, CLI commands,
  dashboard page, and mutation APIs are removed. Historical browser data and profiles are left
  inert. Native CLI quota, billing, overage, Workers, tasks, and usage telemetry remain available;
  the old `#/subscriptions` location now opens Workers.
- **Native extra usage.** Exact-profile `provider_managed` opt-in permits bounded native
  OAuth extra usage under provider account controls; `observe_only` remains the default.
  API-key authorization stays separate. No provider switching or billing-setting changes.
- **Quota-failed continuation.** Eligible tasks resume their original session, model and
  retained work with atomic account-scoped admission, fresh authorization checks and durable
  refusal evidence. Schema 9 preserves historical billing as unknown.
- **Usage visibility.** Workers and Usage expose policy, admission, native billing observations
  and classification separately from estimated token costs. Live extra-usage billing remains
  unqualified; see `docs/native-overage.md` for activation and qualification guidance.
- **Explicit model and effort selection.** Built-in Claude and Grok task selections are
  forwarded and confirmed after opening or restoring sessions, including bounded Grok
  configuration transitions.

- **Controlled worker recovery.** Codex and the CLI can arm one explicit "Retry next task"
  permit against the current account/model refusal evidence. Schema 7 records a single active
  permit per shared provider availability key, with a 24-hour deadline, revocation, permanent
  task association, and outcome history. Arming and revoking run no provider checks or prompts.
- **Bounded initial attempts.** Task creation validates grants, provider/model scope and review
  independence before atomically consuming the permit. Preparation or startup failure consumes
  it. Dispatch and initial prompt admission reject changed refusals and expired permits; future
  quota restrictions with a reported future reset and fresh native exhaustion cannot be bypassed.
  An explicitly authorized missing-reset throttle remains eligible for its one exact permit. A
  successful turn clears only the evidence it tested. Existing continuations remain compatible; `ignore_provider_status` is
  recognized only to return `LEGACY_OVERRIDE_RETIRED`.
- **Durable quota windows and post-reset retries.** Schema 8 retains every applicable quota window
  with scope, model family, reset, source, observation and opaque fingerprint. Changed authentication
  context invalidates recovery authorization without clearing restrictions. After reset, aliases of
  a shared OAuth seat atomically claim one ordinary retry; `QUOTA_RETRY_PENDING` prevents duplicates.
  Unaffected model families retain their ordinary capacity and grant eligibility.
- **Read-only recovery visibility.** Cached capabilities, provider status and the Workers screen
  expose evidence revisions, armed/pending recovery, deadlines, associated tasks and outcomes.
  The dashboard supplies copyable CLI instructions and adds no recovery mutation endpoint.
  Billing remains advisory and separate; scheduled collection stays opt-in and disabled.
- **Isolated packaging smoke.** MCP smoke tests now pass temporary XDG paths directly to the
  child process, preventing a packaging check from opening the installed runtime's database.

## v0.3.0

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
  provider (`ignore_provider_status` overrode it in that release; v0.4 retires the override).
  Nothing is retried elsewhere: the rule that a provider is never substituted is unchanged.
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
