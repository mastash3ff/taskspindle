# Changelog

## Unreleased

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
- **Dashboard.** `taskspindle web` serves a read-only page on localhost: tasks, timelines,
  transcripts, diffs, reviews, provider availability and the usage report. The database is opened
  read-only and there are no mutation endpoints.
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
  Grok consult or review runs inside `--sandbox strict`, the one flag that makes its writes ask.
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
