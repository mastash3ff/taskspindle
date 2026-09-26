# Logs and retention

TaskSpindle keeps three logs in the state directory (`$XDG_STATE_HOME/taskspindle/`, normally
`~/.local/state/taskspindle/`). Every process that uses that directory writes to them: the stdio
MCP server, the Docker controller, workers, drain passes and the CLI. Under Docker the runtime
container mounts the state directory at the same absolute path, so the files are the same on the
host and in the container.

| File | What it holds |
| --- | --- |
| `logs/taskspindle.jsonl` | the operational log: one JSON record per decision or failure |
| `server.log` | the full traceback of an `INTERNAL` tool error, and of a dispatch failure that a tool's own error masked |
| `dispatch-errors.log` | the full traceback of a failed drain pass |

Each traceback block opens with a header naming the time, the component and the event, for
example `--- 2026-09-26T06:38:18.123Z server tool_failed: RuntimeError`. The operational log has a
record with the same timestamp, so you can match the two.

## The operational log

One JSON object per line:

```json
{"ts":"2026-09-26T06:38:18.123Z","level":"warning","component":"recovery","event":"reconcile_action","process":"controller","pid":41,"task_id":"ts_01…","provider":"claude","unit":"taskspindle-worker-ts_01…","from_state":"RUNNING","to_state":null,"reason":"reconcile_failed:UNIT_QUERY_FAILED"}
```

Every record has `ts` (UTC, ISO-8601 with `Z`), `level` (`debug`, `info`, `warning`, `error`),
`component`, `event` and `pid`. `process` names the writing process's role (`server`,
`controller`, `worker` or `cli`) when that process set one. A record adds the optional fields
that apply: `task_id`, `unit`, `provider`, `code`, `exc_type`, a `message` of at most 500
characters, a `traceback`, and a few scalar fields specific to the event. Field values are
capped at 200 characters.

| Component | Events |
| --- | --- |
| `server` | `tool_failed` (an `INTERNAL` error; traceback in `server.log`), `tool_refused` (a coded or validation refusal, with `code` and `tool`) |
| `dispatch` | `worker_dispatched`, `worker_start_failed`, `task_dispatch_failed`, `dispatch_failed` |
| `lease` | `lease_acquired`, `lease_released`; each is written only after its transaction commits |
| `recovery` | `reconcile_action` (task, `from_state`, `to_state`, `reason`), `reconcile_failed` |
| `drain` | `drain_pass` (`started` count; `debug` when nothing started), `drain_failed`, `reconcile_failed`, `dispatch_failed` |
| `controller` | `operation` (`operation`, `ok`, `code`, `duration_ms`, `socket`, `unit`), `operation_crashed`, `admission_set`, `maintenance_acquired`, `maintenance_released`, `controller_started`, `controller_stopping` |
| `worker` | `worker_started`, `worker_settled` (`state`, `reason`, `code`), `worker_crashed`, `worker_exited` |
| `cli` | `gc_applied` |
| any | `repeats_suppressed`, `failure_cleared` (see below) |

The default threshold is `info`. Set `TASKSPINDLE_LOG_LEVEL=debug` in a process's environment to
also record the controller's successful read-only polls (`admission`, `status`, `show`, which run
on every dispatch) and idle drain passes. A failed poll is a `warning` at any threshold.

### What never goes in

Records never contain prompts, agent output, transcripts, environments, provider argv or
credentials. The only free text is a fixed message chosen in the code, or the message of an
error TaskSpindle created itself (one with a `code`). Any other exception contributes only its
class name. When a record carries a `traceback`, it lists the file, line and function of each
frame and then the class name. It leaves out both the exception's text and the source lines. The
controller records crashes this way, because SDK exceptions can carry request arguments.
`server.log` and `dispatch-errors.log` keep the full Python traceback. That was already the case
before these logs were added, and those files stay local and private (`0600`).

### Repeated failures

A failure that repeats on every call, such as a broken reconcile under a tool or a drain pass
that fails every few seconds, is logged in full once. Identical repeats within the next 15
minutes are only counted. Failures count as identical when they have the same class, `code` and
raising line. Deduplication is per scope: `tool:<name>`, `dispatch`, `reconcile`, `drain`, and
`reconcile:<task_id>` for a reconcile decision that keeps coming back unchanged. The count is
logged as a `repeats_suppressed` record (`suppressed`, `reason`) when the failure changes
(`changed`) or reappears after the window (`recurred`). When the same scope next succeeds, a
`failure_cleared` record reports how many repeats went unlogged. The counters live in
`logs/dedup.json` and are shared by every process.

### Rotation

Each of the three files rotates at 5 MiB and keeps three older generations (`.1` is the newest).
The size check, the rename and the append all happen under one `fcntl` lock on `logs/.lock`.
That makes it safe for several processes to append at once. Logging is best effort: a full disk
or an unwritable directory loses the record, never the operation being logged.

### Docker

`taskspindle-controller serve` also writes every record to stderr, so `docker logs
taskspindle-runtime-1` shows what the controller decided. The records include the drain loop,
reconcile decisions and fence changes. The file copy is the durable one. The stdio MCP server
never writes records to its own stdout or stderr, which belong to the MCP transport.

## Reclaiming disk: `taskspindle gc`

A worker's `TMPDIR` is `tasks/<task_id>/tmp`, and one task can leave hundreds of megabytes there.
`cleanup_task` removes it. It does so first, so a cleanup that has to stop at a dirty worktree
still gives the scratch back. `taskspindle gc` reclaims scratch for tasks nobody cleaned up:

```sh
taskspindle gc            # dry run: what would go, what is kept and why, bytes reclaimable
taskspindle gc --apply    # remove it and rotate the logs
taskspindle gc --json     # the same report as JSON
```

`gc` removes only the `tmp` directory of a task in a terminal state (`COMPLETED`, `FAILED`,
`CANCELLED`, `REJECTED`, `ACCEPTED`) that holds no slot lease. It keeps everything else in the
task directory, including `turn-N.json`, `rev-N.diff` and `worker.log`. A task that is still
active, or that the database does not know, is listed as kept with the reason. Each directory is
checked again and moved aside before removal, so a continuation that starts meanwhile gets a
fresh `tmp`. `COMPLETED` and some `FAILED` tasks can still be continued, and a later turn simply
starts with an empty `tmp`. A symlinked `tmp` is unlinked, never followed. `gc` also rotates any log over the cap
and prunes generations beyond the third. It never touches worktrees, which `cleanup_task` owns,
or containers, which the Docker backend owns.

The command resolves paths only from the state directory. It behaves the same on the host and
inside the runtime container (`docker exec taskspindle-runtime-1 taskspindle gc`).
