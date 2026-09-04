# Dashboard

`taskspindle web` serves a read-only view of the database over HTTP: the same tasks, turns,
checks, reviews, provider status and usage rollups the MCP tools and the `usage` command read,
laid out as a page instead of a JSON envelope. It changes nothing. There is no mutation endpoint,
every route is GET-only, and the database itself is opened `mode=ro` so a bug here cannot corrupt
what a running worker is writing.

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

The dashboard binds `127.0.0.1` by default and has **no authentication** — no login, no token,
no cookie. That is a deliberate assumption, not an oversight: the database file itself is
`0600`, readable only by the user that owns `state_dir`, and the dashboard extends exactly that
trust boundary to a local port instead of adding a second one. Binding any other address prints a
warning to stderr; nothing stops you, but nothing behind that port asks who you are either. Do not
put this behind a public interface without your own reverse proxy and authentication in front of
it.

Every read goes through a connection opened `sqlite3.connect(..., mode=ro)` with
`PRAGMA query_only=1`: a write attempt raises rather than mutating the database. The database file
may not exist yet — `/api/health` says so, and every list renders empty instead of failing.

## What it shows

- **Tasks** — a filterable list (state, provider, mode) of every task, and a detail page per task:
  its header (state, versions, repository, worktree, session, model, timestamps, error), the
  event timeline, every turn (timings, stop reason, usage, response text, tool calls, violations,
  permission events, and the parsed transcript file when one was written), every check across
  every revision, the review verdict and findings when one exists, the candidate diff, the
  recorded warnings, and the tail of the worker log.
- **Providers** — each profile's current availability (`ok` / `unknown` / `throttled` /
  `auth_expired`), when it resets, the suggested first-class alternative, the latest observed
  usage windows, and the same preflight checks `taskspindle doctor` runs. A "run live probes"
  button re-runs those checks live, from the browser, on demand.
- **Usage** — the same rollup as `taskspindle usage`: tokens and estimated cost by the filters you
  choose (since, provider, group-by including repository_id), task outcomes, turn and check timing summaries, violation
  counts, window telemetry notes, and the cost-estimate disclaimer.

The task and providers views poll every 5 seconds; usage polls every 30. Polling pauses while the
browser tab is hidden, and a "last refreshed" stamp says how current the page is.

## `/api/doctor?live=1`

The providers panel caches a doctor snapshot (`live_probes=False`, refreshed at most once a
minute) so loading the page never blocks on a process. `/api/doctor?live=1` is the one route that
does real work on a GET: it starts the live checks — a transient systemd unit, an ACP handshake
per configured profile, an `initialize` probe — the same ones `taskspindle doctor` (without
`--no-live`) runs, and returns once they finish.

## JSON API

Every route below is GET-only; anything else is `405`. Errors are JSON (`{"error": "..."}`),
never a traceback.

| Route | Returns |
| --- | --- |
| `/api/health` | `{schema_version, db_path, db_exists, read_only, version}` |
| `/api/tasks?state&provider&mode&limit` | `{tasks: [...]}`, newest first, `limit` default 100, max 1000 |
| `/api/tasks/{id}` | task, events, turns (with usage and transcript), checks, review, repository, worker log tail; `404` `TASK_NOT_FOUND` |
| `/api/tasks/{id}/diff?revision=N` | the candidate diff as `text/plain`, defaulting to the candidate revision; `404` if none |
| `/api/providers` | per-profile availability and windows, raw provider status rows, the cached doctor report |
| `/api/doctor?live=1` | the doctor report, live probes included |
| `/api/usage?since&provider&group_by` | the same shape as `taskspindle usage --json`; `400` on a bad `since` or `group_by` |

`/` serves the page itself; `/static/*` serves its assets. The page is one static HTML file plus
vanilla JavaScript — no build step, no CDN, no request ever leaves the browser's own origin.

Repository usage groups display the registered repository path when available. The JSON keeps
`repository_id` as its stable grouping key and adds `repository_path`; missing paths fall back
to the ID, while repository-free turns display as "No repository".

Task search matches a literal, case-insensitive substring of the ID or prompt and combines with
provider, mode and state filters. `/api/tasks?q=...` applies search before its result limit.
Candidate details place the revision's diff alongside its review, stacking on narrow screens.
Finding links target available new-file line locations; deleted or unavailable locations remain
plain text. Diff requests may include `revision` and `candidate_sha`; a moved candidate returns
409 so a stale review cannot silently be paired with its replacement diff.
