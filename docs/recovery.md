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

Everything below applies to both execution backends. Under the systemd backend the unit is a
transient `systemctl --user` unit; under the [Docker backend](docker-backend.md) it is a
container the private controller launched, and the `systemctl` commands in this page do not
apply — use the controller commands in the next section instead.

## The Docker backend

The controller keeps one private execution record per logical unit under
`state_dir/execution/taskspindle-worker-<task_id>.json` (or `taskspindle-accept-<task_id>.json`).
The record's `phase` is where the launch got to: `creating`, `starting`, `started`, then
`finished` or `retired` with exit evidence, or `failed` when Docker refused the job or an
administrative recovery retired it. Records never contain argv or environment values. In Compose,
run every command below inside the runtime service.

### Looking at the state

```sh
taskspindle-controller status
taskspindle doctor
```

`status` prints one JSON object. The fields that matter for recovery:

- `admission_open` — `false` means the fence is closed and queued work will not start.
  `maintenance.active` is `true` while a maintenance operation owns the fence.
- `engine_reachable` — whether the Docker Engine answered. An unreachable Engine is never read as
  "the container is gone".
- `jobs` — every launch that is active or whose state is `unknown`. `unknown` means the record
  is unsettled: the container is missing without exit evidence, cannot be inspected, or was
  created but never confirmed running.
- `active_reservations` — provider leases (slots) held in the task database, and
  `nonterminal_worker_tasks` — tasks in `RUNNING`, `PREPARING` or `CANCELLING`. A reservation
  with no matching `active` job is a slot held by nothing that runs.

`taskspindle doctor` asks the controller for the same picture and fails, with the unit names, on:

| Check | Fails when |
| --- | --- |
| `worker_admission` | admission is closed; the detail names the command that reopens it |
| `stuck_launches` | a launch record has stayed unsettled (for example in `creating`) for more than ten minutes |
| `orphan_leases` | a lease belongs to a task that is gone or not running, to a task whose launch is unsettled, or has had no worker heartbeat for ten minutes |
| `execution_state` | the controller could not read its records at all |

A `capacity_<provider>` line counts an orphaned lease separately ("0 active, 1 held by orphaned
lease") so it is not mistaken for running work. Under the systemd backend, `orphan_leases` is
answered from the task database alone.

### Docker create failures

An Engine error during container creation can leave an execution record in
`creating` even when the container is absent. `UNIT_START_UNCERTAIN` deliberately
retains the reservation: absence alone does not prove that the request cannot
complete. Cancellation may remain `CANCELLING` until launch uncertainty is resolved.
The error message keeps only safe facts: the HTTP status and a known failure phrase for an
Engine API error, or the exception class (such as `ReadTimeout`) for a transport failure.

After repairing the Engine, use the private controller command for the exact
logical unit:

```sh
taskspindle-controller recover-failed-create taskspindle-worker-<task_id>
taskspindle-controller reconcile
taskspindle-controller status
```

Recovery closes admission and requires a reachable Engine, an absent container,
and a Docker `destroy` event matching the recorded owner, generation, task, unit,
and container name after the reservation was written. It checks absence again
before saving the evidence. Missing, expired, or mismatched events leave the
record unresolved (`UNIT_RECOVERY_UNPROVEN`); do not replace this check with manual database
edits or a blind retry. Back up the database and execution records before operational repair.
Normal reconciliation then settles an already-cancelling task and releases its
reservation, retaining its worktree.

### A launch that provably never ran

The Engine keeps only a bounded event history, and a daemon restart loses it, so the `destroy`
event `recover-failed-create` needs can be gone for good. For a task you have **already
cancelled**, a second administrative recovery retires the record on different evidence:

```sh
taskspindle-controller recover-absent-launch taskspindle-worker-<task_id> --attest-never-ran
taskspindle-controller reconcile
taskspindle-controller status
```

`--attest-never-ran` is your statement that you cancelled the task and that, as far as you can
tell, its worker never ran; without it the command refuses with `UNIT_RECOVERY_UNATTESTED`.
Prefer `recover-failed-create` whenever a destroy event still exists: it is stronger evidence.

The controller closes admission first and keeps it closed, then, under the same operations lock
that serializes launches, requires every one of these:

- the record is a worker launch still in `creating`, with no destroy evidence recorded;
- it was reserved at least one hour ago, judged by the later of its `reserved_at_ns` and the
  record file's modification time, so no create sent for it can still land;
- the task is `CANCELLING`, its `started_at`, `heartbeat_at` and `worker_pid` are all empty, and
  none of its turns has ever ended;
- the Engine answers, and no container in any state (`docker ps --all`) has the record's
  deterministic name or this installation's owner label together with the record's generation or
  unit label. This check runs last, immediately before the record is saved.

Then the record becomes `failed` with a `recovery` object of `kind: "absent"` holding the checked
facts and `checked_at_ns`. The ordinary `reconcile` settles the task to `CANCELLED`, releases its
lease, and keeps its worktree. Anything else is refused and changes nothing but the fence:

| Code | Meaning |
| --- | --- |
| `UNIT_RECOVERY_INVALID` | no record, a record past `creating`, an accept launch, or a task that no longer exists |
| `UNIT_RECOVERY_TOO_RECENT` | the reservation is less than an hour old; wait and retry |
| `UNIT_RECOVERY_NOT_CANCELLED` | cancel the task first |
| `UNIT_RECOVERY_TASK_STARTED` | the task database shows a worker started, heartbeated, or finished a turn |
| `UNIT_RECOVERY_UNPROVEN` | a container for this launch exists; let reconciliation observe it |
| `UNIT_QUERY_FAILED` | the Engine or the task database could not be read; repair it first |

### Reopening admission

Both recoveries, `fence`, and `interrupt-workers` leave admission closed on success and failure
alike; nothing reopens it automatically. Once `status` shows no unexpected `unknown` jobs and
`doctor` no longer reports `stuck_launches` or `orphan_leases`, inspect the queue and reopen:

```sh
taskspindle-controller open
```

Queued tasks then start unattended as slots free. If a maintenance operation owns the fence,
`open` refuses with `UNIT_MAINTENANCE_ACTIVE`; its owner reopens it with
`taskspindle-controller maintenance-release --token <token> --reopen`.

### Engine storage errors

Docker's overlay mount error `no space left on device` can also mean an exhausted
Linux mount namespace. Check free bytes, free inodes, the Engine's mount count,
and `fs.mount-max` before deleting storage. In Docker Desktop, inspect the
daemon's mount namespace, not just a worker container's `/proc/self/mountinfo`.
A reversible limit increase can restore launches but does not clear leaked
mounts. Coordinate Docker Desktop maintenance with all running services; avoid
global pruning or blindly unmounting shared WSL bind mounts.

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
systemctl --user status taskspindle-worker-<task_id>   # systemd backend
taskspindle-controller status                          # Docker backend: look for the unit in jobs
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
  stopped (`systemctl --user stop`, or a container stop through the Docker controller), logged
  once as `cancel_escalated`, and left for the next sweep
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
