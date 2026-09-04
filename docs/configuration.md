# Configuration

TaskSpindle reads one file: `$XDG_CONFIG_HOME/taskspindle/config.toml`, or whatever
`TASKSPINDLE_CONFIG` points at. With no file at all you get the two first-class providers, and
that is the supported setup. `taskspindle setup` writes
[`examples/config.toml`](../examples/config.toml) there if the file does not already exist, and
never overwrites one that does.

## First-class providers

Two profiles ship built in, live-tested, and OAuth-only:

- **`claude`** — the pinned `@agentclientprotocol/claude-agent-acp` adapter that `taskspindle setup`
  installs, launched with `CLAUDE_CONFIG_DIR` pointed at `~/.claude`. Its session options deny the
  adapter every settings source, every MCP server and its own delegation tools (`Agent`, `Task`,
  `TeamCreate`, `SendMessage`): a TaskSpindle worker is a leaf, never a parent.
- **`grok`** — `grok agent --model … --reasoning-effort … --no-leader --no-subagents stdio`, with
  `GROK_CONFIG` pointed at an overlay TaskSpindle writes that turns off hooks, skills, MCP servers
  and subagents, and `GROK_DISABLE_API_KEY_AUTH=true`.

Their ids are reserved. You cannot redefine them in `config.toml`.

## `[providers.<id>]`

Any other ACP-speaking stdio agent is a **configured, second-class profile**.

| Key | Type | Meaning |
| --- | --- | --- |
| `base` | `"claude"` or `"grok"` | inherit that built-in's launch command, environment and quirks |
| `auth` | `"oauth"` or `"api_key"` | **required** |
| `command` | string[] | argv of an ACP stdio agent; required unless `base` supplies one |
| `env` | table of strings | plain environment names and values handed to the agent |
| `secret_env` | string[] | **names only**, `api_key` profiles only |
| `model` | string | recorded on every task; requested where the agent understands it |
| `effort` | string | reasoning effort, likewise |
| `modes` | string[] | narrows which of `consult`, `review`, `implement` this profile serves |

`secret_env` is a list of variable *names*. The value is read from the environment the MCP server
was launched in, at the moment the agent process starts, and is passed to that process only. It is
never written to the store, never logged, never returned by a tool. `doctor` reports each name as
present or absent and nothing more. `secret_env` on an `auth = "oauth"` profile is a configuration
error: an OAuth seat has no key to pass.

A profile with `base` inherits that built-in's environment before its own `env` is applied. A
`grok`-derived profile carries its model and effort on the argv; a `claude`-derived one passes them
through session options instead, so its argv stays the adapter's.

## What second-class actually means

These are rules the code enforces, not advice:

- **Never a default.** A task uses a configured profile only when `start_task` names it in
  `provider` *and* the repository grant lists that exact id.
- **Never a fallback.** Nothing is ever retried on a different provider, in either direction.
- **`allow_metered` gate.** An `auth = "api_key"` profile refuses to run unless the request sets
  `allow_metered: true`. Otherwise: `METERED_NOT_ALLOWED`. There is no configuration key that turns
  this off.
- **Explicit reviewer.** Automatic opposite-provider routing exists only between `claude` and
  `grok`. A candidate produced by a configured profile has no automatic reviewer at all; you name
  the reviewing profile yourself.
- **Independence rule.** A reviewer of a first-class author must be the *other* first-class
  provider. A reviewer of a second-class author must differ from the author in id and in either
  command or model. Otherwise: `REVIEWER_NOT_INDEPENDENT`.
- **Attribution.** Every task records `auth_mode`, the requested and reported model, and — for a
  gateway — the *host* of `ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL`, never the full URL and never
  the token. `task_result` shows all of it, so metered work is visible after the fact.

The child environment is built by allowlist, never by filtering the parent: a name reaches the
agent only because TaskSpindle put it there. `HOME`, `LANG`, `LC_ALL`, `USER`, `LOGNAME`,
`XDG_RUNTIME_DIR` and `NO_COLOR` are copied if present; `PATH` is copied with every
`node_modules/.bin` entry stripped so a stray global adapter cannot shadow the pinned one;
`TERM=dumb`, `CI=1`, `NO_BROWSER=1` and a per-task `TMPDIR` are set. Then the profile's `env`, then
its `secret_env` values. An OAuth profile gets no exemption from the credential-shaped-name check
at all, so a configured `env` cannot smuggle `ANTHROPIC_BASE_URL` past an OAuth seat.

## Example: a Claude adapter behind a LiteLLM gateway

**Not tested live in v0.1.0.**

```toml
[providers.claude-litellm]
base = "claude"
auth = "api_key"
model = "kimi"
modes = ["consult", "review"]
env = { ANTHROPIC_BASE_URL = "http://127.0.0.1:4000" }
secret_env = ["ANTHROPIC_AUTH_TOKEN"]
```

Export `ANTHROPIC_AUTH_TOKEN` in the environment Codex launches the MCP server from. Then
`start_task(provider="claude-litellm", allow_metered=true, …)`, on a repository whose grant lists
`claude-litellm`. `task_result` will report `auth_mode: "api_key"` and `gateway_host: "127.0.0.1"`.

Narrowing `modes` to `consult` and `review` is a choice, not a requirement — a metered profile that
never writes to your repository is easier to reason about.

## Example: a generic OpenCode agent

**Not tested live in v0.1.0.**

```toml
[providers.opencode]
auth = "api_key"
command = ["opencode", "acp"]
modes = ["consult", "review"]
secret_env = ["OPENAI_API_KEY"]
```

No `base`, so the profile declares its own argv and inherits none of either built-in's quirks. Any
agent that speaks ACP 0.12 over stdio can be configured this way; whether it honours a permission
denial or a cancel is between you and that agent. Harnesses with no ACP stdio endpoint are out of
scope: there is no other transport.

## Checking a configuration

`taskspindle doctor` resolves every profile and, for each one, checks that its command exists and
that the environment it would be given contains no credential-shaped name it did not declare. For
`api_key` profiles it additionally reports each `secret_env` name as set or unset — advisory, so a
key you have not exported yet does not fail the run. A `config.toml` that is not valid TOML is
reported as a single failed `config` check rather than a traceback.
