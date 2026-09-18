# Tool reference

Eighteen tools, one envelope. Everything a Codex session can ask TaskSpindle to do is here, and
nothing else is: there is no side channel, no implicit action and no tool that decides on a
candidate's behalf.

## The envelope

Every tool returns the same shape whether it succeeded or not, so a caller never has to tell an
exception from a result:

```json
{"api_version": 1, "ok": true,  "result": { }, "error": null}
{"api_version": 1, "ok": false, "result": null,
 "error": {"code": "GRANT_MISSING", "message": "…", "retryable": false, "details": { }}}
```

`code` is stable and is what you should branch on; `message` is for a human. An error TaskSpindle
did not anticipate comes back as `INTERNAL` with nothing but the exception's class name in
`details`; its traceback goes to `state_dir/server.log`, not into the conversation, where it would
leak paths and arguments.

Read-only tools are annotated `readOnlyHint: true`. Seven are: `doctor`,
`list_repository_policies`, `list_tasks`, `task_status`, `task_result`, `usage_report`, and
`dispatch_policy`.
**`task_diff` is not one of them**, and that is deliberate: handing a page of a diff over appends a
receipt that later proves the whole candidate was inspected. `capabilities` also lacks the hint
because its optional `check_providers` parameter writes a bounded native diagnostic cache. Calling
`capabilities` without that parameter remains cached-only and writes nothing.

## Discovery

### `capabilities`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `check_providers` | string[]\|null | `null` | refresh bounded native diagnostics for these configured profile IDs |

What you need to compose a valid request, and nothing about any task. The default call reads only
stored task outcomes, capacity, usage windows, and native diagnostic cache entries. With
`check_providers=["grok"]`, it may run and cache a session-free native Grok quota check before
returning.

```json
{"dispatch_policy": {
   "revision": 3, "fingerprint": "…", "updated_at": "…Z", "updated_by": "web",
   "source": "store", "document_error": null, "share_window": "week",
   "roles": {"planner": {"brief": "…", "provider_preference": ["claude", "grok", "agy"],
             "selections": {"claude": {"model": "opus[1m]", "effort": "xhigh"}}, "timeout_s": null}},
   "under_target_order": ["grok", "agy"]
 },
 "providers": [{"id": "grok", "first_class": true, "second_class": false, "auth": "oauth",
                "modes": ["consult", "implement", "review"], "model": null, "gateway_host": null,
                "policy": {"enabled": true, "state": "active", "enforced_exhaustion": false,
                           "target_share": 30, "target_share_normalized": 0.3,
                           "share_state": "under_target", "observed": {"…": "…"},
                           "budgets": {"…": "…"}, "allowed_modes": null, "note": "",
                           "advertised_models": ["grok-4.6"],
                           "advertised_efforts": ["low", "medium", "high"],
                           "models_without_effort": []},
                "availability": {"state": "throttled", "reset_at": "2026-09-04T21:00:00Z",
                                 "eligible_at": "2026-09-04T21:00:00Z",
                                 "reason": "The provider reported a usage limit."},
                "windows": [{"window": "five_hour", "status": "rejected", "used_percent": 100.0,
                             "resets_at": "2026-09-04T21:00:00Z", "source": "throttle_error"}],
                "native_check": {"state": "quota", "source": "grok_billing",
                                 "version": "1.0.13", "used_percent": 25.0,
                                 "window": "weekly", "period_start": "…", "reset_at": "…",
                                 "freshness": "fresh", "eligible_hint": true,
                                 "account_binding": "unverified"}}],
 "modes": ["consult", "review", "implement"],
 "versions": {"taskspindle": "0.3.0", "api": 1, "schema": 6,
              "adapter_package": "@agentclientprotocol/claude-agent-acp",
              "adapter_version": "0.70.0", "acp": "0.12.0"},
 "limits": {"timeout_s": [60, 14400], "diff_page_bytes": 16384, "diff_page_max_bytes": 262144,
            "concurrent_turns_per_provider": 1},
 "states": ["PREPARING", "…"], "cleanup_states": ["RETAINED", "…"],
 "isolation": "…what isolation does and does not mean…"}
```

Read the `isolation` string before you trust anything to it. It says plainly that worktrees and
allowlisted environments are containment by construction and not an OS sandbox.

