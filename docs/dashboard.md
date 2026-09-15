# Dashboard

`taskspindle web` serves a local operator console over HTTP. It presents the same tasks, turns,
checks, reviews, cached worker status and usage rollups that the MCP tools and CLI read. The task
database is opened `mode=ro`, so a dashboard bug cannot corrupt what a running worker is writing.
The Policy page is the mutating exception: it edits the dispatch policy that steers Codex, and it
can ask a fixed Codex AI-mode adapter (when one is configured) to switch Ensemble or Native for
new sessions. Both paths are narrowly guarded as described below.

## Starting it

```
taskspindle web
taskspindle web --host 127.0.0.1 --port 8765 --open
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--host` | `127.0.0.1` | address to bind |
| `--port` | `8765` | port to bind |
| `--open` | off | open the page in a browser once the server is up |

The process prints `taskspindle web: http://host:port` and then serves until interrupted.

## Trust model

The dashboard binds `127.0.0.1` by default and has no user login. The task database file is
`0600`, readable only by the user that owns `state_dir`, and the dashboard extends exactly that
trust boundary to a local port instead of adding a second one. Binding any other address prints a
warning to stderr; nothing stops you, but nothing behind that port asks who you are either. Do not
put this behind a public interface without your own reverse proxy and authentication in front of
it.

