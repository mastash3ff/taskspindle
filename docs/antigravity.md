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
The ordinary user D-Bus environment is included in the environment allowlist. A worker's
isolated launch also mounts the resolved binary's real companion directory
(`~/.gemini/antigravity-cli/bin`) read-only, exactly as it mounts the binary itself.

## Models and lifecycle

New tasks select only Gemini entries from the authenticated CLI catalog. Numeric
release order determines the newest release; Flash wins release ties and Medium
effort is preferred when offered. Explicit model/effort overrides must be advertised.
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
worktree scope. Git and control paths in directories present at launch are protected
immediately; controls inside newly created nested directories are masked before a
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
