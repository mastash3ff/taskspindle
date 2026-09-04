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