Every route, read or write, refuses a request whose `Host` header does not name this machine
(`127.0.0.1`, `::1`, or `localhost`, with an optional port); a browser page served from anywhere
else cannot DNS-rebind its way to reading task prompts or diffs even after your machine resolves
the attacker's domain to loopback, because the rebound connection still carries the attacker's
`Host`. This check looks only at `Host`, not the TCP peer, so a deliberate `--host 0.0.0.0` bind
still serves LAN callers. Every response also carries `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, and `X-Frame-Options: DENY`; `/api/*` responses add
`Cache-Control: no-store`; and the HTML page carries a `Content-Security-Policy` that allows its
one inline bootstrap script only via a fresh per-response nonce.

Every task read goes through a connection opened `sqlite3.connect(..., mode=ro)` with
`PRAGMA query_only=1`: a write attempt raises rather than mutating the database. The database file
may not exist yet — `/api/health` says so, and every list renders empty instead of failing.
Overview and Workers use only that cached state. Loading either page starts no provider process,
native quota check, doctor probe, browser, or billing collection.

The Policy page holds the dashboard's mutating routes. Dispatch-policy writes require: a loopback
peer and matching `Host`; a same-origin `Origin`; the per-process `X-TaskSpindle-CSRF` header, whose
token is served by the page's own GET; a JSON object body of at most 64 KiB; and a state database
already at schema 11 — the dashboard never migrates, so an older database answers
`503 POLICY_UNAVAILABLE` instead. Underneath that, the dashboard's write connection carries a SQLite
authorizer that permits writes to only the two policy tables; every task table stays read-only even
on that connection, so a bug in the Policy page cannot touch task history.

`GET`/`PUT /api/ai-policy` uses the same loopback peer and `Host` checks; `PUT` also requires
same-origin `Origin`, the process CSRF token, and a JSON object body of at most 64 KiB. The browser
cannot supply an executable, argv, or filesystem path. The only process the dashboard will spawn is
the optional trusted adapter from TaskSpindle's existing `config.toml`:

```toml
[ai_policy]
command = ["/absolute/executable", "fixed arg"]
hosts = ["windows", "wsl"]
```

`command` is a fixed argv (never a shell). `hosts` names the Codex hosts the adapter may address.
A missing or invalid table, or a missing executable, is reported as `unavailable` per host — the
dashboard does not invent a command, does not call a provider, and does not write `config.toml` or
the task database. Status and apply results come from the adapter's JSON stdout. Changes apply to
new Codex sessions only.

Every other page and route remains exactly what it was: a read-only connection with
`PRAGMA query_only=1`, no provider process, no native quota check, no doctor probe, no browser, no
billing collection.

## What it shows

- **Overview** — global task counts, work currently active, work needing attention, and candidates
  awaiting review. The bounded lists add a short, redacted task summary and expose only the
  registered repository ID and display path. Full prompts are not included in these lists.
  Attention means `RESULT_READY`, `INTERRUPTED`, `RECOVERY_AMBIGUOUS`, or a failed task whose
  cleanup is incomplete.
- **Tasks** — a filterable list (state, provider, mode) of every task, and a detail page per task:
  its header (state, versions, repository, worktree, session, model, timestamps, error), the
  event timeline, every turn (timings, stop reason, usage, response text, tool calls, violations,
  permission events, and the parsed transcript file when one was written), every check across
  every revision, the review verdict and findings when one exists, the candidate diff, the
  recorded warnings, and the tail of the worker log.
- **Workers** — each profile's current availability (`ok` / `unknown` / `throttled` /
  `auth_expired`), when it resets, the suggested first-class alternative, the latest observed
  usage windows, its cached native-check projection, controlled-recovery status, and cached doctor
  status. A recoverable refusal includes a copyable CLI command for the operator to arm one retry.
  Armed and claimed attempts show their admission deadline and related task; completed attempts show
  the named provider, model scope, permit, recorded outcome, and outcome code. A shared-account alias
  shows that named scope and cannot imply the permit authorizes the alias. These are read-only
  projections: Workers has no control that
  arms, revokes, or starts a recovery attempt. A "run live probes"
  button explicitly calls `/api/doctor?live=1`; an untouched dashboard reports doctor status as
  not run rather than treating the absence of a diagnostic as success. The old `#/providers`
  location remains an alias for `#/workers`. Native extra usage shows the current exact-profile
  policy, account/worker control scope, eligibility and safe billing observations, with unknowns
  preserved. Policy changes are read on refresh. Grok balances/caps and automatic-top-up settings
  are read-only account diagnostics; they are not task charges or spending controls.
  Automatic recovery shows the configured policy, current state, remaining attempts, next attempt,
  hold reason, and any active task. Older runtime responses without that projection remain valid.
- **Policy** — at the top, an independent Codex AI mode card (Ensemble or Native, host Both /
  Windows / WSL, Apply) with per-host `configured`, `needs repair`, `unavailable`, `update failed`,
  or mixed state. Ensemble is TaskSpindle-first with native fallback; Native turns external
  integration off and retains Codex subagents. Apply talks to `/api/ai-policy` and does not dirty
  the dispatch-policy draft; a successful apply names the mode and tells you to start a new Codex
  conversation. Routine integrity checks stay collapsed; failures stay visible. Below that, the
  dispatch policy document and its
  observed status: per-provider shares, budgets and enable state; per-role briefs, provider
  preference, model/effort selections and timeouts; and the read-only `[concurrency]`,
  `[native_overage]` and `[provider_recovery]` tables from `config.toml` for context. The dispatch
  editor holds a draft while you edit — polling pauses — validates locally, and saves the whole
  document against the revision it was loaded from; every save is kept in history. See
  [dispatch-policy.md](dispatch-policy.md#editing).
- **Usage** — the same rollup as `taskspindle usage`: tokens and estimated cost by the filters you
  choose (since, provider, group-by including repository_id), task outcomes, turn and check timing summaries, violation
  counts, window telemetry notes, and the cost-estimate disclaimer. Billing classifications count
  all turns separately, including historical/tokenless unknown turns; native-overage observations
  never turn token estimates or account balances into reported task charges.
The navigation order is **Overview**, **Tasks**, **Workers**, **Policy**, and **Usage**.
`Ctrl+K` or `Cmd+K` opens navigation destinations and a GET-only finder over as many as 200
recent tasks. Typing filters task ID, summary, repository, and state; arrow keys select a result,
Enter opens it, and Escape closes the palette. Dark, light, and system themes persist under the browser-local
`taskspindle-theme` preference; dark is the default. The responsive sidebar, focus handling, and
status labels remain keyboard accessible.

Overview polls every 10 seconds, Tasks and task detail every 5, Workers every 15, Policy every 30 s,
paused while the form holds an unsaved draft, and Usage every 30. Polling pauses while the tab is
hidden. Request generations and
`AbortController` reject stale responses, while the GET cache retains the last usable data when a
refresh fails. A "last refreshed" stamp says how current the displayed data is.

## Doctor checks

The Workers page reads only an existing doctor snapshot. `/api/providers` returns `status: not_run`
with empty checks until `/api/doctor` is requested; it never starts a diagnostic itself. Calling
`/api/doctor` runs the non-live checks and populates the cache. `/api/doctor?live=1` additionally
starts the bounded live checks — a transient systemd unit, an ACP handshake per configured profile,
and an `initialize` probe — matching `taskspindle doctor` without `--no-live`.

## JSON API

Routes below are GET-only except the policy and AI-policy routes; other methods on a GET-only
route are `405`. Errors are JSON, never a traceback.

| Route | Returns |
| --- | --- |
| `/api/health` | task DB health; `task_database_read_only: true`, `read_only: false`, `policy_writable`, `policy_schema_ready`, version |
| `/api/overview?limit` | global task counts plus bounded active/attention rows; default `limit` 20, max 100 |
| `/api/tasks?q&state&provider&mode&limit` | `{tasks: [...]}`, newest first, `limit` default 100, max 1000 |
| `/api/tasks/{id}` | task, events, turns (with usage and transcript), checks, review, repository, worker log tail; `404` `TASK_NOT_FOUND` |
| `/api/tasks/{id}/diff?revision=N` | the candidate diff as `text/plain`, defaulting to the candidate revision; `404` if none |
| `/api/providers` | per-profile availability, windows, cached `native_check`, sanitized provider status, cached doctor report; no probes |
| `/api/doctor?live=1` | run the doctor report with explicit live probes |
| `/api/usage?since&provider&group_by` | the same shape as `taskspindle usage --json`; `400` on a bad `since` or `group_by` |
| `/api/policy` | GET: `{policy, revision, fingerprint, updated_at, updated_by, source, document_error, status, defaults, profiles, file_managed, writable, csrf_token}` |
| `/api/policy` | PUT `{if_revision, policy}`: the GET payload after saving; `400 POLICY_INVALID` with `details.errors`, `409 POLICY_REVISION_CONFLICT` with `current_revision` |
| `/api/policy/reset` | POST `{if_revision}`: the GET payload after saving the defaults |
| `/api/policy/history?limit=50` | GET: `{history: [{revision, updated_at, updated_by, fingerprint, reason}]}` |
| `/api/policy/history/{revision}` | GET: `{revision, policy, updated_at, updated_by, reason}`; `404 POLICY_REVISION_NOT_FOUND` |
| `/api/ai-policy` | GET: `{hosts, applies_to: "new_sessions", csrf_token}` from the configured adapter (`action: "status"`). Missing adapter: per-host `unavailable`. `Cache-Control: no-store`. Loopback peer and `Host` required. |
| `/api/ai-policy` | PUT `{mode, hosts, expected_revisions}`: `{hosts, results, applies_to: "new_sessions", csrf_token}` after `action: "use"`. `409 AI_POLICY_APPLY_FAILED` when any host fails (including a stale revision); the body still carries fresh per-host status. Never a top-level success when any host failed. |

The policy `PUT`/`POST` routes and `PUT /api/ai-policy` are the dashboard's mutating routes, and
only they carry the guards described under [Trust model](#trust-model) — loopback peer and `Host`,
same-origin `Origin` and the `X-TaskSpindle-CSRF` header on writes, and a bounded JSON body.
Dispatch-policy writes also use the schema-11 SQLite authorizer limiting writes to the two policy
tables. AI-policy writes never touch those tables or `config.toml`; they send JSON to the configured
adapter on stdin and read JSON on stdout. `/api/health.read_only` reflects that: it is `false`
because the process has mutating routes, while `task_database_read_only` stays `true` because the
task tables are never in them. `policy_writable` is false when the dispatch-policy guards would
refuse a write regardless of the request (for example, not bound to loopback);
`policy_schema_ready` is false below schema 11.

The AI-policy adapter contract is stdin
`{action: "status"|"use", hosts, mode (use only), expected_revisions (required for use)}` and
stdout `{hosts: [{host, mode, status, revision, checks, error?}], results?: [{host, ok, error?}]}`.
`status` is `configured`, `needs_repair`, `unavailable`, or `update_failed`. A stale
`expected_revisions` value becomes a failed result with the adapter's fresh host status. Partial
apply errors keep that fresh per-host state and never claim that every host succeeded.

`/` serves the page itself; `/static/*` serves its packaged modules and styles. The console uses
plain HTML, CSS, and JavaScript — no build step or CDN, and no request leaves the browser's own
origin.

Each `/api/providers` availability projection includes its `evidence_revision` and `recovery`.
Exact entries in `model_availability` carry the same fields for that model. Recovery state is one of
`none`, `armed`, `claimed`, `succeeded`, `failed`, `revoked`, or `expired`. The command shown for an
armable refusal is generated by TaskSpindle from validated provider and model identifiers; copying it
uses the local browser clipboard and does not execute it. Recovery tasks also retain a readable
`PROVIDER_RECOVERY_ATTEMPT` warning and `PROVIDER_RECOVERY_OUTCOME` in the task event timeline. A
successful attempt remains distinct from current availability, so newer refusal evidence remains
visible and blocking. The dashboard never sends a recovery mutation request.

Availability also carries `quota_restrictions`, one normalized row for every applicable quota
window: `scope`, `model_family`, `window`, `reset`, `source`, `observed`, and an opaque
`fingerprint`. Overlapping account and model-family restrictions remain separately visible.
`auth_context` supplies an opaque fingerprint, whether it changed, and its observation; a changed
context is metadata about the evidence, never an account identity. `quota_retry` reports the
single shared post-reset retry state, its task ID, and restriction fingerprints. When it is
pending, claimed, or prompting, Workers links to that task without creating, retrying, or mutating anything.

`automatic_recovery` is an optional availability projection for compatibility with older runtime
responses. When present, it carries `policy`, `state`, `attempts_used`, `attempts_remaining`,
`next_attempt_at`, `hold_reason`, `active_task_id`, `episode_id`, and `evidence_revision`.

Repository usage groups display the registered repository path when available. The JSON keeps
`repository_id` as its stable grouping key and adds `repository_path`; missing paths fall back
to the ID, while repository-free turns display as "No repository".

Task search matches a literal, case-insensitive substring of the ID or prompt and combines with
provider, mode and state filters. `/api/tasks?q=...` applies search before its result limit.
Candidate details default to Changes & Review, with file navigation and linked findings.
Result, turns, events, metadata, and logs use secondary tabs preserved in the URL.
Finding links target available new-file line locations; deleted or unavailable locations remain
plain text. Diff requests may include `revision` and `candidate_sha`; a moved candidate returns
409 so a stale review cannot silently be paired with its replacement diff.
