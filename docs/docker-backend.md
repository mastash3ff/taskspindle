# Docker execution authority

The persistent controller owns Docker access. MCP and web use private Unix sockets;
one-shot worker and accept containers have no Docker or controller socket mounts.
The controller launches only explicit requests. It does not dispatch queues.

```text
MCP --- jobs.sock ---> controller --- Docker Engine ---> worker / accept
web --- doctor.sock -> controller --- Docker Engine ---> diagnostic
```

Keep both sockets outside every worker bind source. The controller restricts socket
mode to 0600 and checks peer UID. Separate controllers for the same state directory
are refused by a file lock. Clients and controller must run as the same UID.

## Configuration

```toml
[execution]
backend = "docker"
jobs_socket = "/run/taskspindle/jobs/jobs.sock"
diagnostics_socket = "/run/taskspindle/diagnostics/doctor.sock"
# Replace with the locally built image ID; tags are deliberately refused.
worker_image = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
user = "1000:1000"
mounts = [
  {source = "/absolute/taskspindle/state", target = "/absolute/taskspindle/state", read_only = false},
  {source = "/absolute/taskspindle/config.toml", target = "/absolute/taskspindle/config.toml", read_only = true},
  {source = "/absolute/repository", target = "/absolute/repository", read_only = false},
]
# Optional reviewed namespace profile; AGY and Grok workers and probes use it.
seccomp_profile = "/opt/taskspindle/src/taskspindle/_container/agy-seccomp.json"

[execution.provider_mounts]
claude = [{source = "/absolute/claude-auth", target = "/absolute/claude-auth", read_only = true}]
grok = [{source = "/absolute/grok-auth", target = "/absolute/grok-auth", read_only = true}]
agy = [{source = "/absolute/agy-auth", target = "/absolute/agy-auth", read_only = true}]
```

Configure only the required auth paths, including writable auth paths if the
provider requires refresh. Common mounts must contain state, selected config, and
the repositories needed by jobs; do not mount a broad home directory containing
all provider credentials. Every bind preserves its absolute host path, and every
source must already exist. A readonly SQLite query resolves each task's actual
provider family. Accept containers receive no provider mounts. Images must exist
locally: the controller never pulls images or requests registry authentication.

The image supplies Python, provider CLIs, and the adapter. Worker PATH is
`/opt/taskspindle/.venv/bin:/usr/local/bin:/usr/bin:/bin`; XDG_DATA_HOME is
`/opt/taskspindle/data`. Host virtualenv paths are not executed inside jobs.
`TASKSPINDLE_WORKER_CONTAINER=1` selects the worker-local diagnostic behavior.

| Resource or security control | Docker setting |
| --- | --- |
| Hard memory limit | 3 GiB |
| Memory reservation | 2 GiB; not an exact systemd MemoryHigh equivalent |
| Memory plus swap | 3.5 GiB, allowing 512 MiB swap |
| Stop grace | 30 seconds, then entire-container termination |
| Restart policy | no |
| Capabilities | all dropped |
| Privilege escalation | no-new-privileges |
| Root filesystem | readonly; bounded writable /tmp |
| Seccomp | Docker default; optional reviewed namespace profile for AGY and Grok |

## Launch and recovery

Private, atomically replaced records under `state_dir/execution` map logical unit
names to Docker IDs, an unpredictable launch generation, and task turn or accept
journal identity. Records never contain argv or environment values. A reservation
is fsynced before Docker receives create/start. Lost replies trigger inspection
using the deterministic container name. An already observed job is never started
again. Controller restarts reuse these records and never invalidate a worker on
account of controller generation.

```text
persist intent -> create -> persist Docker ID -> start -> observe terminal state
      |             |                              |
      +-------------+------------------------------+--> uncertain: retain lease/journal
```

`UNIT_START_FAILED` means no process was launched. `UNIT_START_UNCERTAIN` means a
launch may exist and reservations must remain intact. Engine outages never report
`not_found`. Created-but-unconfirmed containers remain uncertain. Terminal OOM,
exit, and signal evidence is persisted before Docker cleanup and survives it.
A later turn uses a new generation; prior evidence is archived. A prior turn still
running produces `UNIT_PREVIOUS_TURN_ACTIVE`, so the new turn remains queued. No prompt or
integration action is replayed autonomously.

## Controller commands

`taskspindle-controller serve` reads `TASKSPINDLE_CONFIG` and serves both sockets.
`--jobs-socket` and `--diagnostics-socket` override their configured paths. The
same executable offers `status`, `fence`, `open`, `reconcile`, and `interrupt-workers` clients.
Success is JSON and exit 0; failures are bounded JSON errors and exit 1.

Status reports `backend`, `admission_open`, `engine_reachable`, active or uncertain
`jobs` (`unit`, `kind`, `task_id`, `state`), `unsettled_integrations`,
`active_reservations`, and `nonterminal_worker_tasks` (counts are null when
the database cannot be inspected). Integration counts include ACCEPTING task rows
even if their journal is missing. It excludes argv and environment. An unknown
engine or journal inventory is not evidence that shutdown is safe.

Fencing is serialized with launch reservations and persisted across restarts.
A fenced launch returns `UNIT_ADMISSION_CLOSED`. `interrupt-workers` fences first,
refuses any active or unsettled accept/journal, and stops workers with the full
grace period. It then runs one bounded worker recovery sweep to settle interrupted
tasks and leases, and reports the resulting status. Recovery never dispatches queues.
The explicit `reconcile` operation requires a closed fence, a reachable engine, no
active or unknown jobs, and no unsettled integration. `fence` calls this operation
after closing admission; active workers remain running and are reported for the
lifecycle helper to refuse shutdown. Both paths retain the fence across failures.
Safe stack shutdown requires empty jobs and zero integration, reservation, and
nonterminal worker counts. Stopping the controller alone does not stop independent jobs.

The diagnostics socket accepts only `doctor` with a boolean `live` argument. Each
provider probe uses the worker image, security settings, selected auth mounts,
readonly config, and a private tmpfs in place of live state. Probes have bounded
runtime/output, do not participate in leases, and remove their finished diagnostic
containers. The jobs socket does not expose arbitrary commands or diagnostics.

The [official Docker SDK container reference](https://docker-py.readthedocs.io/en/stable/containers.html)
defines memory-plus-swap as the "Maximum amount of memory + swap a container is
allowed to consume." Docker memory reservation is a soft limit, not MemoryHigh.
