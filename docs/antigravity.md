# Antigravity

Provider `agy` uses the native Antigravity CLI for consult, review and implementation.
It reuses the Google login established by interactive `agy`. The official ACP
server is a different component with a separate login; it is not the native worker's
transport. Source integration remains pending release qualification until all live
checks below pass.

## Installation and authentication

Install and sign in to the official CLI, then confirm it meets the qualified minimum version:

```sh
taskspindle setup --provider agy
taskspindle auth agy
```

There is no private copy: workers run whichever `agy` TaskSpindle resolves from `PATH` (the
same one an interactive login already runs), falling back to `~/.local/bin/agy` if PATH does
not have one. `TASKSPINDLE_AGY_SOURCE` is an explicit override of that resolution -- set by the
codex-runtime deploy script, for example -- but it is never required.

TaskSpindle enforces a minimum version (`AGY_MIN_VERSION`) and refuses an older CLI outright. A
CLI newer than the last build TaskSpindle was actually tested against (`AGY_TESTED_MAX`) is
still accepted, not refused -- a vendor release is a one-line constant bump here, not an
outage -- and is reported as an advisory note by `taskspindle doctor` rather than a failure.
`taskspindle setup --provider agy` re-runs this check; it copies nothing and pins no version
of its own. TaskSpindle forces `AGY_CLI_DISABLE_AUTO_UPDATE=true` for version/catalog checks and
inside each worker namespace, preventing the CLI's own background updater from replacing the
resolved binary mid-check or mid-turn.

Authentication checks query the catalog with cached native
credentials; they never open the task database or submit a model prompt. If the
cache is unavailable, run interactive `agy` in the same WSL account to sign in.

Credentials remain at their original native path and are mounted read-only into
private worker state. No tokens are copied into TaskSpindle, no API key is
forwarded, and no account or provider is substituted after an auth/quota error.
D-Bus and runtime socket environment variables are excluded. A worker's
isolated launch mounts the resolved binary's real companion directory
(`~/.gemini/antigravity-cli/bin`) read-only, exactly as it mounts the binary itself.

## Models and lifecycle

New tasks default to Gemini entries from the authenticated CLI catalog. Numeric
release order determines the newest release; Flash wins release ties and Medium
effort is preferred when offered. An explicit model may be a Gemini ID or any
other exact advertised ID (Claude Sonnet/Opus and GPT-OSS). Explicit
model/effort overrides must be advertised.
The exact resolved model and effort persist across continuations and repairs.
Requested, resolved and backend-reported identities remain distinct; CLI launch
configuration is not evidence of a reported backend model.

The native worker reads bounded NDJSON and requires one terminal result and a
successful process exit. Each continuation uses its exact saved conversation ID
and private task state. A missing or mismatched session fails instead of silently
starting another conversation. Partial responses and session IDs survive failure,
timeout and cancellation. Native cumulative usage is converted to per-turn deltas;
unknown counts, provider quota totals and reset times remain unknown.

## Permissions and isolation

The CLI stream does not support ACP permission callbacks. TaskSpindle supplies
immutable native settings and a restricted primary-agent tool list within a Linux
mount namespace. Global and workspace customization locations are masked, and
each task has its own CLI conversations and project metadata. Personal terminal
settings and other CLI conversations are not inherited or modified.

Consult/review expose read tools and deny file writes, shell execution, MCP, browser
actions and delegation. The filesystem is read-only outside explicitly writable
private runtime state. Implementation adds file-editing tools only for its declared
worktree scope. Git and control paths in directories present at launch are masked
immediately: a `.git` directory is bound to an empty directory and a linked worktree's
`.git` pointer file to an empty file, so the model never sees the main repository's
gitdir path, which the sandbox would refuse. Git runs only on the host, before and after
the turn. Controls inside newly created nested directories are masked before a
resumed turn. Before creating a candidate, TaskSpindle rejects any changed path
containing a Git or agent-control component, including those in new directories.
Declare an existing containing directory when a new file is to be created.
Shell tools are unavailable to the model;
TaskSpindle executes the declared verification commands after editing and passes
failures into the existing repair workflow.

