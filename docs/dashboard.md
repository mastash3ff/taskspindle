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
it. There is still no app-level login or HTTPS here: an operator who widens access is expected to
be relying on a network boundary they already trust (a private VPN), not on this dashboard to
authenticate callers.

Every route, read or write, refuses a request whose `Host` header does not name this machine
(`127.0.0.1`, `::1`, or `localhost`, with an optional port), or exactly match one of the netlocs an
operator has explicitly named in `[web] allowed_hosts` (see [Remote access](#remote-access) below,
empty by default); a browser page served from anywhere else cannot DNS-rebind its way to reading
task prompts or diffs even after your machine resolves the attacker's domain to loopback, because
the rebound connection still carries the attacker's `Host`. Note what this check is *not*: `Host`
is a plain, client-supplied HTTP header, not a credential, so this is a restriction on what a
*browser* will let a rebound page send, not an authentication check on the caller. It looks only at
the actual negotiated `Host`, never the TCP peer or any `Forwarded`/`X-Forwarded-*` header a caller
could set to anything, so a deliberate `--host 0.0.0.0` bind still serves LAN callers, and naming a
remote netloc in `allowed_hosts` does too. The thing actually being trusted when either is widened
is the network path to this port — a LAN, or a private VPN an operator has already approved —
exactly as the loopback bind always trusted the local machine's own network stack; this dashboard
adds no login or TLS of its own on top of that. Every response also carries
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and `X-Frame-Options: DENY`;
`/api/*` responses add `Cache-Control: no-store`; and the HTML page carries a
`Content-Security-Policy` that allows its one inline bootstrap script only via a fresh per-response
nonce.

Every task read goes through a connection opened `sqlite3.connect(..., mode=ro)` with
`PRAGMA query_only=1`: a write attempt raises rather than mutating the database. The database file
may not exist yet — `/api/health` says so, and every list renders empty instead of failing.
Overview and Workers use only that cached state. Loading either page starts no provider process,
native quota check, doctor probe, browser, or billing collection.

The Policy page holds the dashboard's mutating routes. Dispatch-policy writes require: a loopback
peer and matching `Host` (or, when explicitly enabled, a request that satisfies the remote opt-in
described below); a same-origin `Origin`; the per-process `X-TaskSpindle-CSRF` header, whose
token is served by the page's own GET; a JSON object body of at most 64 KiB; and a state database
already at schema 11 — the dashboard never migrates, so an older database answers
`503 POLICY_UNAVAILABLE` instead. Underneath that, the dashboard's write connection carries a SQLite
authorizer that permits writes to only the two policy tables; every task table stays read-only even
on that connection, so a bug in the Policy page cannot touch task history.

`GET`/`PUT /api/ai-policy` uses the same loopback-or-remote-opt-in and `Host` checks; `PUT` also
requires same-origin `Origin`, the process CSRF token, and a JSON object body of at most 64 KiB. The
browser cannot supply an executable, argv, or filesystem path. The only process the dashboard will
spawn is the optional trusted adapter from TaskSpindle's existing `config.toml`:

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

### Remote access

Running the dashboard in a container behind a private VPN (see the Compose deployment) means the
TCP peer the container observes is never `127.0.0.1`, even for a legitimate call. Rather than widen
trust implicitly, `taskspindle web` reads an explicit opt-in from the same `config.toml`:

```toml
[web]
allowed_hosts = ["192.168.0.100:8765"]
allow_remote_policy = true
diagnostics_socket = "/run/taskspindle/diagnostics/control.sock"
```

- `allowed_hosts` names exact `host[:port]` netlocs, compared by parsed hostname and port — never a
  prefix, wildcard, or substring — that `SecurityMiddleware` accepts on **every** route in addition
  to the built-in loopback names. It is empty by default; an absent `[web]` table or an absent
  `allowed_hosts` key changes nothing. Listing a remote host here alone only widens *read* access to
  the same routes any loopback caller already reaches.
- `allow_remote_policy` additionally lets the dispatch-policy and AI-policy GET/write routes accept
  a request whose `Host` exactly matches a configured `allowed_hosts` entry, in place of the
  loopback-peer requirement. It is `false` by default. Same-origin `Origin`, the CSRF header, the
  body-size limit, and the schema-11 SQLite authorizer are unconditional and still apply. Neither
  setting ever consults a `Forwarded` or `X-Forwarded-*` header — only the `Host` this connection
  actually negotiated (and, for the loopback path, the actual TCP peer) counts, so a caller cannot
  claim to be a trusted remote host it did not arrive as.
- A field that is simply absent keeps its loopback-only default, evaluated independently per
  field. A field that *is* present but malformed (wrong type, an unparsable host, a relative
  diagnostics path, `[web]` itself not a table) is a configuration mistake, not a hint to guess a
  safer reading of it: `taskspindle web` refuses to start and prints why, the same way a malformed
  `[concurrency]` table already does, rather than silently degrading that setting to its default or
  falling back to a local check it cannot truthfully perform.
- `/api/health.policy_writable` reflects the actual outcome for the calling request: `true` only
  when the policy schema is ready *and* this specific caller's `Host`/peer would pass the guard
  above.

### Diagnostics forwarding

A dashboard running in a container cannot truthfully run `taskspindle doctor`'s checks against the
host it runs on. When `[web] diagnostics_socket` names an absolute path to a private Unix control
socket, `GET /api/doctor` (live or not) forwards its request to a trusted controller process over
that socket instead of ever probing the container itself, via a bounded (90 s) newline-JSON
request-response exchange asking for operation `"doctor"` with argument `{"live": bool}`. A
missing or refused socket, or a reply that does not exactly match the `{ok: bool, checks:
[{name: str, ok: bool, detail: str, advisory: bool}, ...]}` shape `taskspindle doctor` itself
produces, is reported as a clear `{"ok": false, "status": "unavailable", "error": <code>}` result —
the dashboard never falls back to running the check itself on either failure. The non-live response
cache behaves exactly as it does without a socket configured. `diagnostics_socket` is absent by
default, which retains the original local-check behavior; a *present* but malformed value (not an
absolute path string) fails startup clearly rather than silently falling back to that default — see
above.

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
- **Workers** — each profile's current availability (`ok` / `throttled` / `auth_expired` /
  `access_denied` / `model_unavailable`), its `reset_at` and `eligible_at`, a fixed safe `reason`
  (naming the model for a model-scoped refusal), the latest observed usage windows, its cached
  native-check projection, and cached doctor status. These are read-only projections: Workers has
  no control that changes provider status or starts work. A "run live probes" button explicitly
  calls `/api/doctor?live=1`; an untouched dashboard reports doctor status as not run rather than
  treating the absence of a diagnostic as success. The old `#/providers` location remains an alias
  for `#/workers`.
