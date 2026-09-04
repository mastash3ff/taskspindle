# Tool reference

Sixteen tools, one envelope. Everything a Codex session can ask TaskSpindle to do is here, and
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

Read-only tools are annotated `readOnlyHint: true`. Six are:  `capabilities`, `doctor`,
`list_repository_policies`, `list_tasks`, `task_status`, `task_result`. **`task_diff` is not one of
them**, and that is deliberate: handing a page of a diff over appends a receipt that later proves
the whole candidate was inspected. Reading changes the record.

## Discovery

### `capabilities` — read-only

No parameters. What you need to compose a valid request, and nothing about any task.

```json
{"providers": [{"id": "claude", "first_class": true, "second_class": false, "auth": "oauth",
                "modes": ["consult", "implement", "review"], "model": null, "gateway_host": null}],
 "modes": ["consult", "review", "implement"],
 "versions": {"taskspindle": "0.1.0", "api": 1, "schema": 1,
              "adapter_package": "@agentclientprotocol/claude-agent-acp",
              "adapter_version": "0.70.0", "acp": "0.12.0"},
 "limits": {"timeout_s": [60, 14400], "diff_page_bytes": 262144,
            "concurrent_turns_per_provider": 1},
 "states": ["PREPARING", "…"], "cleanup_states": ["RETAINED", "…"],
 "isolation": "…what isolation does and does not mean…"}
```

Read the `isolation` string before you trust anything to it. It says plainly that worktrees and
allowlisted environments are containment by construction and not an OS sandbox.

### `doctor` — read-only

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `live_probes` | bool | `true` | run the two checks that start a process |

Returns `{"ok": bool, "checks": [{"name", "ok", "detail", "advisory"}]}`. `ok` ignores advisory
checks. Same checks as the `taskspindle doctor` command.

## Repository policy

A provider may work in a repository only while an active grant covers that repository, that
provider and that mode. Grants are keyed to the repository's canonical identity: the real path of
its common git directory plus its root commit.

### `authorize_repository`

| Parameter | Type | Meaning |
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

### `list_repository_policies` — read-only

No parameters. Returns `{"repositories": [{"repository_id", "display_path", "common_dir",
"root_commit", "grants": [...]}]}`.

## Starting and watching work

### `start_task`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `provider` | string | — | a profile id from `capabilities` |
| `mode` | `consult`\|`review`\|`implement` | — | what the task may do |
| `prompt` | string | — | the request itself |
| `repository` | string\|null | `null` | required for `implement` |
| `model` | string\|null | `null` | recorded and, where the agent supports it, requested |
| `effort` | string\|null | `null` | as above |
| `timeout_s` | int | `1800` | 60–14400 |
| `allow_metered` | bool | `false` | required for an `api_key` profile |
| `acceptance_criteria` | string\|null | `null` | **implement only, required** |
| `path_prefixes` | string[]\|null | `null` | **implement only, required**; repository-relative, no `..`, no leading `/` |
| `verification_commands` | string[]\|null | `null` | **implement only, required**; may be `[]` |
| `candidate_message` | string\|null | `null` | **implement only, required**; one line, ≤ 72 characters |
| `review_target` | object\|null | `null` | **review only, required** |

`review_target` is either
`{"kind": "candidate", "task_id": …, "candidate_sha": …}` — review another task's staged candidate —
or `{"kind": "snapshot", "repository": …, "expected_head": …, "paths": [...]}`, which commits the
current working tree to `refs/taskspindle/snapshots/<id>` and reviews that.

Returns `{"task_id", "state", "state_version"}`. The task is created, its worktree is made, its
first turn is composed and it is queued; a worker unit starts if the provider's lease is free.

