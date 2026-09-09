# Architecture

## Components

```
  Codex session                                  browser (localhost)
        │  MCP over stdio (18 tools, one envelope)        │  taskspindle web:
        ▼                                                 ▼  task database opened read-only
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
  provider_status · provider_windows   │                                    │
  provider_recovery_permits            │                                    │
  turn_usage                           │                                    │
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
  limits.py      what a refused turn means for its provider; never what to do about it
  provider_recovery.py  single-use authorization bound to cached refusal evidence
  usage.py       token counts per turn, the price table, the rolled-up report
  web/           operator console: read-only task/overview store, protected subscription actions
  subscriptions/ separate SQLite observations/queue, background browser collector, CLI
```

Subscription actions use a separate `subscriptions.sqlite3` database. Normal mode connects to a
selected regular Windows Chrome profile through the paired Playwright extension; dedicated mode
uses separate TaskSpindle browser profiles. The dashboard queues loopback-only, same-origin,
CSRF-protected connection/refresh requests; an independent collector processes them and observes
billing information. It does not change task state or worker eligibility. See
[subscription tracking](subscriptions.md).

The MCP server never owns an ACP connection. It writes rows and starts units; the units talk to
agents. That is what lets the server exit, crash or be restarted without taking the work with it.
`claude-agent-acp` is launched through a shim `taskspindle setup` writes over npm's own symlink,
execing the `node` it pinned at setup time by absolute path, since a worker unit's PATH is too
short to find one on its own.

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
| `FAILED` | `RESUMING` only for an eligible native extra-usage quota continuation |
| `ACCEPTED`, `REJECTED`, `CANCELLED` | nothing; these are terminal |

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
worker that does not exit takes its whole process group with it when systemd escalates. The cancel
time is recorded on the `CANCEL_REQUESTED` event: a unit still running thirty seconds later is
stopped outright by the next reconciliation, which logs `cancel_escalated` once and lets the
unit's own post-mortem settle the task.

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
2. **Journal, then apply.** The journal — task, phase, target head, candidate sha, changed paths —
   is written
   *before* git is allowed to touch anything, so a crash mid-apply is always recognisable. The
   apply is `git cherry-pick --no-commit <candidate>` in the root, which first requires the root to
   be clean and its HEAD to still be at the journalled target.
3. **Verify in the root.** The task's own `verification_commands` run again, this time against the
   applied tree in the real repository, in an environment holding only `HOME`, `PATH`, `LANG`,
   `TERM=dumb` and `CI=1`. A check that needs a credential is a check that does not belong in an
   automated acceptance. Results are recorded with a `[root]` prefix so they are distinguishable
   from the worktree run.
4. **Commit.** The journal moves to `committing`, then `git commit -m <the message the accepting
   session signed for>`, then `committed`. Author and committer come from the repository's own
   configuration: the commit belongs to you, not to TaskSpindle. The `committing` phase is what
   lets a recovery tell a commit that landed from one that never ran.

Any failure after step 2 aborts the apply (`git cherry-pick --abort`, or a `reset --hard` back to
the journalled head followed by the removal of the candidate's own untracked files), leaves the
repository byte-for-byte where it started, and returns the task to `RESULT_READY` with an
`ACCEPT_FAILED:<reason>` warning. The abort refuses to act at all if HEAD is somewhere unexpected,
or if the working tree is dirty with anything the candidate does not touch: discarding work nobody
signed for is not a decision recovery gets to make. See [recovery.md](recovery.md).

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
  snapshots the root's HEAD, branch and dirty state before dispatch and compares afterwards. Every
  task with a repository behind it is checked this way, `consult` and `review` included; only a
  repository-less consult, which has no root, is not. The warning blocks acceptance — and a
  `record_integration` of a hand-made merge — until someone calls `record_integration` with
  `root_mutation_acknowledged`, which is written to the event log with their summary. If the
  snapshot cannot be read the check did not run, and that is recorded as `ROOT_CHECK_SKIPPED`
  rather than passed.
- **`DELEGATION_ATTEMPT`** — the agent asked for a subagent, team or delegation tool. The request
  is denied and recorded. Both first-class profiles are launched with those tools disabled in the
  first place; this catches the case where they are asked for anyway.
- **`MODE_SWITCH_ATTEMPT`** — the agent asked to leave the session mode it was put in (Claude's
  "Ready to code?" prompt on its way out of plan mode). Denied and recorded; the mode was chosen
  for the task, not by it.

## Provider availability

A provider can refuse a turn because a usage window is exhausted, login expired, access was
denied, or the selected model is unavailable. TaskSpindle records those observations for the
coordinator to consult before assigning new work. Billing observations remain separate and do
not gate workers: a browser account is not assumed to be the CLI account.

**Classifying.** `acp_client.py` stays thin: it keeps what the wire said — the JSON-RPC code,
message and data of the agent's error — on the `AcpError` it raises, and interprets none of it.
`limits.py` does the interpreting, in a fixed order of evidence: the ACP "authentication required"
code; the Claude adapter's `errorKind` (`authentication_failed`, `oauth_org_not_allowed`,
`rate_limit`, `billing_error`, `model_unavailable`); the Claude Agent SDK's own usage-limit message
prefixes and older `usage limit reached|<epoch>` sentinel; and Grok's terminal structured provider
HTTP status and source-proven terminal xAI retry notifications. Incidental status numbers and
tool output do not change account state. A credit-specific 403 needs terminal provider evidence;
an unrelated 403 stays ambiguous. Retry prose is reduced to fixed diagnostic fields.
Anything else is the failure it always was. A billing refusal is an access denial, not proof of
subscription expiry. Model refusals bind to the task's selected model, not an unrelated ID in
provider output. Persisted access reasons and diagnostic fields are sanitized.

