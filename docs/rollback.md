# Backing TaskSpindle out

Removing TaskSpindle is not one action. Stopping Codex from reaching it takes a second; deciding
what to do with the work it is holding takes longer, and is the part worth being careful about.

## 1. Disable the registration

```sh
codex mcp remove taskspindle
```

Or comment out the `[mcp_servers.taskspindle]` block in `~/.codex/config.toml`. Either way, new
Codex sessions stop launching the server. That is the whole of the immediate rollback: no daemon
survives it, because there was never a daemon — the server only ran while Codex had it open.

A task whose worker unit is already running keeps running. It will finish, write its outcome to
the database, and exit. Nothing about de-registering interrupts work in flight.

To stop a specific one first:

```sh
systemctl --user list-units 'taskspindle-*'
systemctl --user stop taskspindle-worker-<task_id>
```

Prefer `cancel_task` while the server is still registered — it records the cancellation properly.
`systemctl stop` sends the same `SIGTERM` the worker handles, but if you have already removed the
registration there is nobody to record what happened, and the task will settle as `INTERRUPTED` on
some future reconcile that may never come.

## 2. Decide about each task

```
list_tasks                     # what exists, and in what state
task_status <task_id>          # the candidate, the warnings, the worktree path
```

Per task, the honest options are:

- **Accept it** while the server is still registered, if the candidate is good and reviewed.
- **Cancel it**, then `cleanup_task` to give back the worktree, refs and scratch space.
- **Retain it.** Do nothing. The worktree stays, the branch-free candidate commits stay under
  `refs/taskspindle/<task_id>/rev/<n>`, and the database keeps the record. This is the default and
  costs nothing but disk.

A candidate you want but do not want to accept through TaskSpindle is an ordinary git commit. Cherry-pick it:

```sh
git -C /path/to/repo log --oneline refs/taskspindle/<task_id>/rev/1
git -C /path/to/repo cherry-pick --no-commit <candidate_sha>
```

## 3. Preserve the record

The SQLite database at `~/.local/state/taskspindle/taskspindle.sqlite3` is the only record of what
ran: every task, every state change, every diff receipt, every review, every override, every
acceptance. The artifacts beside it — candidate diffs, transcripts, root snapshots, under
`~/.local/state/taskspindle/tasks/<task_id>/` — are the evidence those rows refer to.

If any of that might be needed later, copy the state directory somewhere before you delete
anything:

```sh
cp -a ~/.local/state/taskspindle ~/taskspindle-state-backup
```

Uninstalling the package does not touch it. Deleting it is irreversible and no tool will do it for
you.

## 4. Give back the worktrees, carefully

Task worktrees live under `~/.local/state/taskspindle/tasks/<task_id>/worktree` and are registered
with the repositories they came from.

- Use `cleanup_task` where you can. It removes the worktree, deletes the task's refs and clears its
  scratch space, and it **retains a worktree with uncommitted changes** instead of removing it.
- `cleanup_task(force=true)` overrides that retention. Only use it on a worktree you have looked at.
  Uncommitted work in a task worktree is work no candidate commit captured, and it is not recorded
  anywhere else.
- Never `git worktree remove --force` or `rm -rf` a dirty task worktree to make an error go away.
  If `cleanup_task` refuses, that refusal is information.

If you delete the state directory by hand, run `git worktree prune` afterwards in every repository
you granted, so git forgets the worktrees that are no longer there.

## 5. Leave the credentials alone

TaskSpindle never wrote a credential and has none to remove. `~/.claude`, `~/.grok/auth.json` and
your Codex authentication belong to those tools; a rollback that touched them would log you out of
software that has nothing to do with this.

The one file TaskSpindle does write near a provider's configuration is
`~/.local/state/taskspindle/grok-overlay.toml`, which is its own file inside its own state
directory, pointed at by `GROK_CONFIG` for the child process only. Your `~/.grok/` configuration is
never edited.

## 6. Uninstall

```sh
uv tool uninstall taskspindle
```

Then, only if you have settled every task and kept whatever record you need:

```sh
rm -rf ~/.local/state/taskspindle ~/.local/share/taskspindle ~/.config/taskspindle
```