Settings, custom-agent definitions and mounts are regenerated from task policy on
every turn. A policy setup failure refuses the task. These controls must be qualified
against the actual native backend, including implicit tools, inherited hooks/MCP,
exact-session resume and process-group cancellation; observing a model's refusal
alone does not prove containment.

## Reviews and local release

Reviewer selection remains explicit. Any different built-in provider may review a
candidate, while self-review and same-family aliases are refused. Immutable family
provenance is rechecked during dispatch, continuation, review, acceptance and manual
integration. Full-diff receipts, candidate binding, dispositions and Codex-controlled
acceptance remain required.

Before release, run lint, the full fake-provider suite, packaging and real MCP stdio
checks. Exercise consultation, continuation, policy denial, implementation, both
directions of cross-review, full diff inspection, acceptance and cleanup in a
disposable repository under the actual systemd environment. Obtain an independent
TaskSpindle review and resolve findings. Failed policy qualification leaves the old
runtime active.

Schema 3 adds resolved model/effort and immutable provider family. Wait for active
tasks and take a consistent SQLite backup before the new runtime opens the live
database. Provision a reviewed local commit into a separate runtime and retain the
old runtime with its matching database backup. Preserve existing grants and revoke
temporary rollout grants.

Upstream references: [headless CLI](https://antigravity.google/docs/cli/headless/),
[permissions](https://antigravity.google/docs/cli/permissions/),
[authentication](https://antigravity.google/docs/cli/install/).

### Explicit namespace runtime

The sandbox starts with an empty root and a synthetic HOME. It mounts only system
executable directories, architecture libraries, the current Python standard library,
shared data and locale paths, the selected executable, approved companion directory,
and task policy/state leaves. DNS configuration is flattened from its resolved file
(including WSL's symlink target); hosts, the CA bundle and loader cache are explicit
read-only files. Network access remains available. Host home, task root, `/mnt`,
Docker mount trees and host sockets are not inherited. Private `/tmp` and `/var/tmp`
remain writable. The original token remains the same read-only inode.

Every retained directory is checked for nested mounts before launch. Runtime trees
are split around and exclude mounted descendants; 64 descendants is a planning
limit, never authorization for an otherwise unapproved mount. Workspace, state and
companion sources with nested mounts fail closed. A Python verifier inside the
completed private namespace checks source identities, read-only flags and unexpected
descendant mounts before replacing itself with the selected executable. Root stays
read-only and the executable keeps the transport process group for cancellation.

`agy_cli_policy.smoke_launch(prepare_launch(...))` runs that verifier and synthetic
Python checks with a ten-second deadline, without executing AGY, reading credentials
or contacting a server. It reports elapsed time, namespace mount count, CA loading
and localhost resolution. Launch metadata records host/retained mount counts and
excluded runtime mounts. These checks prove namespace startup, not provider login,
inference or every optional companion's loader compatibility. An executable needing
nonstandard libraries outside the explicit runtime requires a reviewed runtime
extension; the launcher never falls back to exposing the host root.

A large Docker Desktop mount inventory can make recursive root binds expensive.
The launcher avoids those binds; it does not delete mounts or change host namespace
settings. Mount count alone does not establish stale mounts or their owner. Any host
cleanup requires a separate inventory and authorization.

The final verifier also rejects mounts anywhere outside the complete mount allowlist
and rechecks writable scopes for hardlinks immediately before execution. Scope data
remains a live bind of a coordinator-owned worktree: the coordinator must not mutate
its filesystem topology concurrently with a worker. These checks reject changes made
between planning and verification; they do not claim to defend against a hostile host
process continuously modifying the worktree after verification. That stronger boundary
would require private writable staging and controlled writeback.


Administrative executable directories (`/usr/sbin` and a real `/sbin`) are omitted
from the worker runtime. Docker's `--init` injects `/usr/sbin/docker-init` into the
outer container; the worker neither needs that executable nor inherits its mount.
Runtime splitting excludes mounted files as well as mounted directories, while
retaining the existing 64-mount planning limit.