**Recording.** The runner writes a classified refusal in three places: the task's `error` (code
`PROVIDER_THROTTLED`, `PROVIDER_AUTH_EXPIRED`, `PROVIDER_ACCESS_DENIED`, or
`PROVIDER_MODEL_UNAVAILABLE`, with the relevant scope and reset time in `details`), a
`PROVIDER_LIMIT` event on the task, and the provider's row in `provider_status`, keyed by the
seat — an OAuth profile derived from `claude` shares `claude`'s seat and therefore its throttle;
an `api_key` profile is keyed by its own id. A throttle also writes a `provider_windows` row at
100% for the window it named. A turn that runs writes the windows the agent reported along the way
(the Claude adapter forwards the SDK's rate-limit events). Model-scoped evidence is stored
separately so it cannot disable other models. Successful use advances `last_success_at`, but
clears a refusal only when it matches the observation taken before the turn; a sibling's newer
failure survives. Schema 5 adds this evidence without changing task ownership or provider binding.

**Selection before task creation.** MCP `capabilities`, `GET /api/providers`, and
`taskspindle providers --json` share the availability projection: scope, source, observation time,
last successful use, staleness, reset time, retry eligibility, and a suggested next action.
`model_availability` lists known model observations even when a profile has no fixed default.
A passed reset means `unknown` and eligible for an ordinary attempt, not proven healthy.
Unresolved refusals remain blocking even when their observations become stale. Each availability
projection includes an `evidence_revision` and a recovery projection derived entirely from cached
state.

Quota is a separate durable projection. `quota_restrictions` retains every applicable account or
model-family window with its scope, normalized model family, reset, source, observation, and
opaque fingerprint. A changed `auth_context` invalidates authorization tied to the earlier context;
it never clears quota restrictions or identifies an account. After all applicable restrictions have
passed their reset, aliases of the same OAuth seat share one ordinary, acceptance-bearing retry
claim. A pending claim is attached to its task and prevents duplicate replacement tasks. An
unaffected model remains eligible under the normal capacity and grant rules.

The Codex coordinator selects a compatible subscription worker before calling `start_task`,
preserving explicit provider/model requirements, repository grants, capacity, and review
independence. Unknown access permits ordinary needed work; it never justifies synthetic probes.
TaskSpindle receives an explicit provider and does not migrate, retry, or change that task's
provider. When none is eligible the coordinator reports the constraint and next action.
A controlled retry first arms one permit against that exact evidence revision, then names the
permit on one `start_task`. Arming and task creation both reject changed evidence, future resets,
fresh native quota exhaustion, scope changes, and another active attempt for the shared provider
account. Task creation claims the permit transactionally, so it cannot authorize two tasks. The
permit is settled permanently from the accepted provider turn; expiry only governs admission and
never interrupts running work. A non-quota refusal or an explicitly authorized throttle with no
reported reset may use this one exact permit. A quota restriction with a reported future reset and
fresh native exhaustion never bypass it. The legacy `ignore_provider_status` field remains
recognizable for compatibility but is rejected as `LEGACY_OVERRIDE_RETIRED`. Native cached-login
checks alone do not clear a refusal.
Metered workers still require separate explicit opt-in and are never selected automatically by
the work pool.

`capabilities(check_providers=["grok"])` and `taskspindle providers --check --provider grok`
optionally refresh Grok's native quota observation. The checker starts a session-free native ACP
process, calls the vendor billing extension, and retains only normalized percentage, weekly/monthly
window, reset, timestamps, version, freshness, and safe status fields. The persistent cache is
shared by OAuth model aliases of the same provider account. It coalesces concurrent checks for
five minutes and invalidates on executable, auth mode, or relevant config/auth-file metadata
changes; model and effort selection do not create separate account quota caches. It never starts inference, login, a browser, or direct HTTP;
it reads no credential contents and never upgrades the CLI. Unsupported versions keep quota
unknown rather than trying another transport.

A fresh explicit 100-percent native observation can add a temporary new-task gate. A passed reset,
stale observation, or unknown quota permits an ordinary attempt unless task evidence still refuses
it. Native evidence never clears account/model refusals or establishes browser identity, CLI
account binding, billing dates, or future model-turn success. Because the optional MCP parameter
writes this diagnostic cache, `capabilities` is not advertised with a read-only annotation; its
default call is cached-only. Dashboard Overview and Workers GETs read cached task/native/doctor
data, start no diagnostics, and tolerate older task schemas without migrating them. The CLI's
default `providers` status path also uses the read-only store. Schema 6 adds the native diagnostic
cache; schema 7 adds recovery permits; schema 8 adds durable quota windows, authentication-context
binding, and shared post-reset retry claims without changing task ownership or provider binding.
Schema 9 adds nullable per-turn native-overage metadata, bounded admission claims, and safe
native observations. Historical billing remains unknown. [Native extra usage](native-overage.md)
adds exact-profile standing policy and a same-session quota continuation; account settings
remain the spending authority. Native-overage service success preserves included-quota evidence.

## What isolation is, and is not

Each task gets its own detached git worktree under the state directory, created from the
repository's HEAD, never checked out on a branch you use. The agent process is a transient systemd
user unit with a memory ceiling, and its environment is built by allowlist — a name reaches it only
because TaskSpindle put it there, so it inherits no credentials, no proxy settings and no agent
configuration beyond what its profile declares.

Two things make the permission gate real rather than advisory, one per first-class provider:

- **Claude sessions are put in a mode.** The adapter starts every session in whatever
  `permissions.defaultMode` the operator's own Claude settings name — `bypassPermissions` on a
  machine that runs Claude Code that way — and in that mode `session/request_permission` is never
  sent. So after `session/new` or `session/load`, a worker calls `session/set_mode`: `plan` for a
  consult or a review, where every write and every shell command is refused by the agent itself,
  and `default` for an implement, where each one is sent to TaskSpindle's permission policy to
  decide. An agent that refuses the mode fails the turn with `MODE_UNAVAILABLE` rather than running
  unguarded.
- **Grok read-only turns run in its `read-only` sandbox.** `grok --sandbox read-only` makes the
  kernel (Landlock) refuse every write outside `~/.grok` and the temp dirs, the worktree included:
  a file write or a shell redirect fails with `Permission denied` and the agent reports the block
  (its `--permission-mode` and `--deny` flags did nothing on the ACP endpoint, verified on 1.0.13).
  A consult or review is launched with it, an implement is not. The earlier `strict` profile was
  wrong twice over: it allows writes inside the cwd, and on WSL its read set omits `/mnt/wsl`, the
  target of the `/etc/resolv.conf` symlink, so the sandboxed agent's DNS fell back to `127.0.0.1`
  and a resumed session's first model request failed about half the time.

**That is containment by construction, not an OS security sandbox.** The agent runs as your user
with your filesystem permissions. It can read anything you can read, and it can write outside its
worktree if it tries. TaskSpindle detects a write to the root repository and reports it as
`ROOT_MUTATION`; it does not, and cannot, prevent one. If you need a real boundary, run TaskSpindle
in a VM or a container.

### Grok continuation measurement (2026-09-04)

Grok 1.0.13 reported per-turn usage for a TaskSpindle consult followed by two `continue_task`
turns in the same persisted session: input/output tokens were 14500/34, 14561/32 and 14620/27;
each reported one model call. A direct ACP same-process/reloaded-session comparison also
reported per-turn counts. Keep the raw `acp_turn_completed` counters; subtracting preceding
turns would undercount usage.

Continuation reliability is still unresolved. In a separate direct run using the same ACP
client, `session/load` completed and replayed ten updates, but the following prompt timed out
at 90 seconds. Another fresh-process load succeeded. This narrows the failure beyond the
TaskSpindle orchestrator but does not establish its cause. Publication and activation of 0.2.0
remain on hold pending resolution; the accounting measurement alone does not close that blocker.
