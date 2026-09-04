# Registering TaskSpindle with Codex

TaskSpindle is an MCP server spoken over stdio. Codex launches it; nothing listens on a port and
no daemon runs between sessions.

## The command

```sh
codex mcp add taskspindle -- taskspindle mcp
```

Then set the two timeouts, because the defaults are too short for what this server does:

```sh
codex mcp add taskspindle \
  --tool-timeout-sec 1800 \
  --startup-timeout-sec 30 \
  -- taskspindle mcp
```

## The equivalent configuration block

If you would rather edit `~/.codex/config.toml` yourself, the block is:

```toml
[mcp_servers.taskspindle]
command = "taskspindle"
args = ["mcp"]
tool_timeout_sec = 1800
startup_timeout_sec = 30
```

The same block is in [`examples/codex-mcp.toml`](../examples/codex-mcp.toml). `taskspindle mcp` and
`python -m taskspindle.server` are the same server; use whichever the environment makes easier.

`taskspindle doctor` looks for `[mcp_servers.taskspindle]` in `~/.codex/config.toml` and reports it
as an advisory check — it will tell you the registration is missing, but it will never fail the run
over it, and it never writes to that file.

## Why the timeouts matter

`tool_timeout_sec = 1800` is not caution for its own sake. Two tool calls can legitimately take a
long time:

- **A turn.** `start_task` and `continue_task` return as soon as the worker unit is launched, but
  an implement turn runs the agent, collapses the result into a candidate commit and runs your
  verification commands in the worktree. The tool call that started it is short; the polling you do
  afterwards with `task_status` is what fills the wall clock.
- **An acceptance.** `accept_task` validates every precondition, journals the intent and hands the
  root commit to a detached unit, then returns. The unit runs your verification commands *again*,
  this time in the root repository, before it commits. A test suite that takes twenty minutes takes
  twenty minutes.

Thirty seconds of startup timeout covers opening the SQLite database, reading the configuration
and reconciling any task left over from a previous session.

## Why the diff is paged small

Codex hands a tool's output to the model only up to its `tool_output_token_limit` — a few thousand
tokens by default — and truncates the rest without telling the server. A `task_diff` page that was
cut off on the way in is still receipted in full, and the "whole diff retrieved" gate that
`accept_task` enforces would then pass on bytes the session never read. So `task_diff` returns
16384 bytes by default (about 22 KB of base64, under the default limit), and the session should
page — `offset` advancing by each page's `length` until it reaches `size` — rather than ask for
the 262144-byte maximum. Put that rule in the instructions Codex reads (`AGENTS.md`); raising
`tool_output_token_limit` globally makes every other tool's output larger too.

## What Codex offers that TaskSpindle does not use

TaskSpindle is a server Codex calls, and most of what recent Codex releases added is on the other
side of that line: `spawn_agent` and the multi-agent roles, goals, memories and the app-server
protocol are Codex's own, and a TaskSpindle worker is deliberately not one of Codex's threads.
Three things on the Codex side are worth knowing about:

- **Hooks.** Codex's `PostToolUse` and `SubagentStop` hooks can run a command when a tool call
  finishes. A hook on `accept_task` could, for example, record the accepted candidate somewhere of
  your own. Nothing in TaskSpindle needs one.
- **Elicitation.** Codex supports MCP elicitation, so a future TaskSpindle could ask the session a
  question in the middle of a tool call — the natural shape for `RECOVERY_AMBIGUOUS` — instead of
  returning `MANUAL_RECOVERY_REQUIRED` and waiting. It does not today.
- **Codex's own rate limits.** Every Codex turn carries its account's usage windows, and the
  app-server exposes them. Those are Codex's limits, not the workers': TaskSpindle's
  `usage_report` and `capabilities` describe the Claude and Grok seats it drives, and say plainly
  where a window is only observable from a refusal.

## Granting a repository

Registration lets Codex reach the tools. It does not let any provider touch any repository:
TaskSpindle refuses to start a task in a repository that has no active grant, with `GRANT_MISSING`.

Ask Codex to authorize one:

> Authorize `/home/you/projects/thing` for claude and grok, consult and implement.

which calls:

```json
{"tool": "authorize_repository",
 "path": "/home/you/projects/thing",
 "providers": ["claude", "grok"],
 "modes": ["consult", "review", "implement"]}
```

A grant is keyed to the repository's canonical identity — the real path of its common git
directory plus its root commit — so a clone moved to a new path, or re-rooted, needs authorizing
again. `list_repository_policies` shows every grant, and `revoke_repository` withdraws them; omit
`providers` or `modes` to revoke all of them.