Errors: `INVALID_REQUEST` (validation, unknown provider, mode not served, a repository with
uncommitted changes inside the task's own path prefixes — `details.code = DIRTY_OVERLAP`),
`GRANT_MISSING`, `METERED_NOT_ALLOWED`, `REVIEWER_NOT_INDEPENDENT`, `CANDIDATE_MISMATCH`,
`TARGET_MOVED`.

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

`state_version` increments on every transition. Hold on to it: the four tools that change a task
require the version you last saw, and answer `STALE_STATE_VERSION` if something moved underneath
you.

### `task_result` — read-only

`task_id`. Returns `{"task_id", "state", "response", "checks": [{"command", "exit_code", "ok",
"duration_ms", "stdout_tail", "stderr_tail"}], "attribution": {"provider", "auth_mode",
"requested_model", "reported_model", "gateway_host"}, "quota_warnings", "transcript_locator"}`.

`attribution` is how metered work stays visible: an `api_key` profile shows `auth_mode: "api_key"`
and the gateway's host — never its URL path, never the token.

### `task_diff` — **not** read-only

| Parameter | Type | Default |
| --- | --- | --- |
| `task_id` | string | — |
| `offset` | int | `0` |
| `length` | int | `262144` |

Returns `{"digest", "size", "offset", "length", "data", "receipt_id"}`. `data` is base64; `size` is
the whole diff, `length` what this page actually contains. Errors: `TASK_NOT_FOUND`,
`INVALID_REQUEST` (no candidate at this revision, offset past the end, non-positive length).

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

One more turn. What it means depends on where the task is:

| State | Mode | Turn |
| --- | --- | --- |
| `RESULT_READY` | implement | a **repair** on the candidate, producing a new revision |
| `COMPLETED` | consult | a **follow-up** question in the same session |
| `INTERRUPTED` | any | a **resume** of the interrupted turn |

Anything else is `ILLEGAL_TRANSITION`. A resume of a task with no stored session is
`RESUME_UNAVAILABLE`. A task in `RECOVERY_AMBIGUOUS` is reconciled once more first, and if it is
still ambiguous the answer is `MANUAL_RECOVERY_REQUIRED`. Other errors: `TASK_NOT_FOUND`,
`STALE_STATE_VERSION`. Returns `{"task_id", "state", "state_version"}`.

## Acceptance

### `accept_task`

| Parameter | Type | Meaning |
| --- | --- | --- |
| `task_id` | string | the implement task holding the candidate |
| `expected_state_version` | int | the version you last saw |
| `candidate_sha` | string | the candidate you inspected |
| `diff_digest` | string | its diff digest |
| `inspection_summary` | string | what you concluded, in your own words; not empty |
| `expected_target_head` | string | the repository HEAD you are applying onto |
| `review_task_id` | string | the review task covering this candidate |
| `commit_message` | string | one line, ≤ 72 characters |
| `dispositions` | object[] | `{"finding_id", "disposition", "reason"}` |

**Every precondition, in the order they are checked:**

1. The task is in `RESULT_READY`.
2. `expected_state_version` matches.
3. `candidate_sha` matches the task's current candidate — otherwise `CANDIDATE_MISMATCH`.
4. `diff_digest` matches that candidate's diff — otherwise `CANDIDATE_MISMATCH`.
5. The whole diff has been retrieved — otherwise `DIFF_NOT_FULLY_RETRIEVED`.
6. A review exists for `review_task_id` — otherwise `REVIEW_REQUIRED`.
7. That review covers this task and this `candidate_sha` — otherwise `REVIEW_STALE`.
8. The reviewer's provider differs from the author's — otherwise `REVIEWER_NOT_INDEPENDENT`.
9. Every blocking or critical finding has an explicit override — otherwise `REVIEW_BLOCKED`.
10. The candidate's own verification commands all passed — otherwise `CHECKS_FAILED`.
11. The candidate carries no `SCOPE_VIOLATION` warning, and no unacknowledged `ROOT_MUTATION`
    warning — otherwise `ACCEPT_BLOCKED`, with the blocking warnings in `details.warnings`.

Only then does the task move to `ACCEPTING`, the intent get journalled, and a detached unit start.
`accept_task` returns as soon as that unit is launched: poll `task_status` for `ACCEPTED`, or for a
return to `RESULT_READY` with a new warning. What the unit does — merge-tree probe, root apply,
verification in the root, commit — is in [architecture.md](architecture.md).

Errors also include `UNIT_START_FAILED` (retryable; nothing touched the repository) and
`STALE_STATE_VERSION`.

### `record_integration`

| Parameter | Type | Meaning |
| --- | --- | --- |
| `task_id` | string | — |
| `expected_state_version` | int | — |
| `kind` | `conflict_resolved`\|`manual_integration`\|`root_mutation_acknowledged` | — |
| `summary` | string | what you did, for the audit trail |
| `resulting_head` | string\|null | required except for `root_mutation_acknowledged` |

What you did by hand, written into the record. `conflict_resolved` and `manual_integration` move
the task straight to `ACCEPTED` at `resulting_head` and require the task to be in `RESULT_READY`.
`root_mutation_acknowledged` moves nothing: it clears the `ROOT_MUTATION` warning that was blocking
acceptance, so somebody has signed for what the agent did outside its worktree.
Errors: `TASK_NOT_FOUND`, `STALE_STATE_VERSION`, `INVALID_REQUEST`, `ILLEGAL_TRANSITION`.

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

## The review contract

A `review` task is asked for exactly one JSON object, and `REVIEW_MALFORMED` is recorded if it does
not produce one. Fenced or surrounded by prose is fine; the parser takes the innermost JSON object
it can find.

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

**Disposition rules**, enforced by `accept_task`:

| Review says | What acceptance requires |
| --- | --- |
| `PASS`, no critical findings | nothing |
| any `critical` finding | that finding disposed `overridden`, with a reason |
| `BLOCK` with findings | *every* finding disposed `overridden`, with a reason |
| `BLOCK` with no findings | nothing can be overridden, so acceptance is refused outright |
| `CONCERN` | *every* finding needs some disposition |

A disposition is `{"finding_id", "disposition", "reason"}` where `disposition` is `fixed`,
`accepted_risk`, `not_applicable` or `overridden`. Every disposition except `fixed` needs a
non-empty reason. Naming a finding that does not exist is `INVALID_REQUEST`. Each `overridden`
disposition is written to the event log as a `REVIEW_OVERRIDE`, so a decision to ship over a
reviewer's objection is a matter of record.

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
| `CLEANUP_FAILED` | a worktree could not be given back |
| `INTERNAL` | an unanticipated error; the traceback is in `state_dir/server.log` |
