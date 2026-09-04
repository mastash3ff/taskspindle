# Recovery

A worker can be killed at any moment: a reboot, `wsl --shutdown`, an OOM kill, someone closing a
laptop. When that happens the store still claims the task is running and nothing is. Reconciliation
settles the difference by asking systemd, and it runs on **every tool call**, not only at startup —
so a tool call is also the heartbeat that keeps a restarted server honest.

Two rules shape every decision:

- **Work is never thrown away.** A task whose worker vanished becomes `INTERRUPTED`, which keeps
  its worktree, its session and its candidate. Not `FAILED`.
- **Ambiguity is never resolved by guessing.** A task systemd has forgotten but whose heartbeat is
  recent becomes `RECOVERY_AMBIGUOUS` and waits for a person.

## `INTERRUPTED`

Something stopped the turn, and TaskSpindle knows it. The worktree, the ACP session id and any
candidate from an earlier revision are all still there.

`continue_task(task_id, expected_state_version)` resumes it: a fresh unit, a fresh transport, a
`session/load` with the stored session id, and the turn is asked again. The default prompt is
"Continue where you left off."; pass your own if the situation changed. If the agent cannot load
the session — it does not advertise `loadSession`, or the load fails — the answer is
`RESUME_UNAVAILABLE`, and your options are to accept whatever candidate already exists, reject the
task, or start a new one.

`cancel_task` is the other way out, and it settles immediately: there is nothing running to signal.

## `RECOVERY_AMBIGUOUS`

systemd does not know the unit, but the worker's heartbeat is less than thirty seconds old. Either
the unit was cleaned up while the process is somehow still running, or the heartbeat is the last
one it ever wrote. TaskSpindle will not choose between those, because one choice risks two workers
in the same worktree and the other risks discarding a live turn.

`task_status` on such a task carries two extra fields: `evidence`, the last recovery decision and
why, and `manual_action`, the sentence telling you what to do. Do that:

```sh
systemctl --user status taskspindle-worker-<task_id>
```

- **Nothing there.** The worker is gone. `continue_task` reconciles once more and, if the task has
  settled to `INTERRUPTED` in the meantime, resumes it.
- **Still running.** Leave it alone and poll `task_status`. It will finish and record its own
  outcome.

If `continue_task` finds the task still ambiguous after that second look, it refuses with
`MANUAL_RECOVERY_REQUIRED` rather than guessing, and the error carries the same evidence and
instruction. That code means one thing: a person has to look. Either wait for the unit to finish,
or `cancel_task` — which moves the task to `CANCELLING` and, once the unit is truly gone, to
`CANCELLED` with its worktree still on disk.

## The reconciliation rules

For each task in an active state (`PREPARING`, `QUEUED`, `RUNNING`, `REPAIRING`, `RESUMING`,
`ACCEPTING`, `CANCELLING`):

| What is observed | What happens |
| --- | --- |
| The recorded boot id is not this boot | `INTERRUPTED` — the machine rebooted; there cannot be a live worker |
| No unit was ever started, and the task is younger than ten minutes | nothing yet |
| No unit was ever started, and the task is older than ten minutes | `FAILED`, code `NEVER_STARTED` |
| The unit is active | nothing; it is still working |
| The unit exited 0 but recorded no outcome | `INTERRUPTED` |
| The unit was OOM-killed | `INTERRUPTED` |
| The unit was signalled or dumped core | `INTERRUPTED` |
| The unit exited non-zero | `FAILED`, with the exit status, marked retryable |
| systemd does not know the unit, heartbeat is stale | `INTERRUPTED` |
| systemd does not know the unit, heartbeat is fresh | `RECOVERY_AMBIGUOUS` |

Three refinements matter:

- **The state machine is never overridden.** If the rule above names a state the machine forbids
  from where the task is, `INTERRUPTED` is used instead when that is legal. An `ACCEPTING` task has
  no path to `FAILED`, so an accept unit that exited badly becomes `INTERRUPTED` and keeps its
  candidate. If nothing legal exists, the task is left exactly where it is and the reason is logged
  once as a `RECOVERY` event — a sweep that keeps finding the same stuck task stays quiet.
- **A `CANCELLING` task whose unit is gone is `CANCELLED`.** The cancel got what it asked for. A
  `CANCELLING` task whose unit is *still running* thirty seconds after the cancel was asked for is
  stopped (`systemctl --user stop`), logged once as `cancel_escalated`, and left for the next sweep
  to settle from the unit's own post-mortem.
- **An `ACCEPTING` task consults its journal**, unless the unit itself is ambiguous — then the
  accept may still be in flight, and asking the journal would undo an apply that is still running.
  The journal has five phases: `probing`, `staged`, `verified`, `committing`, `committed`.
  - `committed`, or `committing` where `HEAD^` is the journalled target head: the commit landed.
    The task is `ACCEPTED` at the head the repository is actually on.
  - `probing`: nothing was ever applied. The task returns to `RESULT_READY`.
  - anything else: the apply is undone — `cherry-pick --abort`, or a `reset --hard` back to the
    journalled head followed by the removal of the candidate's own untracked files — and the task
    returns to `RESULT_READY` with an `ACCEPT_FAILED` warning.
  - The undo is refused outright if HEAD has moved, or if `git status` mentions **any path the
    candidate does not touch**: a `reset --hard` would discard work nobody signed for. That is
    `JOURNAL_MISMATCH`; the repository is not touched, the journal is kept, and the task becomes
    `RECOVERY_AMBIGUOUS` with the reason `accept_recovery_manual`. Look at the repository, then
    decide: finish the merge by hand and `record_integration`, or clean the tree and let the next
    sweep undo the apply.

One task can never end a sweep. A systemd hiccup, or a task that moved underneath the sweep while a
worker finalised, is recorded and the next task is examined.

Reconciliation also drops the provider lease a settled task was holding, so the next queued task
for that provider can start.

## The WSL restart drill

`wsl --shutdown` (or a Windows reboot, or WSL's idle timeout) kills every unit at once. Nothing
gets to run an exit path, and the boot id changes.

1. Start WSL again and let Codex launch the MCP server, or run any tool call. The first call
   reconciles.
2. `list_tasks` — everything that was in flight is now `INTERRUPTED`, reason `boot_changed`.
   Nothing is `FAILED` and nothing was deleted.
3. For each task worth continuing: `task_status` for the current `state_version`, then
   `continue_task`. The fresh worker loads the stored session and asks the turn again.
4. For the rest: `cancel_task`, then `cleanup_task` when you are ready to give the worktree back.
5. If an acceptance was in flight, check the repository before doing anything else —
   `git status` and `git log -1`. The journal will already have settled it one way or the other,
   but the whole point of the journal is that you can verify the answer yourself.

An interrupted task keeps its worktree until you call `cleanup_task`, and `cleanup_task` refuses to
remove a dirty worktree unless you pass `force`. Nothing here removes work on its own.
