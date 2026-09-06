# Provider concurrency

TaskSpindle defaults to one active turn per provider. After qualifying overlapping jobs, set
positive integer limits in its existing configuration:

```toml
[concurrency]
claude = 4
grok = 4
agy = 4
```

An omitted provider stays at one. Keys identify loaded provider profiles; an unknown profile or
an invalid limit is rejected. These are scheduling limits, not a claim about subscription quota.
Provider refusals remain authoritative, and usage reports never infer unreported remaining quota.

## Shared scheduling and isolation

All MCP processes connected to the same SQLite state database share capacity. Windows clients
connected through WSL must use the same Linux runtime, configuration, database, and systemd user
as the WSL clients. Four jobs for each built-in provider permit up to twelve external turns
across those clients. Native Codex workers have their own separate limits.

The `capabilities` result exposes each provider's `capacity.limit`, `capacity.active`, and
`capacity.available`. The existing scalar limit remains a conservative compatibility value;
capacity-aware clients use the provider fields. `execution` identifies the backend platform,
path namespace, state directory, config file, and WSL distribution when available. Repository
paths sent to a Linux backend must use its Linux path namespace.

One short SQLite write transaction counts existing provider leases and claims a task-specific
lease. The task ID is unique, so competing MCP processes cannot start duplicate turns or exceed
the configured provider limit. Queue dispatch rechecks provider availability and takes capacity
before starting the unit. Lower limits prevent new acquisitions without canceling running work.

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

To reduce concurrency, set the limits back to one and restart the connected MCP processes so
they all use the same limits. Existing workers finish normally. Do not run mixed configurations
against the shared database and assume the lowest value will govern other clients.

Runtime rollback also needs the previous lease schema. After draining work, stopping all MCP
processes and workers, and backing up the current database, run this command with the newer
runtime and the explicit existing database path:

```sh
taskspindle rollback-concurrency --database /absolute/path/to/taskspindle.sqlite3
```

The command requires schema 4, no leases or active tasks, no ambiguous recovery tasks, and no
integration journal entries. It reverses only the lease migration in one transaction and
preserves task history and grants. Then start only the previous runtime: starting the newer
runtime again upgrades the schema. Do not restore an old database backup over newer task history.