- **Policy** — at the top, an independent Codex AI mode card (Ensemble or Native, host Both /
  Windows / WSL, Apply) with per-host `configured`, `needs repair`, `unavailable`, `update failed`,
  or mixed state. Ensemble is TaskSpindle-first with native fallback; Native turns external
  integration off and retains Codex subagents. Apply talks to `/api/ai-policy` and does not dirty
  the dispatch-policy draft; a successful apply names the mode and tells you to start a new Codex
  conversation. Routine integrity checks stay collapsed; failures stay visible. Below that, the
  dispatch policy document and its
  observed status: per-provider shares, budgets and enable state; per-role briefs, provider
  preference, model/effort selections and timeouts; and the read-only `[concurrency]` table from
  `config.toml` for context. The dispatch editor holds a draft while you edit — polling pauses —
  validates locally, and saves the whole document against the revision it was loaded from; every
  save is kept in history. See [dispatch-policy.md](dispatch-policy.md#editing).
- **Usage** — the same rollup as `taskspindle usage`: tokens and estimated cost by the filters you
  choose (since, provider, group-by including repository_id), task outcomes, turn and check timing summaries, violation
  counts, window telemetry notes, and the cost-estimate disclaimer.
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
| `/api/providers` | per-profile availability (`state`, `reset_at`, `eligible_at`, `reason`), windows, cached `native_check`, sanitized provider status, cached doctor report; no probes |
| `/api/doctor?live=1` | run the doctor report with explicit live probes |
| `/api/usage?since&provider&group_by` | the same shape as `taskspindle usage --json`; `400` on a bad `since` or `group_by` |
| `/api/policy` | GET: `{policy, revision, fingerprint, updated_at, updated_by, source, document_error, status, defaults, profiles, file_managed, writable, csrf_token}` |
| `/api/policy` | PUT `{if_revision, policy}`: the GET payload after saving; `400 POLICY_INVALID` with `details.errors`, `409 POLICY_REVISION_CONFLICT` with `current_revision` |
| `/api/policy/reset` | POST `{if_revision}`: the GET payload after saving the defaults |
| `/api/policy/history?limit=50` | GET: `{history: [{revision, updated_at, updated_by, fingerprint, reason}]}` |
| `/api/policy/history/{revision}` | GET: `{revision, policy, updated_at, updated_by, reason}`; `404 POLICY_REVISION_NOT_FOUND` |
| `/api/ai-policy` | GET: `{hosts, applies_to: "new_sessions", csrf_token}` from the configured adapter (`action: "status"`). Missing adapter: per-host `unavailable`. `Cache-Control: no-store`. Loopback peer and `Host` required, or the `[web]` remote opt-in. |
| `/api/ai-policy` | PUT `{mode, hosts, expected_revisions}`: `{hosts, results, applies_to: "new_sessions", csrf_token}` after `action: "use"`. `409 AI_POLICY_APPLY_FAILED` when any host fails (including a stale revision); the body still carries fresh per-host status. Never a top-level success when any host failed. |

The policy `PUT`/`POST` routes and `PUT /api/ai-policy` are the dashboard's mutating routes, and
only they carry the guards described under [Trust model](#trust-model) — a loopback peer and
`Host` (or the `[web]` remote opt-in), same-origin `Origin` and the `X-TaskSpindle-CSRF` header on
writes, and a bounded JSON body. Dispatch-policy writes also use the schema-11 SQLite authorizer
limiting writes to the two policy tables. AI-policy writes never touch those tables or
`config.toml`; they send JSON to the configured adapter on stdin and read JSON on stdout.
`/api/health.read_only` reflects that: it is `false` because the process has mutating routes, while
`task_database_read_only` stays `true` because the task tables are never in them. `policy_writable`
is `true` only when the dispatch-policy schema is ready *and* the calling request would itself pass
the guard above (loopback, or the explicit remote opt-in); `policy_schema_ready` is false below
schema 11.

The AI-policy adapter contract is stdin
`{action: "status"|"use", hosts, mode (use only), expected_revisions (required for use)}` and
stdout `{hosts: [{host, mode, status, revision, checks, error?}], results?: [{host, ok, error?}]}`.
`status` is `configured`, `needs_repair`, `unavailable`, or `update_failed`. A stale
`expected_revisions` value becomes a failed result with the adapter's fresh host status. Partial
apply errors keep that fresh per-host state and never claim that every host succeeded.

`/` serves the page itself; `/static/*` serves its packaged modules and styles. The console uses
plain HTML, CSS, and JavaScript — no build step or CDN, and no request leaves the browser's own
origin.

Each `/api/providers` availability projection is deliberately small: `state` (`ok`, `throttled`,
`auth_expired`, `access_denied`, or `model_unavailable`), `reset_at`, `eligible_at`, and `reason` —
the same shape `capabilities.providers[].availability` returns over MCP. A model-scoped refusal
names the model in `reason`; there is no separate per-model projection. Nothing here starts a
process or mutates provider state; Workers is entirely read-only.

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
