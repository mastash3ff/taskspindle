# Architecture

## Components

```
  Codex session
        │  MCP over stdio (16 tools, one envelope)
        ▼
  taskspindle mcp ──────────────────────────────────────────────┐
  server.py: envelope, annotations, traceback → server.log      │
  service.py: Orchestrator — every rule lives here              │
        │                                                       │
        │ reads/writes            starts units                  │ reconciles
        ▼                              │                        ▼
  store.py (SQLite, WAL)               │                  units.py ──► systemd --user
  tasks · events · receipts            │                              taskspindle.slice
  reviews · grants · leases            │                                    │
  artifacts · journals                 │                                    │
        ▲                              ▼                                    ▼
        │            taskspindle-worker-<task>.service     taskspindle-accept-<task>.service
        │            runner.py: ONE turn, then exit        accept.py: probe, apply, verify, commit
        │                  │                                       │
        │                  ▼                                       ▼
        │            acp_client.py ──► claude-agent-acp        integration.py ──► the root
        │            (ACP 0.12, stdio)   or grok agent stdio    repository (git)
        │                  │
        └──────────────────┘  heartbeat, candidate, checks, transcript

  worktrees.py   detached worktree per task, candidate commit, diff artifact
  repos.py       canonical identity, root snapshot and comparison
  providers.py   profiles, child environment allowlist, OAuth evidence
  recovery.py    what to believe when a worker vanished
  review.py      the reviewer's JSON, and what blocks an acceptance
```

The MCP server never owns an ACP connection. It writes rows and starts units; the units talk to
agents. That is what lets the server exit, crash or be restarted without taking the work with it.

## The state machine

`state_version` increments on every transition. Every tool that changes a task takes the version
you last read and refuses with `STALE_STATE_VERSION` if it has moved.

| From | May go to |
| --- | --- |
| `PREPARING` | `QUEUED`, `FAILED`, `CANCELLING` |
| `QUEUED` | `RUNNING`, `FAILED`, `CANCELLING`, `INTERRUPTED`, `RECOVERY_AMBIGUOUS` |
| `RUNNING` | `COMPLETED`, `RESULT_READY`, `FAILED`, `CANCELLING`, `INTERRUPTED`, `RECOVERY_AMBIGUOUS` |
| `COMPLETED` | `RESUMING` |
| `RESULT_READY` | `ACCEPTING`, `REJECTED`, `REPAIRING` |
| `ACCEPTING` | `ACCEPTED`, `RESULT_READY`, `INTERRUPTED`, `RECOVERY_AMBIGUOUS` |
| `REPAIRING` | `RUNNING`, `FAILED`, `CANCELLING`, `INTERRUPTED`, `RECOVERY_AMBIGUOUS` |
| `INTERRUPTED` | `RESUMING`, `CANCELLING` |
| `RESUMING` | `RUNNING`, `FAILED`, `CANCELLING`, `INTERRUPTED`, `RECOVERY_AMBIGUOUS` |
| `RECOVERY_AMBIGUOUS` | `RESUMING`, `CANCELLING` |
| `CANCELLING` | `CANCELLED`, `FAILED` |
| `ACCEPTED`, `REJECTED`, `CANCELLED`, `FAILED` | nothing; these are terminal |

Two mode restrictions sit on top of the table:

- `RESULT_READY`, `ACCEPTING`, `ACCEPTED`, `REJECTED` and `REPAIRING` are **implement-only**. A
  consult or review task that tried to enter one would be `MODE_FORBIDS_STATE`; they finish at
  `COMPLETED`.
- `COMPLETED → RESUMING` is **consult-only**. A finished advisory conversation can be asked one
  more question in its own session; an implement task that is done is done, and further work goes
  through `REPAIRING` on its candidate.

`COMPLETED` is terminal for accounting purposes and still reopenable for a consult — the one place
where those two ideas differ.

## Worker lifetime: one turn per unit

A worker unit drives exactly one ACP prompt turn and exits. There is no long-lived agent process to
lose track of: every fact a turn produced is in the store before the process returns, and anything
that could not be recorded leaves the task in a state recovery knows how to settle.

Every continuation — a repair, an advisory follow-up, a resume after an interruption — is a
**fresh unit**, a fresh transport and a fresh `session/load` with the stored session id and the
task's worktree as cwd. The replayed `session/update` history that arrives during `session/load` is
consumed and discarded; only updates after the new `session/prompt` are recorded as this turn. If
the agent does not advertise `loadSession`, or the load fails, the answer is `RESUME_UNAVAILABLE`
rather than a silently restarted conversation.

