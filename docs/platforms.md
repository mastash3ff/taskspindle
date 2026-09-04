# Supported platforms

| Platform | v0.1.0 |
| --- | --- |
| Linux with a systemd user manager | supported |
| WSL2 with systemd enabled | supported |
| Linux without systemd (or with a user manager that is not running) | not supported |
| macOS | not supported |
| Windows, outside WSL2 | not supported |

## Why systemd

Every worker and every acceptance runs as a transient `systemd-run --user` unit. That is not a
packaging preference; three things depend on it:

- **The MCP server can exit.** A turn outlives the Codex session that started it, and the work
  keeps going. Nothing is lost when Codex restarts.
- **A crashed server can find out what happened.** After a restart TaskSpindle asks systemd about
  each unit it thinks is running, and gets an answer — still active, exited cleanly, OOM-killed,
  signalled — rather than having to guess. That is the whole basis of
  [recovery](recovery.md).
- **A runaway agent hits a ceiling instead of the machine.** Each unit runs with `MemoryHigh=2G`,
  `MemoryMax=3G`, `MemorySwapMax=512M`, `OOMPolicy=kill`, `KillMode=control-group` and
  `TimeoutStopSec=30`. Those limits are per unit, so two workers can each use up to their own
  ceiling. Units are placed in a `taskspindle.slice`, which systemd creates implicitly — there is
  no slice unit file shipped, and therefore no aggregate limit across all TaskSpindle work. If you
  want one, write your own `~/.config/systemd/user/taskspindle.slice`.

`taskspindle doctor` checks all of this: `systemctl --user is-system-running` must answer `running`
or `degraded`, and (unless `--no-live`) a transient unit is actually started and waited on.

## WSL2

WSL2 works, with systemd enabled in `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

**What a WSL restart does.** `wsl --shutdown`, a Windows reboot, or WSL's own idle shutdown stops
the whole distribution: every unit dies at once, without warning and without running any exit path.
The boot id changes.

TaskSpindle is built for exactly this. Each task records the boot id it was dispatched under; on
the next reconcile — which happens on every tool call, not just at startup — a task whose boot id
does not match this boot cannot have a live worker, whatever the database says. Such a task becomes
`INTERRUPTED`, which keeps its worktree, its session and its candidate, and `continue_task` resumes
it. Nothing is thrown away and nothing is silently retried. The drill is in
[recovery.md](recovery.md).

Two practical notes:

- Work inside the WSL filesystem, not under `/mnt/c`. Git operations across the Windows filesystem
  boundary are slow enough to change how long a turn takes, and file mode and symlink handling
  differ.
- WSL shuts an idle distribution down by default. A long turn with no other activity can be stopped
  by that timer; raise `vmIdleTimeout` in `.wslconfig` if it bites.

## macOS and Windows

Not supported in v0.1.0, and not partially supported: `taskspindle doctor` will fail on the systemd
check, and no worker can be dispatched. The obstacle is the unit backend — launchd and Windows
services answer different questions about a dead process, and the recovery rules are written
against systemd's answers. Everything above the unit backend is portable; nobody has written the
other backends.