The top-level **`dispatch_policy`** block is the operator's dispatch policy: `revision`,
`fingerprint`, `updated_at`, `updated_by`, `source` (`store` or `defaults`), `document_error`,
`share_window`, the `roles` table (`brief`, `provider_preference`, `selections`, `timeout_s` per
role) and `under_target_order`. `source` is `defaults` when nothing has been saved or the stored
document no longer parses; `document_error` then says why. Each entry in `providers` carries a
matching per-provider **`policy`** block: `enabled`, `state` (`paused`, `budget_exhausted` or
`active`), `enforced_exhaustion`, `target_share`, `target_share_normalized`, `share_state`
(`paused`, `untracked`, `under_target`, `on_target` or `over_target`), `observed` and `budgets` per
window, `allowed_modes`, `note`, `advertised_models`, `advertised_efforts` and
`models_without_effort`. It is read fresh on every call — an edit made in the dashboard or with
`taskspindle policy` takes effect without restarting the MCP server. None of this selects a
provider or changes admission by itself; see [dispatch-policy.md](dispatch-policy.md) for the full
shape and [`dispatch_policy`](#dispatch_policy--read-only) below for the dedicated tool.

`availability` is the one part of this answer that changes, and it is deliberately small: `state`
(`ok`, `throttled`, `auth_expired`, `access_denied`, or `model_unavailable`), `reset_at` (the
provider's own reset time, when it gave one), `eligible_at` (when the provider is eligible again —
`reset_at` if there is one, otherwise fifteen minutes after the refusal was observed), and `reason`
(a fixed, safe sentence for the state; a model-scoped refusal names the model here). A provider with
no recorded refusal is simply `"state": "ok"`. Nothing else is in this object, and nothing is chosen
for you: see [`start_task`](#start_task) for what a refused provider does to a request, and
[architecture.md](architecture.md#provider-availability) for the one rule behind it. `windows` is
the newest observation of each usage window, as far as the agent reports them — informational only;
nothing gates admission on it.

Every provider includes a normalized `native_check`. Stable fields include `state`, `source`,
`version`, `checked_at`, `last_attempt_at`, `last_success_at`, `used_percent`, `window`,
`period_start`, `reset_at`, `freshness`, `eligible_hint`, `checking`, `error_code`, `detail`, and a
bounded `last_success`. Null fields remain explicit. This is an on-demand, account-unbound probe of
what the CLI itself reports; it is entirely separate from `availability` and never gates admission —
it exists so a caller can ask "is this CLI logged in, and what does its catalog look like" before
starting work, the same way `doctor` does.

The Grok check uses only the installed OAuth CLI's ACP billing extension. It starts no model turn,
login, browser, or direct HTTP request; reads no token contents; and does not upgrade the CLI. A
persistent cache shared by OAuth aliases of the same provider account coalesces calls for five
minutes. Executable, authentication-mode, and relevant configuration/auth-file metadata changes
invalidate it; model and effort selection do not split the account quota. Unsupported native
methods are reported as `unsupported` without another transport.

### `doctor` — read-only

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `live_probes` | bool | `true` | run the two checks that start a process |

Returns `{"ok": bool, "checks": [{"name", "ok", "detail", "advisory"}]}`. `ok` ignores advisory
checks. Same checks as the `taskspindle doctor` command.

### `dispatch_policy` — read-only

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `action` | `get`\|`status` | `get` | which projection to return |

`get` returns `{"policy", "revision", "fingerprint", "updated_at", "updated_by", "source",
"document_error"}` — the same document `capabilities()` reads. `status` adds `status` (observed
usage against the policy, per [dispatch-policy.md](dispatch-policy.md#status)) and
`file_managed: {"config_file", "concurrency"}`, the `config.toml` tables the dashboard shows
read-only alongside the policy.

There is no `set` through MCP: the caller being steered does not rewrite its own steering. The
policy is edited only through the dashboard's Policy page or `taskspindle policy`; see
[dispatch-policy.md](dispatch-policy.md#editing).

## Repository policy

A provider may work in a repository only while an active grant covers that repository, that
provider and that mode. Grants are keyed to the repository's canonical identity: the real path of
its common git directory plus its root commit.

### `authorize_repository`

Takes one object parameter, `request`; the fields below go inside it.

| Fields of `request` | Type | Meaning |
| --- | --- | --- |
| `path` | string | any path inside the repository |
| `providers` | string[] | profile ids, which must exist |
| `modes` | string[] | `consult`, `review`, `implement` |

Returns `{"repository_id", "display_path", "grants": [{"provider", "mode", "active", "created_at",
"revoked_at"}]}`. Errors: `INVALID_REQUEST` (not a repository, unknown provider, unknown mode).

### `revoke_repository`

Same parameters, with `providers` and `modes` optional — omit either to revoke every one of them.
Returns the policy plus `"revoked": <count>`. A task already running is never touched.
Errors: `INVALID_REQUEST` when the repository was never authorized.

A repository that no longer exists on disk cannot be named by a path. Name it by the
`repository_id` that `list_repository_policies` shows instead: `revoke_repository` with
`repository_id` and no `path`.

### `list_repository_policies` — read-only

No parameters. Returns `{"repositories": [{"repository_id", "display_path", "common_dir",
"root_commit", "grants": [...]}]}`.

## Starting and watching work

### `start_task`

Verification commands should leave no untracked files; ignore generated files such as
`__pycache__/` in the repository's `.gitignore` to avoid `DIRTY_OVERLAP` on later tasks.

Takes one object parameter, `request`; the fields below go inside it.

| Fields of `request` | Type | Default | Meaning |
| --- | --- | --- | --- |
| `provider` | string | — | a profile id from `capabilities` |
| `mode` | `consult`\|`review`\|`implement` | — | what the task may do |
| `prompt` | string | — | the request itself |
| `repository` | string\|null | `null` | required for `implement` |
| `model` | string\|null | `null` | recorded and, where the agent supports it, requested |
| `effort` | string\|null | `null` | as above |
| `timeout_s` | int | `1800` | 60–14400 |
| `allow_metered` | bool | `false` | required for an `api_key` profile |
| `ignore_provider_status` | bool | `false` | an explicit coordinator override: admit the task even while the provider's last turn is still within its refusal window |
| `acceptance_criteria` | string\|null | `null` | **implement only, required** |
| `path_prefixes` | string[]\|null | `null` | **implement only, required**; repository-relative, no `..`, no leading `/` |
| `verification_commands` | string[]\|null | `null` | **implement only, required**; may be `[]` |
| `candidate_message` | string\|null | `null` | **implement only, required**; one line, ≤ 72 characters |
| `review_target` | object\|null | `null` | **review only, required** |
| `review_kind` | `"standard"`\|`"adversarial"` | `"standard"` | review only; the reviewer's stance, recorded on the task and its review — see [the review contract](#the-review-contract) |
| `role` | string\|null | `null` | matches `^[a-z][a-z0-9_-]*$`, at most 32 characters; recorded on the task for reporting only, does not select a provider or change admission |
| `context_files` | string[]\|null | `null` | absolute paths under the operator's `[context_files]` roots whose contents are copied into the worker's first turn — see [handing over context](#handing-over-context) |

`review_target` is either
`{"kind": "candidate", "task_id": …, "candidate_sha": …}` — review another task's staged candidate —
or `{"kind": "snapshot", "repository": …, "expected_head": …, "paths": [...]}`, which commits the
current working tree to `refs/taskspindle/snapshots/<id>` and reviews that.

#### Handing over context

`context_files` lets the coordinator hand a worker what it already knows — a design note, a
failing log, a transcript excerpt — instead of retyping it into `prompt`. The server reads each
file under the operator's `[context_files]` allowlist (see
[configuration.md](configuration.md#context_files)) and appends them to the first turn after
the prompt, each framed as `--- BEGIN CONTEXT FILE n/N: <path> (<bytes> bytes) ---` … `--- END
CONTEXT FILE n/N ---`, under a header telling the worker they are reference material copied at
dispatch time, possibly stale, and not part of the repository. The exact text is kept as the
task's `context_files` artifact (`state_dir/tasks/<id>/context_files.md`), and the paths are
recorded on the task (`task_status.context_files`). Only the initial turn carries them;
`continue_task` does not repeat them.

Every path is checked before any row is written, so a refusal leaves no task behind. The
request itself must give absolute, canonical paths with no `..` segment. The read then refuses,
as `INVALID_REQUEST` with `details.code` set to one of: `CONTEXT_FILES_DISABLED` (no
`[context_files]` table), `CONTEXT_PATH_DENIED` (not under an allowed root, or unresolvable),
`CONTEXT_FILE_UNSAFE` (a symlink anywhere in the path, not a regular file, more than one hard
link, or a NUL byte), `CONTEXT_FILE_TOO_LARGE` (over the per-file or per-task byte cap) and
`CONTEXT_FILES_TOO_MANY`. Under the Docker backend the server reads from inside the runtime
container, so only paths beneath an `[execution]` mount can ever be handed over.

One call, in full:

```json
{
  "request": {
    "provider": "claude",
    "mode": "implement",
    "prompt": "Add a --json flag to the report command",
    "repository": "/home/you/src/project",
    "acceptance_criteria": "report --json prints one JSON object and exits 0",
    "path_prefixes": ["src/report"],
    "verification_commands": ["uv run pytest -q tests/test_report.py"],
    "candidate_message": "Add a JSON output mode to report",
    "timeout_s": 1800
  }
}
```

Returns `{"task_id", "state", "state_version"}`. The task is created, its worktree is made, its
first turn is composed and it is queued; a worker unit starts if the provider's lease is free.

Errors: `INVALID_REQUEST` (validation, unknown provider, mode not served, a repository with
uncommitted changes inside the task's own path prefixes — `details.code = DIRTY_OVERLAP`),
`GRANT_MISSING`, `METERED_NOT_ALLOWED`, `REVIEWER_NOT_INDEPENDENT`, `CANDIDATE_MISMATCH`,
`TARGET_MOVED`, `PROVIDER_UNAVAILABLE`.

**`PROVIDER_UNAVAILABLE`** is the whole of TaskSpindle's answer to a subscription limit. When the
last turn on a provider was refused for a usage, rate, credit or login reason, the next
`start_task` on that provider is refused too, until the provider's own `reset_at` — or, when it gave
none, fifteen minutes after the refusal was observed. `details` carries the same shape
`capabilities.providers[].availability` does (`state`, `reset_at`, `eligible_at`, `reason`), plus
the per-state `code` (`PROVIDER_THROTTLED`, `PROVIDER_AUTH_EXPIRED`, `PROVIDER_ACCESS_DENIED`, or
`PROVIDER_MODEL_UNAVAILABLE`). The error is `retryable`; once `eligible_at` passes, the provider is
simply eligible again — one ordinary attempt, and if that one is refused too the clock restarts from
the new observation. Nothing is re-queued on another provider.

`ignore_provider_status` is the only bypass: an explicit coordinator override that admits the task
regardless of the provider's current status. There is no other override, and no permit to arm or
claim — a provider that is eligible again needs no override at all.

### `list_tasks` — read-only

Filters `repository_id`, `provider`, `mode`, `state`, all optional, plus `limit` (default 50).
Returns `{"tasks": [<task view>]}`, newest first.

### `task_status` — read-only

`task_id`. Returns the task view: `id`, `state`, `state_version`, `cleanup_state`, `provider`,
`auth_mode`, `mode`, `repository_id`, `base_head`, `target_head`, `branch`, `worktree_path`,
`session_id`, `requested_model`, `reported_model`, `oauth_evidence`, `candidate_sha`,
`candidate_revision`, `changed_paths`, `diff_digest`, `diff_size`, `check_summary`, `warnings`,
`error`, `unit_name`, `heartbeat_at`, and the four timestamps. A task in `RECOVERY_AMBIGUOUS` also
carries `evidence` and `manual_action` — see [recovery.md](recovery.md). Errors: `TASK_NOT_FOUND`.

A review task's view also carries `review_kind`; it is `null` on every other mode.
`context_files` lists the paths handed over at dispatch, empty when there were none.
`task_status` also returns `resume`, the same [native resume handle](#reopening-a-workers-session)
`task_result` carries, or `null` before the worker has opened a session.

`state_version` increments on every transition. Hold on to it: the four tools that change a task
require the version you last saw, and answer `STALE_STATE_VERSION` if something moved underneath
you.

**Telling a stuck worker from a slow one.** `heartbeat_at` alone only proves the worker process is
alive; it advances on a fixed interval whether or not the agent is doing anything. `task_status`
also returns `progress`: the running worker's own `progress.json`, or `null` when the task has no
turn in flight, no such file yet, or the file could not be parsed (never an error). When present
it has `revision` (increments on every rewrite), `phase` (`"prompting"`, `"verifying"`,
`"collapsing"`, or `"settled"`), `updated_at`, `tool_calls`, `tool_call_updates`, `text_chars`,
`thought_chunks`, `permission_requests`, `violations`, `last_tool_title`, and `last_event_at` (the
timestamp of the last agent activity of any kind, which can lag `updated_at`). Alongside it,
`task_status` computes `elapsed_s` (seconds since `started_at`, or `null` before the turn starts),
`heartbeat_age_s` (seconds since `heartbeat_at`, or `null`), `progress_age_s` (seconds since
`progress.updated_at`, or `null` with no `progress`), and `timeout_s` (the task's own timeout).
A heartbeat that keeps advancing while `progress` does not, for longer than a few minutes, is a
stuck worker: cancel it.

### `task_result` — read-only

`task_id`. Returns `{"task_id", "state", "response", "checks": [{"command", "exit_code", "ok",
"duration_ms", "stdout_tail", "stderr_tail"}], "attribution": {"provider", "auth_mode",
"requested_model", "reported_model", "gateway_host", "agent"}, "warnings", "quota_warnings",
"usage", "transcript_locator", "session_id", "resume"}`.

`attribution` makes metered work visible: an `api_key` profile shows `auth_mode: "api_key"`
and the gateway's host, without its URL path or token. `agent` names the adapter and version.
`reported_model` uses the agent's model identity, falling back to the configured profile model
when no identity was reported. Claude's per-turn wire ID wins, then a canonical model ID from ACP
session configuration, then the bounded session-file fallback for unresolved aliases or display
names. Grok uses the first per-turn `_meta.modelId`, then the first `modelUsage` key, then the
profile model. New turn attribution and usage rows use the same choice; raw `modelUsage` and
historical rows remain unchanged.

`warnings` is the task's warning list, as `task_status` shows it. `quota_warnings` is every
usage, rate, credit or login refusal a turn of this task ran into, each as
`{"code", "provider", "status_key", "state", "window", "reset_at", "observed_at", "source",
"message"}`. `usage` is one entry per turn that reported token counts:
`{"provider", "repository_id", "model", "input_tokens", "output_tokens", "cache_read_tokens",
"cache_write_tokens", "reasoning_tokens", "model_calls", "duration_ms", "cost_estimate_usd",
"cost_is_estimate", "price_table_version", "source", "raw", "captured_at"}`. See
[`usage_report`](#usage_report) for what the cost is and is not.

#### Reopening a worker's session

`session_id` is the provider's own session id for the worker, and `resume` says how a human
reopens it outside TaskSpindle: `{"family", "session_id", "cwd", "argv", "command", "env",
"note"}`. For a Claude or Grok task, `command` is the native CLI invocation (`claude --resume
<id>` or `grok -r <id>`); run it from `cwd` (the task's worktree, or its scratch repository for a
consult without one) with `env` applied, because both CLIs key their session store to the
directory the worker ran in. For an Antigravity task, or a profile of an unknown family, `argv`
and `command` are `null` and `note` says why: an agy conversation lives in the task's private
state directory that only the sandboxed worker mounts. `note` also warns when the workspace has
already been cleaned up. The handle follows the task's recorded provider family, never the
current profile configuration.

This is a hand-off, not a continuation. Whatever happens in a reopened session is outside
TaskSpindle's containment: it is not recorded on the task, the task's mode restrictions no longer
apply, and a candidate the worker built is unaffected unless you commit on top of it yourself.
Use [`continue_task`](#continue_task) to keep working inside the record instead.

### `task_diff` — **not** read-only

| Parameter | Type | Default |
| --- | --- | --- |
| `task_id` | string | — |
| `offset` | int | `0` |
| `length` | int | `16384` (at most `262144`) |

Returns `{"digest", "size", "offset", "length", "data", "receipt_id"}`. `data` is base64; `size` is
the whole diff, `length` what this page actually contains. Errors: `TASK_NOT_FOUND`,
`INVALID_REQUEST` (no candidate at this revision, offset past the end, non-positive length).

**Keep pages smaller than your client will truncate.** An MCP client caps the tool output it hands
the model — Codex's `tool_output_token_limit` is a few thousand tokens by default — and a page that
was cut off on the way in is still receipted in full here. A 16384-byte page (about 22 KB of
base64) arrived intact in the September 4 Codex measurement with an 8000-token output limit;
tokenization depends on the content, so this is not a guarantee for every diff. Decode each
received page and check its byte length against `length`; re-read smaller pages if truncated.

**The diff-coverage rule.** Every call records a receipt of `(digest, offset, length)`. An
acceptance requires that the recorded receipts for the exact `diff_digest` cover `[0, size)` with
no gap. Ask for a page, then the next, until `offset + length == size`. If you accept before that,
`accept_task` answers `DIFF_NOT_FULLY_RETRIEVED` and `details.missing` lists the byte ranges you
have not seen. A new candidate revision has a new digest, and its coverage starts empty.

### `continue_task`

| Parameter | Type | Default |
| --- | --- | --- |
| `task_id` | string | — |
| `expected_state_version` | int | — |
| `prompt` | string | `""` |

One more turn. What it means depends on where the task is.

| State | Mode | Turn |
| --- | --- | --- |
| `RESULT_READY` | implement | a **repair** on the candidate, producing a new revision |
| `COMPLETED` | consult | a **follow-up** question in the same session |
| `INTERRUPTED` | any | a **resume** of the interrupted turn |

A `FAILED` task cannot be continued: `ILLEGAL_TRANSITION`. A provider refusal is recorded on
`provider_status` and cleared by `start_task`'s own admission once the provider is eligible again
(or immediately with `ignore_provider_status`) — start a new task rather than resuming the failed
one's own session.

Anything else is `ILLEGAL_TRANSITION`. A resume of a task with no stored session is
`RESUME_UNAVAILABLE`. A task in `RECOVERY_AMBIGUOUS` is reconciled once more first, and if it is
still ambiguous the answer is `MANUAL_RECOVERY_REQUIRED`. Other errors: `TASK_NOT_FOUND`,
`STALE_STATE_VERSION`. Returns `{"task_id", "state", "state_version"}`.

Clients advertising form elicitation can receive a single retry-or-cancel question when recovery
remains ambiguous. Both choices use the caller's original `expected_state_version` and the same
recovery checks: neither forces resolution or cancellation. A changed state returns
`STALE_STATE_VERSION`; unresolved recovery still returns `MANUAL_RECOVERY_REQUIRED`. The original
error also remains when the question is declined or dismissed, elicitation fails or returns
malformed data, or the client lacks form support.

## Acceptance

### `accept_task`

Takes one object parameter, `request`; the fields below go inside it.

| Fields of `request` | Type | Meaning |
| --- | --- | --- |
| `task_id` | string | the implement task holding the candidate |
| `expected_state_version` | int | the version you last saw |
| `candidate_sha` | string | the candidate you inspected |
| `diff_digest` | string\|null | its diff digest; needed only with `require_diff_receipts` |
| `inspection_summary` | string | what you concluded, in your own words; not empty |
| `expected_target_head` | string | the repository HEAD you are applying onto |
| `review_task_id` | string\|null | the review task covering this candidate, if any |
| `commit_message` | string | one line, ≤ 72 characters |
| `dispositions` | object[] | `{"finding_id", "disposition", "reason"}` |
| `require_diff_receipts` | bool | default `false`; see below |
| `require_review` | bool | default `false`; see below |
| `rerun_verification` | bool | default `false`; see below |
| `require_root_stability` | bool | default `false`; see below |

**Every gate that is not about repository safety is opt-in, defaulting to off.** The merge-tree
probe, the integration journal, the scope (`path_prefixes`) check and `state_version` are never
optional. The rest are:

- **`require_diff_receipts`** — `diff_digest` must match the candidate's, and every byte of the
  diff must have been retrieved through `task_diff` (`CANDIDATE_MISMATCH`,
  `DIFF_NOT_FULLY_RETRIEVED`). Off by default: an accepting session that read the candidate some
  other way is not forced to also page through `task_diff`.
- **`require_review`** — a review must exist for `review_task_id`, cover this task and
  `candidate_sha`, come from a different provider than the author, and have every blocking or
  critical finding disposed (`REVIEW_REQUIRED`, `REVIEW_STALE`, `REVIEWER_NOT_INDEPENDENT`,
  `REVIEW_BLOCKED`). Off by default, but **naming a `review_task_id` is always checked**, required
  or not: a stale or self review is never accepted quietly just because it was optional. With
  `require_review` off, a named review's undisposed blocking findings do not refuse the
  acceptance; they are recorded as a `REVIEW_BLOCKED:<finding ids>` warning on the task instead.
- **`rerun_verification`** — the accept unit reruns `verification_commands` in the root repository
  and that run decides `CHECKS_FAILED`. Off by default: the candidate's own `check_summary.ok`
  from the worker is trusted instead, and a candidate whose own checks failed is refused
  `CHECKS_FAILED` immediately, before anything is journalled or a unit is started.
- **`require_root_stability`** — an unacknowledged `ROOT_MUTATION` warning blocks acceptance
  (`ACCEPT_BLOCKED`) until `record_integration` clears it with `root_mutation_acknowledged`. Off
  by default: the warning is still recorded on the task, but does not by itself refuse
  acceptance. See [architecture.md](architecture.md) for what does and does not still count as a
  mutation now that a sibling task's own prior accept is excluded.

`SCOPE_VIOLATION` always blocks (`ACCEPT_BLOCKED`, with the blocking warnings in
`details.warnings`), whatever the flags say.

Only then does the task move to `ACCEPTING`, the intent get journalled, and a detached unit start.
`accept_task` returns as soon as that unit is launched: poll `task_status` for `ACCEPTED`, or for a
return to `RESULT_READY` with a new warning. What the unit does — merge-tree probe, root apply,
verification in the root (when `rerun_verification` asked for it), commit — is in
[architecture.md](architecture.md).

Errors also include `UNIT_START_FAILED` (retryable; nothing touched the repository) and
`STALE_STATE_VERSION`.

### `record_integration`

Takes one object parameter, `request`; the fields below go inside it.

| Fields of `request` | Type | Meaning |
| --- | --- | --- |
| `task_id` | string | — |
| `expected_state_version` | int | — |
| `kind` | `conflict_resolved`\|`manual_integration`\|`root_mutation_acknowledged` | — |
| `summary` | string | what you did, for the audit trail |
| `resulting_head` | string\|null | required except for `root_mutation_acknowledged` |
| `require_diff_receipts` | bool | default `false`; same meaning as on `accept_task` |
| `require_review` | bool | default `false`; same meaning as on `accept_task` |
| `rerun_verification` | bool | default `false`; requires the recorded `check_summary.ok` instead of trusting it, since a hand-made integration has no root rerun to fall back on |
| `require_root_stability` | bool | default `false`; same meaning as on `accept_task` |

What you did by hand, written into the record. `conflict_resolved` and `manual_integration` move
the task straight to `ACCEPTED` at `resulting_head` and require the task to be in `RESULT_READY`.
`root_mutation_acknowledged` moves nothing: it clears the `ROOT_MUTATION` warning that a
`require_root_stability` acceptance of this candidate revision would otherwise block on.

A hand-made integration always skips the probe (you ran the merge yourself) and always requires a
`resulting_head` that exists in the repository and differs from the candidate's base
(`INVALID_REQUEST`), and never skips `SCOPE_VIOLATION` (`ACCEPT_BLOCKED`). The checks gate,
diff-receipt gate, review gate and root-stability gate are the same opt-in flags `accept_task`
uses, all off by default; a review that is found is still checked for being bound to this
candidate and independent of its author, whether or not `require_review` asked for one.
Errors: `TASK_NOT_FOUND`, `STALE_STATE_VERSION`, `INVALID_REQUEST`, `ILLEGAL_TRANSITION`,
`DIFF_NOT_FULLY_RETRIEVED`, `REVIEW_REQUIRED`, `REVIEW_STALE`, `REVIEWER_NOT_INDEPENDENT`,
`CHECKS_FAILED`, `ACCEPT_BLOCKED`.

### `reject_task`

`task_id`, `expected_state_version`, `reason`. Discards the candidate. The worktree is retained
until `cleanup_task`, so a rejection is still recoverable by hand.
Errors: `TASK_NOT_FOUND`, `STALE_STATE_VERSION`, `ILLEGAL_TRANSITION`.

## Stopping and tidying

### `cancel_task`

`task_id`, `expected_state_version`. A live worker is signalled `SIGTERM`, which it turns into an
ACP `session/cancel` and a recorded `CANCELLED`; a task that never started is cancelled here and
now. Errors: `TASK_NOT_FOUND`, `STALE_STATE_VERSION`, `ILLEGAL_TRANSITION`.

### `cleanup_task`

`task_id`, `force` (default `false`). Gives back the worktree, the task's refs under
`refs/taskspindle/<task_id>/`, and its scratch space. The task must be in a terminal state
(`COMPLETED`, `ACCEPTED`, `REJECTED`, `CANCELLED`, `FAILED`) or the answer is
`ILLEGAL_TRANSITION`. A worktree with uncommitted changes is **retained**, not removed, unless
`force` is set. Returns `{"task_id", "cleanup_state", "removed": [...], "retained": [...]}` and, on
a git failure, `cleanup_state: "FAILED"` with an `error` object rather than a raised error.

A task whose repository no longer exists cannot have its worktree removed by git, and the answer
is `cleanup_state: "FAILED"` with `error.code = REPOSITORY_UNRESOLVABLE`. With `force`, the
worktree directory — which lives under TaskSpindle's own state directory and holds nothing anyone
can use any more — is deleted outright and the cleanup completes.

## Usage

### `usage_report` — read-only

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `since` | string\|null | `null` | ISO-8601, or shorthand: `7d`, `24h`, `30m`, `90s` |
| `provider` | string\|null | `null` | only this profile |
| `group_by` | `provider`\|`day`\|`provider_day`\|`model`\|`mode`\|`repository_id`\|`role` | `provider` | how the token counts are rolled up |

`repository_id` combines providers within each repository; repository-free turns share a `null`
group. Buckets retain that stable ID and add `repository_path` (the registered display path or
null). Tables display the path when available, otherwise the ID, and use "No repository" for
the null group. Labels never change grouping identity. Individual `task_result.usage` entries
also include `repository_id`. `role` rolls token use up by provider and role, from the `role`
recorded on `start_task`; a turn from a task started without `role` falls in a `null` group.
Existing provider and time filters apply.

Returns:

```json
{"since": "2026-08-28T18:41:07.000000Z", "provider": null, "group_by": "provider",
 "generated_at": "2026-09-04T18:41:07Z", "cost_note": "…",
 "usage": [{"provider": "claude", "turns": 4, "input_tokens": 20, "output_tokens": 1695,
            "cache_read_tokens": 194157, "cache_write_tokens": 34570, "reasoning_tokens": 0,
            "cost_estimate_usd": 0.35, "priced_turns": 4}],
 "outcomes": [{"provider": "claude", "mode": "implement", "state": "ACCEPTED", "count": 1}],
 "turns": {"count": 5, "mean_ms": 24000, "p50_ms": 12000, "max_ms": 63000,
           "by_provider": {"claude": {"count": 3, "…": "…"}}},
 "checks": {"count": 2, "passed": 2, "mean_ms": 49, "p50_ms": 49, "max_ms": 50},
 "violations": [{"provider": "grok", "kind": "READ_ONLY_VIOLATION", "count": 1}],
 "windows": [{"provider": "claude", "state": "ok", "observable": true, "note": "…",
              "status": {"…": "…"}, "windows": [{"window": "five_hour", "…": "…"}]}]}
```

Where the numbers come from, and what they are not:

- **Tokens** are what the agent reported on the wire. The Claude adapter puts the turn's counts on
  every prompt response; Grok sends them in its `turn_completed` update, with `costUsdTicks` and
  the per-model breakdown kept untouched in the record's `raw`. Only when an agent reported
  nothing does TaskSpindle read the Claude session record the adapter's own Claude Code wrote —
  the one file for that turn's session, nothing else under `~/.claude/projects`.
- **`cost_estimate_usd` is an estimate**, and always says so. OAuth sessions may consume native
  extra usage; this figure is not a reported charge. It prices tokens at published API rates from a
  static price table whose date is `price_table_version`, so seat usage can be compared and
  budgeted. The table covers Claude, Grok and the Gemini models Antigravity selects; a model it
  does not know has no estimate, and the rate it used is named by `price_table_version`. Two
  caveats on the figure: a Grok or Gemini Pro request whose prompt reaches 200k tokens is billed
  by the vendor at double the rate used here, so the estimate is a floor; and Grok's own
  `costUsdTicks` is still kept raw and never converted, because its unit is not documented.
  When the table changes -- a new vendor rate, or a model that was previously unpriced -- run
  `taskspindle reprice` to re-estimate stored rows from the token counts already on file and bring
  their `cost_estimate_usd` and `price_table_version` back in line with the table current at the
  time it runs; it never touches `captured_at`, `raw`, or which model a row is attributed to.
- **Outcomes, timings and violations** are computed from the task, turn, check and event tables
  on every call; nothing is aggregated ahead of time.
- **Windows** are observed, never polled. For Claude, the adapter forwards the SDK's rate-limit
  events while a turn runs, and a refusal records the window as rejected at 100%; there is no
  local way to ask for the seat's headroom between turns. Grok 1.0.13 reports no window at all, so
  its entry says `observable: false` and only a refusal is ever recorded.

The same report is `taskspindle usage` on the command line, and the usage panel of the
[dashboard](dashboard.md).

## The review contract

A `review` task is asked for exactly one JSON object, and `REVIEW_MALFORMED` is recorded if it does
not produce one. Fenced or surrounded by prose is fine; the parser takes the innermost JSON object
it can find.

The reviewer is given the change itself: a candidate review's prompt carries the subject's
recorded diff, and a snapshot review's carries `git diff <expected_head> <snapshot>`, cut at 96 KiB
with a note when it is longer. The worktree is checked out at the subject as well, so files can be
read in full, but a reviewer never needs to run git — which matters, because it cannot: a Claude
review runs in the adapter's `plan` session mode, where a write or a shell command becomes a
permission request TaskSpindle refuses, and a Grok review inside Grok's `read-only` sandbox, where
the kernel refuses the write itself.

```json
{"verdict": "PASS",
 "summary": "one paragraph, in your own words",
 "findings": [{"id": "F1", "severity": "high", "path": "src/thing.py", "line": 42,
               "evidence": "what is actually there", "remedy": "what to do about it"}],
 "checks": ["what you verified"]}
```

- `verdict`: `PASS`, `CONCERN` or `BLOCK`.
- `severity`: `low`, `medium`, `high` or `critical`.
- `line` is 1-based. Finding ids must be non-empty and unique. No extra fields are accepted.

**Review kinds.** `review_kind` picks the reviewer's stance without changing the contract. A
`standard` review is the fixed defect review above. An `adversarial` review puts a preamble ahead
of the same rules telling the reviewer to assume the change is wrong and hunt for the evidence:
edge cases, error paths, ordering and state bugs, security holes, silent behaviour changes, tests
that pass without proving the acceptance criteria, and claims the diff does not deliver. Both
produce the same JSON, both are parsed the same way, and `accept_task` applies the same disposition
rules to both. The kind is recorded on the review task (`task_status.review_kind`,
`task_result.attribution.review_kind`) and on the review row, so a dashboard or a later gate can
tell which stance a candidate has been through. Nothing currently requires an adversarial review;
run one when the candidate touches something you would not want a polite reviewer to wave past.

**Disposition rules**, enforced by `accept_task` when `require_review` is set:

| Review says | What acceptance requires |
| --- | --- |
| `PASS`, no critical findings | nothing |
| any `critical` finding | that finding disposed `overridden`, with a reason |
| `BLOCK` with findings | *every* finding disposed `overridden`, with a reason |
| `BLOCK` with no findings | nothing can be overridden, so acceptance is refused outright |
| `CONCERN` | *every* finding needs some disposition |

Without `require_review`, a named review's undisposed blocking findings do not refuse acceptance;
they land as a warning on the task instead, so they stay visible without forcing the ritual.
Naming a finding that does not exist is `INVALID_REQUEST` either way.

A disposition is `{"finding_id", "disposition", "reason"}` where `disposition` is `fixed`,
`accepted_risk`, `not_applicable` or `overridden`. Every disposition except `fixed` needs a
non-empty reason. Each `overridden` disposition is written to the event log as a
`REVIEW_OVERRIDE`, so a decision to ship over a reviewer's objection is a matter of record.

A review is bound to `(task_id, candidate_sha)`. A repair produces a new candidate, which
invalidates the review: get a new one.

## Error codes

| Code | Meaning |
| --- | --- |
| `TASK_NOT_FOUND` | no such task |
| `INVALID_REQUEST` | the request does not validate, or names something that does not exist |
| `ILLEGAL_TRANSITION` | the task is not in a state where this is possible |
| `STALE_STATE_VERSION` | the task moved since you read it |
| `MODE_FORBIDS_STATE` | a consult or review task cannot enter an implement-only state |
| `GRANT_MISSING` | no active grant for this repository, provider and mode |
| `LEASE_BUSY` | that provider already has a turn in flight |
| `METERED_NOT_ALLOWED` | an `api_key` profile without `allow_metered` |
| `REVIEWER_NOT_INDEPENDENT` | the reviewer is not a genuinely different agent |
| `DIFF_NOT_FULLY_RETRIEVED` | the whole diff has not been read |
| `REVIEW_REQUIRED` / `REVIEW_STALE` / `REVIEW_BLOCKED` | the review gate |
| `CANDIDATE_MISMATCH` | the candidate moved since it was inspected |
| `TARGET_MOVED` | the repository head is not where the request said |
| `CHECKS_FAILED` | verification commands did not all pass |
| `ACCEPT_BLOCKED` | warnings on the candidate must be resolved first |
| `ACCEPT_FAILED` | recorded as a warning when an acceptance was undone |
| `ROOT_MUTATION` | the task changed the root repository |
| `RESUME_UNAVAILABLE` | there is no session to resume |
| `MANUAL_RECOVERY_REQUIRED` | recovery will not guess; see [recovery.md](recovery.md) |
| `UNIT_START_FAILED` | systemd would not start the unit (retryable) |
| `DIRTY_OVERLAP` | in `details.code`: the repository is dirty inside the task's own prefixes |
| `MODE_UNAVAILABLE` | on a FAILED task: the agent refused the session mode its task needs, so the turn did not run |
| `PROVIDER_THROTTLED` | on a FAILED task: the provider refused the turn for a usage, rate or credit limit |
| `PROVIDER_AUTH_EXPIRED` | on a FAILED task: the provider refused the turn because the seat is logged out or not allowed |
| `PROVIDER_ACCESS_DENIED` | on a FAILED task: the provider denied account access without claiming the login itself is invalid |
| `PROVIDER_MODEL_UNAVAILABLE` | on a FAILED task: the requested model, rather than the account, is unavailable |
| `PROVIDER_UNAVAILABLE` | `start_task` refused: the provider's last turn hit one of the above and it is not eligible again yet; pass `ignore_provider_status` for an explicit coordinator override |
| `POLICY_BUDGET_EXHAUSTED` | `start_task` refused (retryable): the named provider has an exhausted, enforced dispatch-policy budget; `details` carries `provider`, `window`, `kind`, `limit`, `used`, `window_start`, `policy_revision` |
| `INTERNAL` | an unanticipated error; the traceback is in `state_dir/server.log` |