One provider runs one turn at a time. A lease row enforces it: `dispatch_queued` takes the lease
before it starts the unit, so a task whose provider is busy simply waits.

A worker writes a heartbeat to SQLite every five seconds. That heartbeat is the tiebreaker when
systemd has forgotten a unit.

## The control channel: a signal and a table

`cancel_task` moves the task to `CANCELLING` and asks systemd to deliver `SIGTERM` to the unit. The
worker traps it, sends ACP `session/cancel`, waits — bounded — for the prompt response, writes
`CANCELLED` with its transcript, and exits. `KillMode=control-group` and `TimeoutStopSec=30` mean a
worker that does not exit takes its whole process group with it when systemd escalates.

Nothing else crosses between the server and a worker. There is no socket, no pipe and no shared
memory: the SQLite database is the only channel, and a signal is the only interrupt.

## Acceptance

`accept_task` checks its preconditions ([tools.md](tools.md) lists all eleven), writes an
integration journal, moves the task to `ACCEPTING` and starts
`taskspindle-accept-<task>.service`. Then it returns. The unit does the rest:

1. **Probe.** `git merge-tree --write-tree --merge-base=<base> <target> <candidate>` merges in
   memory. It reads and writes neither the index nor the working tree, so it is safe to run against
   a repository you are actively working in. Conflicts here mean the candidate does not apply: the
   task goes back to `RESULT_READY` with a `CONFLICT:<paths>` warning and the repository is
   untouched.
2. **Journal, then apply.** The journal — task, phase, target head, candidate sha — is written
   *before* git is allowed to touch anything, so a crash mid-apply is always recognisable. The
   apply is `git cherry-pick --no-commit <candidate>` in the root, which first requires the root to
   be clean and its HEAD to still be at the journalled target.
3. **Verify in the root.** The task's own `verification_commands` run again, this time against the
   applied tree in the real repository, in an environment holding only `HOME`, `PATH`, `LANG`,
   `TERM=dumb` and `CI=1`. A check that needs a credential is a check that does not belong in an
   automated acceptance. Results are recorded with a `[root]` prefix so they are distinguishable
   from the worktree run.
4. **Commit.** `git commit -m <the message the accepting session signed for>`. Author and committer
   come from the repository's own configuration: the commit belongs to you, not to TaskSpindle.

Any failure after step 2 aborts the apply (`git cherry-pick --abort`, or a `reset --hard` back to
the journalled head — and it refuses to act at all if HEAD is somewhere unexpected), leaves the
repository byte-for-byte where it started, and returns the task to `RESULT_READY` with an
`ACCEPT_FAILED:<reason>` warning. Untracked files are never removed.

Candidate commits themselves are made in the task's worktree with hooks and signing disabled, and
live under `refs/taskspindle/<task_id>/rev/<n>` so they are never on a branch you use.

## Violations

Four things a task can do that TaskSpindle records rather than hides.

- **`SCOPE_VIOLATION`** — the collapsed candidate touches a path outside the task's granted
  `path_prefixes`. The revision is still recorded, with the warning; it cannot be accepted, and a
  repair turn is allowed. Nothing is deleted.
- **`READ_ONLY_VIOLATION`** — a review task tried to write. Every write permission request in
  review mode is denied at the ACP boundary, and after the turn the review worktree must be clean;
  if it is not, the violation is recorded.
- **`ROOT_MUTATION`** — the agent changed the root repository, outside its worktree. TaskSpindle
  snapshots the root's HEAD, branch and dirty state before dispatch and compares afterwards. The
  warning blocks acceptance until someone calls `record_integration` with
  `root_mutation_acknowledged`, which is written to the event log with their summary.
- **`DELEGATION_ATTEMPT`** — the agent asked for a subagent, team or delegation tool. The request
  is denied and recorded. Both first-class profiles are launched with those tools disabled in the
  first place; this catches the case where they are asked for anyway.

## What isolation is, and is not

Each task gets its own detached git worktree under the state directory, created from the
repository's HEAD, never checked out on a branch you use. The agent process is a transient systemd
user unit with a memory ceiling, and its environment is built by allowlist — a name reaches it only
because TaskSpindle put it there, so it inherits no credentials, no proxy settings and no agent
configuration beyond what its profile declares.

**That is containment by construction, not an OS security sandbox.** The agent runs as your user
with your filesystem permissions. It can read anything you can read, and it can write outside its
worktree if it tries. TaskSpindle detects a write to the root repository and reports it as
`ROOT_MUTATION`; it does not, and cannot, prevent one. If you need a real boundary, run TaskSpindle
in a VM or a container.
