# Installing TaskSpindle

## What the machine needs first

TaskSpindle drives other people's command line tools and puts every worker in a systemd user
unit, so most of the requirements are things it launches rather than things it bundles.

- **Linux with a systemd user manager**, or **WSL2 with systemd enabled**. `systemctl --user
  is-system-running` must answer `running` or `degraded`. macOS and Windows are not supported; see [platforms.md](platforms.md).
- **Python 3.12 or newer.**
- **Node 22 or newer**, with `npm`. The Claude adapter is a Node program.
- **git 2.38 or newer.** Acceptance probes a merge with `git merge-tree --write-tree`, which needs
  that version to behave the way TaskSpindle relies on.
- **Codex CLI**, which is what talks to the MCP server.
- **Claude Code, logged in to a Pro or Max subscription.** `claude auth status --json` must report
  `loggedIn: true`, `authMethod: claude.ai`, `subscriptionType: pro` or `max`, and
  `apiProvider: firstParty`.
  TaskSpindle reads those four fields and nothing else out of the payload.
- **Grok CLI with compatible ACP capabilities**, logged in at grok.com, with
  `~/.grok/auth.json` present. Its version label is informational, not an allowlist.
  The real read-only sandbox launch must initialize and advertise session loading
  and a supported OAuth authentication method.
- **Antigravity CLI (`agy`), if you use that provider**, logged in interactively, resolved from
  `PATH` (or `~/.local/bin/agy`). Only a minimum version is enforced; a newer build than
  TaskSpindle has been tested with is accepted and reported as advisory, not blocking. See
  [antigravity.md](antigravity.md).

TaskSpindle never logs you in and never copies a credential. Both provider CLIs hold their own
sessions; TaskSpindle only asks them whether they have one.

## Install

```sh
uv tool install "git+https://github.com/mastash3ff/taskspindle@v0.5.0"
taskspindle setup
taskspindle doctor
```

`taskspindle setup` installs the pinned Claude ACP adapter, creates the directories TaskSpindle
owns and writes an example `config.toml` if you do not already have one. It runs
`npm ci --ignore-scripts --no-audit --no-fund` against a lock file shipped inside the package, in
an environment holding only `PATH`, `HOME` and `LANG`, so no registry token or proxy setting
reaches npm. It never logs in, never copies credentials and never edits your Codex configuration.
It also pins whichever `node` it finds on your PATH at that moment into the adapter launcher, so a
worker unit's short PATH never needs its own `node` entry; re-run `taskspindle setup` after
installing or switching Node versions to re-pin it.

`taskspindle doctor` asks one question at a time and prints the whole list, so a first run on a
fresh machine tells you everything that is missing rather than the first thing that broke:

```
[ok] git: git version 2.43.0
[ok] systemd_user: the user manager is running
[FAIL] node: RuntimeError: node 20.11.1 is older than the required 22
[warn] codex_registration: RuntimeError: /home/you/.codex/config.toml has no [mcp_servers.taskspindle] section
```

`[warn]` marks an advisory check: it describes a setup that is merely convenient and never makes
the run fail. `taskspindle doctor --json` prints the same report as JSON, and
`taskspindle doctor --no-live` skips launch probes, including the transient systemd unit,
Grok and Claude ACP initialization, and AGY cached catalog access. It still runs bounded
local diagnostics such as version and cached-auth commands. A passing `--no-live` report
does not establish protocol compatibility or model-turn access.

TaskSpindle also has `taskspindle worker` and `taskspindle accept` subcommands, invoked by systemd
units for detached work and acceptance — not for interactive use.

Registering the server with Codex is a separate, deliberate step:
[codex-registration.md](codex-registration.md).

## Where things go

Everything follows the XDG variables, and honours them if you set them.

| What | Default | Contents |
| --- | --- | --- |
| Configuration | `$XDG_CONFIG_HOME/taskspindle/config.toml` (`~/.config/...`) | the one file you may write |
| State | `$XDG_STATE_HOME/taskspindle/` (`~/.local/state/...`) | `taskspindle.sqlite3`, `server.log`, `grok-overlay.toml`, `tasks/<task_id>/` worktrees, transcripts and diffs |
| Data | `$XDG_DATA_HOME/taskspindle/` (`~/.local/share/...`) | installed runtimes |
| Runtime | `$XDG_DATA_HOME/taskspindle/runtimes/<version>/` | the pinned adapter's `node_modules` |

The runtime directory is versioned, so upgrading installs the new adapter beside the old one
rather than pulling it out from under a task that is still running. Every directory is created
mode `0700`.

`TASKSPINDLE_CONFIG` overrides the configuration file path for every subcommand, the MCP server
and the detached units alike.

## Upgrading

### Daily provider CLI updates

| Provider | Update boundary | Compatibility evidence |
| --- | --- | --- |
| Grok | Uses the daily CLI; a new version or changed version banner does not veto readiness | Actual sandboxed ACP initialization, session-loading and OAuth capabilities; task-time permission and model checks |
| Claude | Worker uses the managed, locked ACP adapter/SDK and pinned Node; daily CLI supplies cached auth diagnostics | Explicit JSON output; extra fields ignored, required OAuth fields validated; live managed-adapter initialization |
| AGY | Worker runs whichever `agy` is resolved from PATH (or `~/.local/bin/agy`); only a minimum version is enforced, and a newer build is accepted as advisory | Minimum-version check, cached model catalog and task-time stream/permission checks |

Run `taskspindle doctor` after an update to inspect actual launch readiness. Version output
alone cannot prove compatibility. Missing required protocol features, unsafe paths, changed
authentication context, or quota/billing restrictions still block the affected operation.
There is no automatic provider substitution, credential rebinding, or paid fallback.

Claude's managed adapter pin is a reproducible installation boundary: `setup` rebuilds it from
the shipped lock, so drain active workers before provisioning, and a version below the pin is
refused (a newer one is accepted as advisory). AGY has no such installation boundary -- there is
nothing for `setup --provider agy` to build or replace, only a minimum version to confirm -- so
every daily CLI update takes effect on the next launch automatically; a release below the
enforced minimum is what actually blocks a task.

### TaskSpindle releases

```sh
uv tool upgrade taskspindle    # or: uv tool install --force "git+...@v0.5.0"
taskspindle setup
taskspindle doctor
```

Run `setup` again after every upgrade: the adapter version is pinned per TaskSpindle version, and
the new runtime directory starts empty. Your `config.toml` is never overwritten.

## Uninstall

```sh
codex mcp remove taskspindle          # stop Codex launching the server
uv tool uninstall taskspindle
```

That leaves your data behind on purpose. To remove it, check first that no task still owns a
worktree you care about — `taskspindle` has no destructive subcommand, and the SQLite database is
the only record of what ran:

```sh
rm -rf ~/.local/state/taskspindle ~/.local/share/taskspindle ~/.config/taskspindle
```

Task worktrees live under the state directory and are registered with the repositories they came
from, so if you delete the state directory by hand, run `git worktree prune` in each repository
afterwards. [rollback.md](rollback.md) describes the careful version of this.
