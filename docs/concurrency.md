# Provider concurrency

TaskSpindle defaults to one active turn per provider. How many a provider may run at once is
set in two places, and they compose:

```toml
# config.toml: the fallback, and the ceiling. The dashboard never writes this file.
[concurrency]
claude = 4
grok = 4
agy = 4

[capacity]
per_provider_max = 8   # 1-16, default 8
total_max = 12         # 1-48, default none
```

| Source | Set with | Effect |
| --- | --- | --- |
| `providers.<id>.max_concurrent` in the [dispatch policy](dispatch-policy.md) | the dashboard's Policy page, `taskspindle policy set`, or a `policy preset` | The provider's limit, capped by `[capacity] per_provider_max`. Read at every dispatch, so it applies to the next one in every connected process with no restart. |
| `[concurrency]` | editing `config.toml` | The limit wherever the policy sets none; an omitted provider stays at one. Used as written. Read when a process starts. |
| `max_concurrent_total` in the policy, `[capacity] total_max` | as above | Slots the whole pool may hold across providers; the lower of the two applies. |

`[capacity]` exists because the policy can be edited from the dashboard, which may be reachable
from the LAN, and the file cannot: it bounds what a policy edit can do to the host. Each worker
may use up to 3 GiB, so `total_max` is what keeps the pool inside the machine's memory;
`taskspindle doctor` reports the exposure as `capacity_memory`. An upgrade changes nothing
until `max_concurrent` is set: the limits stay whatever `[concurrency]` says.

Keys identify loaded provider profiles; an unknown profile or an invalid limit is rejected.
These are scheduling limits, not a claim about subscription quota. Provider refusals remain
authoritative, and usage reports never infer unreported remaining quota. Raise a limit in
steps you have qualified on this host, watching `availability`: only Claude reports a usage
window, so for Grok and AGY the first sign of too much is a refusal.

## Queue drain

A task that finds no free slot is queued, not refused. It used to start only when some later
tool call ran a dispatch, so a slot a worker had just given back sat empty until the
coordinator next spoke to the server. Now whichever process can start workers drains the queue:
under the Docker backend the controller runs a pass every `interval_s`, and under the systemd
backend the exiting worker runs one after its turn is recorded.

```toml
[dispatch]
drain = true     # default
interval_s = 2   # 1-60, the controller's loop
```

A pass is the ordinary dispatch: the same leases, limits, provider availability and admission
fence. It starts only tasks the coordinator already queued. An idle pool costs one read-only
query per interval; a pass that starts nothing backs the loop off to at most a minute. A drain
failure is logged to `dispatch-errors.log` in the state directory and can never fail the turn
that triggered it. `taskspindle dispatch` runs one pass by hand. Note the consequence: queued
work now starts unattended, including as soon as the admission fence reopens.

## What was used

`lease_history` keeps one row per slot hold, so occupancy survives the lease. From it,
`dispatch_policy(action="status")`, `usage_report`, `taskspindle policy show --status`, the
dashboard and `doctor` report per provider: slot time used over slot time offered, turns that
held a slot, peak active, how many acquisitions took the last slot, tasks queued now, and how
long queued tasks waited for a slot. A provider at its limit with work waiting is where a
higher `max_concurrent` would be used; `doctor` says so as an advisory `capacity_<provider>`.

## Shared scheduling and isolation

All MCP processes connected to the same SQLite state database share capacity. Windows clients
connected through WSL must use the same Linux runtime, configuration, database, and systemd user
as the WSL clients. Four jobs for each built-in provider permit up to twelve external turns
across those clients. Native Codex workers have their own separate limits.

The `capabilities` result exposes each provider's `capacity.limit`, `capacity.active` and
`capacity.available` (already reduced by the pool total), with `source` (`policy` or `config`),
`ceiling` and `queued`, plus `limits.concurrent_turns_total`. The scalar
`concurrent_turns_per_provider` remains a conservative compatibility value; capacity-aware
clients use the provider fields. `execution` identifies the backend platform,
path namespace, state directory, config file, and WSL distribution when available. Repository
paths sent to a Linux backend must use its Linux path namespace.

One short SQLite write transaction counts existing provider leases and claims a task-specific
lease. The task ID is unique, so competing MCP processes cannot start duplicate turns or exceed
the configured provider limit. Queue dispatch rechecks provider availability and takes capacity
before starting the unit. Lower limits prevent new acquisitions without canceling running work.

Provider availability is checked in the same selection path: a provider whose last turn was
refused is not dispatched to until its `eligible_at`, and the tasks queued for it wait. Leases
are counted per profile id while a refusal is recorded per seat, so two OAuth profiles on one
seat each bring their own slots to it; `doctor` calls that out as `capacity_shared_<family>`.

Each worker has its own worktree or scratch repository, systemd unit, process, session, temporary
directory, stderr, transcript, and turn usage record. Continuation uses that task's session and
worktree and claims its next turn separately. Completion, cancellation, and recovery release the
exact task lease; a sibling worker keeps its own capacity and outputs.

The coordinator chooses useful independent assignments and refills ready work as capacity becomes
available. Free slots do not create a requirement to invent work. Existing repository grants,
provider-family review independence, full-diff inspection, candidate acceptance, and integration
requirements still apply.

## Provider health and shared files

A worker records the account status before provider startup. Its successful completion can clear
only that unchanged observation, with the comparison and update in one SQLite transaction. An
older successful job therefore cannot clear a newer sibling's throttle or authentication failure.
A cancelled turn is not evidence of account recovery. Rejected rate-limit telemetry records the
actual rejected window even when later telemetry describes a different allowed window.

Grok's fixed compatibility overlay is left untouched when its content and private mode already
match. Changes use a private temporary file and atomic replacement in the same directory, so
concurrent readers see a complete old or new file. Target symlinks are refused and file mode
remains `0600`; an unchanged file with loose permissions is repaired without rewriting it.

Concurrency does not change provider models, adapter versions, OAuth roots, upstream credential
locks, or AGY's read-only credential mount. Credentials are neither copied into per-task roots nor
used to serialize entire jobs. A throttle or authentication failure prevents further eligible
dispatch while healthy jobs already running may finish; TaskSpindle does not silently retry on a
different provider or switch to metered authentication.

## Upgrade and rollback

Before activating the new runtime, drain provider jobs and stop connected MCP processes and
workers. Preserve the existing runtime, configuration, and database backup. Schema 4 converts the
provider-keyed lease table to task-specific leases; ordinary upgrades preserve existing lease
records, task history, and repository grants. Restart all clients with the reviewed runtime and
the same configuration. Qualify one, two, and then four overlapping jobs per selected provider
before activating four as the managed limit.

To reduce concurrency, lower `max_concurrent` in the policy: every connected process reads it at
its next dispatch, and existing workers finish normally. A change to `[concurrency]` or
`[capacity]` in `config.toml` is read when a process starts, so restart the connected MCP
processes after editing the file, and do not run mixed configurations against the shared
database and assume the lowest value will govern other clients.

There is no in-place schema downgrade for the lease table. To roll back the runtime after this
upgrade, restore the pre-upgrade backup; see [rollback.md](rollback.md).
