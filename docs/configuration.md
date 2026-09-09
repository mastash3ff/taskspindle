# Configuration

Subscription browser collection uses a separate optional `[subscriptions]` section;
see [subscription configuration](subscriptions.md#browser-runtime-and-configuration).
It has no effect on worker profiles, credentials, provider limits, or dispatch decisions.

TaskSpindle reads one file: `$XDG_CONFIG_HOME/taskspindle/config.toml`, or whatever
`TASKSPINDLE_CONFIG` points at. Built-in provider ids are reserved. `taskspindle setup` writes
[`examples/config.toml`](../examples/config.toml) there if the file does not already exist, and
never overwrites one that does.

## First-class providers

The released Claude and Grok profiles use OAuth:

- **`claude`** — the pinned `@agentclientprotocol/claude-agent-acp` adapter that `taskspindle setup`
  installs, launched with `CLAUDE_CONFIG_DIR` pointed at `~/.claude`. Its session options deny the
  adapter every settings source, every MCP server and its own delegation tools (`Agent`, `Task`,
  `TeamCreate`, `SendMessage`): a TaskSpindle worker is a leaf, never a parent.
- **`grok`** — `grok --no-subagents agent --model … --reasoning-effort … --no-leader stdio`
  (`--no-subagents` is a top-level flag and must come before `agent`). Every vendor
  compatibility source Grok would import (Claude, Cursor and Codex skills, rules, agents, MCP
  servers, hooks and sessions) is switched off through the `GROK_<VENDOR>_<SOURCE>_ENABLED=false`
  environment variables, `GROK_DISABLE_API_KEY_AUTH=true` is set, and `GROK_CONFIG` points at a small
  overlay TaskSpindle writes that backs up the subagent denial.

Their ids are reserved. You cannot redefine them in `config.toml`.

**`agy`** is an OAuth-only profile using Google's separately pinned native CLI and its existing
personal login. Setup, authentication and the enforced worker policy are described in
[antigravity.md](antigravity.md). An `agy`-derived profile cannot switch to API-key authentication.

## `[native_overage]`

Every exact profile defaults to `"observe_only"`. Setting an OAuth profile to
`"provider_managed"` is standing authorization for native extra usage after included
allowance exhaustion. Provider account settings remain the spending authority.
Aliases require their own entry; API-key profiles cannot use this policy.

```toml
[native_overage]
claude = "observe_only"
grok = "observe_only"
agy = "observe_only"
```

`observe_only` retains ordinary quota gates and disables AGY credits. It cannot
prevent Claude/Grok from consuming extra usage already enabled on the provider
account. `allow_metered` remains the separate API-key authorization gate.
See [native extra usage](native-overage.md) for continuation, telemetry, and rollout.

## `[providers.<id>]`

Any other ACP-speaking stdio agent is a **configured, second-class profile**.

| Key | Type | Meaning |
| --- | --- | --- |
| `base` | `"claude"`, `"grok"`, or `"agy"` | inherit that built-in's launch command, environment and quirks |
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
  A provider that refused a turn for a usage or login reason is *reported* — in `capabilities`,
  in `doctor`, and as a `PROVIDER_UNAVAILABLE` refusal of the next `start_task` on it — and the
  caller chooses what to do; see [architecture.md](architecture.md#provider-availability). An
  OAuth profile with `base = "claude"` shares the `claude` seat and so shares its availability;
  an `api_key` profile is tracked under its own id.
- **`allow_metered` gate.** An `auth = "api_key"` profile refuses to run unless the request sets
  `allow_metered: true`. Otherwise: `METERED_NOT_ALLOWED`. There is no configuration key that turns
  this off.
- **Explicit reviewer.** Name the reviewing provider yourself. A suggested alternative in a
  quota error does not start a task or switch providers.
- **Independence rule.** A reviewer of a built-in author must be a different built-in provider.
  Every reviewer must differ in id and family. Configured profiles additionally require a
  different command or model. These checks apply at creation, acceptance and manual integration,
  including when profiles have changed since a review. Tasks retain immutable `provider_family`
  provenance. Changed profile families return `PROVIDER_FAMILY_CHANGED`; historical configured
  tasks without that record return `PROVIDER_FAMILY_UNKNOWN`. Old reserved Claude/Grok IDs remain
  valid historical evidence. Same-family review returns `REVIEWER_NOT_INDEPENDENT`.
- **Attribution.** Every task records `auth_mode`, the requested and reported model, and — for a
  gateway — the *host* of `ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL`, never the full URL and never
  the token. `task_result` shows all of it, so metered work is visible after the fact.

The child environment is built by allowlist, never by filtering the parent: a name reaches the
agent only because TaskSpindle put it there. `HOME`, `LANG`, `LC_ALL`, `USER`, `LOGNAME`,
`XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS` and `NO_COLOR` are copied if present; `PATH` is copied with every
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

## Finding agents to configure

`taskspindle discover` reads the ACP community registry — the published list at
`cdn.agentclientprotocol.com`, cached under the data directory after the first fetch — works out
which of its agents are installed on this machine, and prints a `[providers.<id>]` block for each
one. Nothing is downloaded, nothing is run, and `config.toml` is never written: the block is a
proposal to read, edit and paste, with `auth` and `secret_env` left for you to decide. A registry
entry that resolves to an interpreter or a shim rather than an agent is not proposed. `--all` also
lists the registry agents that are not installed; `--registry` points at another URL or a local
file; `--refresh` ignores the cache. The two first-class providers are reported as such and never
proposed, since they are not configured this way.

## Checking a configuration

`taskspindle doctor` resolves every profile and, for each one, checks that its command exists and
that the environment it would be given contains no credential-shaped name it did not declare. For
`api_key` profiles it additionally reports each `secret_env` name as set or unset — advisory, so a
key you have not exported yet does not fail the run. A `config.toml` that is not valid TOML is
reported as a single failed `config` check rather than a traceback.

### Probe discovered agents

`taskspindle discover --probe` starts each installed registry launch form and performs only ACP
`initialize`, with a 30-second handshake limit per agent. It does not authenticate, create a
session, send a model prompt, install software or write profiles. Results include the advertised
agent identity, session-loading support and authentication methods; they do not prove a working
login or model call. `--json` adds a `probe` object to each installed entry. Failures do not stop
other probes, but any failed probe makes the command exit 1. Without `--probe`, discovery starts
no agent processes.
