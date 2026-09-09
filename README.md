# TaskSpindle

A local MCP server that lets a Codex session delegate bounded work to OAuth-backed Claude Code,
Grok and Antigravity workers in detached git worktrees, then inspect, cross-review and explicitly accept what they
produced.

## What it does

- **Three modes.** `consult` asks a question, `review` reads and reports, `implement` writes code
  in its own worktree. Only `implement` can produce something you can accept.
- **Nothing lands by accident.** A candidate is a commit on a ref of its own, never on a branch you
  use. Accepting it requires that you retrieved the whole diff, that an independent reviewer looked
  at that exact candidate, that every blocking finding has an explicit override with a reason, and
  that the candidate's own verification commands pass — in your repository, before the commit.
- **Independent review.** Codex explicitly selects a different built-in provider to review a
  candidate. Self-review and aliases of the author's family are refused, including at acceptance.
- **Durable workers.** Each turn runs as a transient systemd user unit, so the MCP server can exit,
  crash or restart without taking the work with it. Every continuation reloads the agent's session
  explicitly rather than starting a new conversation and hoping.
- **Recovery that does not guess.** A worker that vanished leaves an `INTERRUPTED` task with its
  worktree and session intact. A situation TaskSpindle cannot settle becomes
  `RECOVERY_AMBIGUOUS` and waits for you.
- **Read-only means read-only.** A consult or review runs a Claude worker in the adapter's `plan`
  session mode, so a write or a shell command becomes a permission request TaskSpindle refuses,
  and a Grok worker in its `read-only` sandbox, where the kernel refuses the write itself; an
  implement runs Claude in `default` mode so every write and command is decided by TaskSpindle's
  own gate, not by your Claude settings.
- **Credentials stay where they are.** The environment each agent sees is built by allowlist, not
  by filtering yours. Workers reuse cached authentication. `auth agy` checks the native CLI's
  existing Google login; credentials are never copied.
- **OAuth first, metered second.** `claude`, `grok` and `agy` are first-class and OAuth-only. An API-key
  or LiteLLM-gateway harness is a configured profile that is never a default, never a fallback, and
  refuses to run without `allow_metered`.
- **Limits are reported, not worked around.** A turn a provider refused for a usage or login
  reason marks that provider throttled or logged out, with the reset time the provider gave;
  `capabilities` and `doctor` say so, and the next `start_task` on it is refused with the other
  provider named. Choosing is yours.
- **Usage you can see.** Every turn's token counts are recorded from the wire, with an estimated
  cost at published rates that is labelled as an estimate. `usage_report`, `taskspindle usage` and
  the dashboard (`taskspindle web`) roll them up with outcomes, timings and violations.
- **Subscription dates.** An independent browser collector records verified renewal and
  cancellation access-end dates for the [Subscriptions page](docs/subscriptions.md).
  Collection uses your normal Chrome profile through the Playwright extension and reports unknown or stale results explicitly;
  it never changes subscriptions or worker routing.

## Quick start

```sh
uv tool install "git+https://github.com/mastash3ff/taskspindle@v0.1.0"
taskspindle setup
taskspindle doctor
codex mcp add taskspindle -- taskspindle mcp
```

Then, in a Codex session, authorize a repository before anything can work in it. See
[docs/install.md](docs/install.md) and [docs/codex-registration.md](docs/codex-registration.md) —
the registration needs `tool_timeout_sec = 1800`.

## How a task flows

1. **Authorize.** `authorize_repository` grants specific providers specific modes on one
   repository, keyed to its canonical identity.
2. **Start.** `start_task` creates a detached worktree from HEAD, composes the first turn, and
   launches a worker unit. An `implement` task must declare its acceptance criteria, the path
   prefixes it may touch, the commands that verify it, and the one-line commit message it is
   aiming at.
3. **Work.** The worker drives exactly one provider turn, collapses the result into a candidate commit,
   runs the verification commands in the worktree, records everything, and exits. The task is now
   `RESULT_READY`.
4. **Inspect.** `task_diff` hands the diff back a page at a time, and each page is receipted. You
   must retrieve all of it.
5. **Review.** A second `start_task` in `review` mode, pointed at that candidate, run by the other
   provider. It returns a structured verdict with findings.
6. **Accept.** `accept_task` checks every precondition, then a detached unit probes the merge with
   `git merge-tree`, applies the candidate to your repository, runs your verification commands
   there, and commits — or undoes everything and tells you why.
7. **Tidy.** `cleanup_task` gives back the worktree and the refs. It refuses to remove a dirty
   worktree unless you insist.

At every step the task carries a `state_version`; hand it back with each change, and a task that
moved underneath you is refused rather than clobbered.

## Documentation

| | |
| --- | --- |
| [install.md](docs/install.md) | requirements, installing, XDG locations, uninstalling |
| [codex-registration.md](docs/codex-registration.md) | registering the server, the timeouts, granting a repository |
| [tools.md](docs/tools.md) | all eighteen tools, the envelope, controlled recovery, and acceptance rules |
| [configuration.md](docs/configuration.md) | `config.toml`, `taskspindle discover`, and what second-class providers may not do |
| [antigravity.md](docs/antigravity.md) | native AGY login reuse, model selection, containment and release gates |
| [platforms.md](docs/platforms.md) | the support matrix and WSL2 |
| [architecture.md](docs/architecture.md) | components, the state machine, acceptance, violations |
| [recovery.md](docs/recovery.md) | `INTERRUPTED`, `RECOVERY_AMBIGUOUS`, and the restart drill |
| [native-overage.md](docs/native-overage.md) | Native extra usage policy, billing observations, and rollout |
| [dashboard.md](docs/dashboard.md) | `taskspindle web`: task inspection, subscriptions, and its JSON API |
| [subscriptions.md](docs/subscriptions.md) | Dedicated browser setup, collection, status, and deployment |
| [rollback.md](docs/rollback.md) | backing it out without losing anything |

## Supported platforms

Linux with a systemd user manager, and WSL2 with systemd enabled. macOS and Windows are not
supported in v0.2.0. Python 3.12+, Node 22+, git 2.38+. See
[docs/platforms.md](docs/platforms.md).

## Status

v0.2.0 adds native Antigravity alongside Claude and Grok. API-key and gateway profiles remain
configured second-class providers. See [the Antigravity guide](docs/antigravity.md) for cached
authentication, model selection and isolation requirements.

TaskSpindle is not affiliated with, endorsed by, or sponsored by OpenAI, Anthropic, Google, or xAI.

## License

Apache-2.0. See [LICENSE](LICENSE).
