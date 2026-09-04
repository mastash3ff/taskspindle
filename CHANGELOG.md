# Changelog

## Unreleased

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
