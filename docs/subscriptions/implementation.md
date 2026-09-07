# Automatic subscription tracking implementation

Approved scope: automatically track direct-web ChatGPT, Claude, Google AI (one shared
Gemini/AGY subscription), and Grok subscriptions. The user's subsequent instruction replaces
dedicated sign-in with normal Chrome and its saved passwords, using the official Playwright
extension bridge. Dedicated mode remains an explicit compatibility option.
No billing mutations, app-store/X Premium collection, inference requests,
worker routing changes, installed-runtime activation, or publication.

Base: `be70171a42d20bbd6526f55268ac0b2d4a44947a`; branch
`feature/subscription-tracking`. The installed runtime remains independent.

## Integration contract

New Python package: `taskspindle.subscriptions`. Provider IDs are `chatgpt`, `claude`,
`google_ai`, `grok`. One connected personal account per provider initially.

Browser request JSON (stdin, one request per process): `provider`, `action`
(`connect` or `refresh`), `chrome_path`, `expected_account_id`
(nullable), `timeout_s`, `timezone`, and internal `operation_nonce` (random per invocation,
used with OS process identity to authorize cleanup). Stdout contains exactly one result object:
`{ok: true, observation: ...}` or `{ok: false, error: {code, message}}`.
Messages must come from a safe allowlist, never raw browser/network errors.
Normal mode also selects `chrome_profile` and uses an operation-specific ownership directory;
the private extension token is passed only in the helper environment. Dedicated mode retains
`profile_dir` for its separate profile. Neither mode exports browser credentials.

An observation contains `provider`, `account_id` (opaque stable hash),
`account_label` (masked), `billing_channel` (`provider_web`, `apple`, `google_play`,
`x_premium`, `unknown`), `plan`, `status` (`renewing`, `cancelled`, `expired`, `free`,
`none`, `unknown`), `renews_at`, `access_ends_at`, `date_precision` (`date`,
`datetime`, or null), `timezone`, `source_url` (no query/fragment), and
`collector_version`. Date fields are ISO-8601; unavailable fields are null.
Only directly observed billing values are permitted. Token expiry, quota resets,
creation dates, and calculated billing cycles cannot supply subscription dates.

`subscriptions/models.py` owns constants, validation, and presentation calculations.
`subscriptions/store.py` owns a separate `subscriptions.sqlite3` (0600), transactional
current snapshots, observation history, and a coalescing work queue. Public contract:

- `SubscriptionStore(path, read_only=False)` context manager; read-only missing DB
  returns the four empty provider rows without creating any filesystem state.
- `list_subscriptions(now)` returns presentation-ready row dictionaries.
- `enqueue(provider, action, now)` returns a job dictionary.
- `claim_next(now, owner, lease_seconds=300)` returns a job or null.
- `heartbeat(job_id, owner, now, lease_seconds=300)` extends ownership.
- `finish(job_id, owner, result, now)` records success/failure only for the owner.
- `enqueue_due(now)` schedules refresh for connected accounts after six hours;
  authentication/account/unsupported failures await explicit action.
- `set_collector_heartbeat(now)` and `collector_heartbeat()` support UI health.

Rows include provider metadata and the observation fields above plus `connected`,
`last_attempt_at`, `last_success_at`, `error`, `freshness` (`never_verified`, `fresh`,
`stale`), `days_remaining`, `end_passed_unverified`, and `operation` (job or null).
Failure retains last success; 24-hour-old success is stale. A passed recorded access
end needs verification and does not independently mark an account expired.

`subscriptions/service.py`: `SubscriptionService(paths, clock=None, runner=None)`;
`status()` returns `{subscriptions, collector_running, collector_last_seen_at}`;
`request(provider, action)` enqueues work; `run_once()` performs one bounded job and returns
its normalized result, or null when idle;
`watch()` runs periodic collection. Injected runner signature:
`runner(provider, action, expected_account_id) -> result`.

`subscriptions/runtime.py`: `run_browser(paths, provider, action,
expected_account_id=None) -> result`; `setup_browser(paths) -> dict` provisions only
the separate subscription browser runtime. Use Windows Node/Chrome from WSL and a
packaged, pinned Node/Playwright helper attached through the official Chrome extension.
Normal mode opens regular Chrome for Connect; missing extension setup yields an explicit
failure and does not save a successful account observation. Never read browser/CLI
credential stores. Keep the extension token in a private file outside the subscription DB.
Native Linux retains the explicit dedicated-profile mode. Serialize operations and close only
collector-owned billing tabs. Normal Chrome must never be terminated by helper cleanup.

Web `build_app` accepts optional `subscription_service` for tests. GET
`/api/subscriptions` returns service status and an ephemeral `csrf_token` on trusted
loopback requests. POST `/api/subscriptions/{provider}/connect|refresh` requires
loopback peer, localhost/loopback Host, exact same Origin, JSON, and
`X-TaskSpindle-CSRF`; returns 202 with a job. No CORS access. GET cannot initialize
the subscription database. Existing task database remains read-only. Health reports
`read_only: false`, `task_database_read_only: true` and subscription actions enabled.

CLI: `subscriptions setup-browser`, `setup-extension`, `connect PROVIDER`, `refresh [PROVIDER]`,
`status [--json]`, `watch [--once]`, `service-unit`. Connect/refresh enqueue jobs;
`watch` owns scheduled and manual collection. `service-unit` prints the reviewed
unit, never installs/enables it implicitly.

## Ownership and verification

1. Data model/store: model + store + focused Python tests.
2. Browser collector: packaged Node helper + provider extractors + sanitized tests.
3. Runtime/service: process bridge + queue worker + focused Python tests.
4. Web: API security + subscription view + focused API/UI tests.
5. Controller: CLI, dependency/build packaging, documentation, integration, live
   authenticated browser evidence, isolated preview, and independent final review.

Use provider-page evidence before claiming a provider works. Unknown schemas fail
closed with `PARSE_CHANGED`; auth prompts yield `AUTH_REQUIRED`; wrong account yields
`ACCOUNT_MISMATCH`; unsupported billing yields `UNSUPPORTED_BILLING_CHANNEL`.
Live cancelled cases may use an existing cancelled account; otherwise record fixture
coverage and the missing live proof. Do not cancel any subscription to test.

Deliver relevant tests/lint, wheel asset verification, Windows-browser preview, live
collector evidence with explicit gaps, local commit, exact build checksum, and
activation proposal. Approval is a final gate for installed-runtime activation,
pushes/PRs, shared merges, tags/releases, or publication.
