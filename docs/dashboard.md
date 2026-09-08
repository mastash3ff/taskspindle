# Dashboard

`taskspindle web` serves a local operator console over HTTP. It presents the same tasks, turns,
checks, reviews, cached worker status and usage rollups that the MCP tools and CLI read. The task
database is opened `mode=ro`, so a dashboard bug cannot corrupt what a running worker is writing.
The separate **Subscriptions** page can enqueue account sign-in and refresh operations in
`subscriptions.sqlite3`; it cannot change a provider subscription or a TaskSpindle task. See
[subscription tracking](subscriptions.md).

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

Every task read goes through a connection opened `sqlite3.connect(..., mode=ro)` with
`PRAGMA query_only=1`: a write attempt raises rather than mutating the database. The database file
may not exist yet — `/api/health` says so, and every list renders empty instead of failing.
Overview and Workers use only that cached state. Loading either page starts no provider process,
native quota check, doctor probe, browser, or billing collection.

Subscription actions additionally require a loopback peer and Host, a matching Origin,
JSON content, and the per-process CSRF token returned to the local dashboard. They are
not available through a remote reverse proxy. Subscription GETs are cached database reads,
do not launch a browser, and do not create an absent database. Connect opens your normal
Chrome profile; its Playwright extension enables billing verification. Browser credentials
stay in Chrome and never reach the dashboard or task records. The private extension token
is entered through `subscriptions setup-extension`, never through a dashboard API.

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
  usage windows, its cached native-check projection, and cached doctor status. A "run live probes"
  button explicitly calls `/api/doctor?live=1`; an untouched dashboard reports doctor status as
  not run rather than treating the absence of a diagnostic as success. The old `#/providers`
  location remains an alias for `#/workers`.
- **Usage** — the same rollup as `taskspindle usage`: tokens and estimated cost by the filters you
  choose (since, provider, group-by including repository_id), task outcomes, turn and check timing summaries, violation
  counts, window telemetry notes, and the cost-estimate disclaimer.
- **Subscriptions** — ChatGPT, Claude, Google AI, and Grok billing observations, including
  renewal/access-end dates, cancellation countdowns, freshness, account connection, and
  collection failures. Google AI is one subscription shared by Gemini/AGY. ChatGPT is
  listed even though it is not a TaskSpindle worker profile.

The navigation order is **Overview**, **Tasks**, **Workers**, **Subscriptions**, and **Usage**.
`Ctrl+K` or `Cmd+K` opens navigation destinations and a GET-only finder over as many as 200
recent tasks. Typing filters task ID, summary, repository, and state; arrow keys select a result,
Enter opens it, and Escape closes the palette. Account actions remain explicit buttons. Dark, light, and system themes persist under the browser-local
`taskspindle-theme` preference; dark is the default. The responsive sidebar, focus handling, and
status labels remain keyboard accessible.

Overview polls every 10 seconds, Tasks and task detail every 5, Workers every 15, Subscriptions
every 5, and Usage every 30. Polling pauses while the tab is hidden. Request generations and
`AbortController` reject stale responses, while the GET cache retains the last usable data when a
refresh fails. A "last refreshed" stamp says how current the displayed data is.

## Doctor checks

The Workers page reads only an existing doctor snapshot. `/api/providers` returns `status: not_run`
with empty checks until `/api/doctor` is requested; it never starts a diagnostic itself. Calling
`/api/doctor` runs the non-live checks and populates the cache. `/api/doctor?live=1` additionally
starts the bounded live checks — a transient systemd unit, an ACP handshake per configured profile,
and an `initialize` probe — matching `taskspindle doctor` without `--no-live`.

## JSON API

Task routes below are GET-only; other methods are `405`. Subscription actions are the
explicit POST exceptions. Errors are JSON, never a traceback.

| Route | Returns |
| --- | --- |
| `/api/health` | task DB health; `read_only: false`, `task_database_read_only: true`, subscription-action capability, version |
| `/api/overview?limit` | global task counts plus bounded active/attention rows; default `limit` 20, max 100 |
| `/api/tasks?q&state&provider&mode&limit` | `{tasks: [...]}`, newest first, `limit` default 100, max 1000 |
| `/api/tasks/{id}` | task, events, turns (with usage and transcript), checks, review, repository, worker log tail; `404` `TASK_NOT_FOUND` |
| `/api/tasks/{id}/diff?revision=N` | the candidate diff as `text/plain`, defaulting to the candidate revision; `404` if none |
| `/api/providers` | per-profile availability, windows, cached `native_check`, sanitized provider status, cached doctor report; no probes |
| `/api/doctor?live=1` | run the doctor report with explicit live probes |
| `/api/usage?since&provider&group_by` | the same shape as `taskspindle usage --json`; `400` on a bad `since` or `group_by` |
| `/api/subscriptions` | cached subscription records, collector health, and local CSRF token |
| `POST /api/subscriptions/{provider}/connect` | queue visible connection/reconnection; 202 |
| `POST /api/subscriptions/{provider}/refresh` | queue a noninteractive billing refresh; 202 |

`/` serves the page itself; `/static/*` serves its packaged modules and styles. The console uses
plain HTML, CSS, and JavaScript — no build step or CDN, and no request leaves the browser's own
origin.

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
