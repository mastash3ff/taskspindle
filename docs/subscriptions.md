# Subscription tracking

The Subscriptions page records billing observations for `chatgpt`, `claude`, `google_ai`,
and `grok`. It is independent of worker configuration: ChatGPT is listed directly, and
Google AI appears once for a subscription shared by Gemini/AGY. Initial connections use
one personal account per provider. Account changes require verification and cannot overwrite
another account's saved subscription silently.

Only subscriptions purchased directly from provider websites are in scope. Apple, Google
Play, and X Premium-derived subscriptions are reported as unsupported billing channels.
An adapter being installed does not prove that a live provider's billing data was readable.
See [collector evidence](subscriptions/evidence.md) for observed validation and limitations.

## Setup and operation

```sh
taskspindle subscriptions setup-browser
taskspindle subscriptions watch
```

Keep the collector running in its own terminal initially. In another terminal, or using
the dashboard's Connect buttons:

```sh
taskspindle subscriptions connect chatgpt
taskspindle subscriptions status --json
taskspindle subscriptions refresh chatgpt
taskspindle subscriptions refresh
```

Connect queues a visible dedicated Chrome window where you complete the normal sign-in.
The browser must read billing data and verify the account before the connection succeeds.
Closing the sign-in window early does not count as successful connection. Reconnect uses
the same dedicated profile and verifies the same account.

Refresh never opens an interactive login prompt. The collector refreshes connected accounts
after six hours and handles explicitly requested refreshes immediately when it is available.
Duplicate requests coalesce; a provider profile cannot be driven concurrently. Authentication
and account-mismatch failures wait for explicit action. `watch --once` processes at most one
queued or due job, prints its normalized result, and returns a nonzero exit status when
collection fails. With no work queued or due, it exits successfully without starting a browser.

No model request is used to collect billing information. No collector may cancel a plan,
change billing details, purchase credits, or alter TaskSpindle provider availability.

## Browser runtime and configuration

Setup provisions only the packaged, lockfile-pinned Node/Playwright helper. It uses an
installed Chrome executable rather than downloading a browser. On WSL, the helper runs
through Windows Node and Chrome. Its runtime lives under
`%LOCALAPPDATA%\TaskSpindle\subscriptions\runtime`; provider profiles live under
`%LOCALAPPDATA%\TaskSpindle\subscriptions\profiles\<provider>`.
Native Linux uses local Node/Chrome and TaskSpindle-owned directories.

Browser cookies and credentials remain in these private profiles. Existing normal browser
profiles and CLI authentication files are never imported. The helper returns only normalized
billing metadata and safe errors, not raw provider responses, payment details, or credentials.

Optional settings in the normal TaskSpindle config:

```toml
[subscriptions]
platform = "auto" # auto, windows, or native
timezone = "America/Chicago"
connect_timeout_s = 600
refresh_timeout_s = 180
# node_path = 'C:\Program Files\nodejs\node.exe'
# chrome_path = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
# local_app_data = 'C:\Users\you\AppData\Local'
```

The timezone is used for date-only billing information. Without an override, local timezone
discovery falls back to UTC. Executable overrides must be absolute. Browser runtime setup
and persistent collector service activation are separate operations.

## Date and freshness rules

An explicit renewal flag and an observed billing boundary produce either **Renews on**
or **Cancelled — access ends**. The latter includes days remaining. OAuth expiry, quota
reset times, subscription creation timestamps, and inferred billing cycles never supply
these dates. Date-only information stays date-only; an exact cutoff is not invented.

Collection failures preserve the last successful snapshot and show the error and last
verification time. Observations older than 24 hours are stale. If a recorded access end
passes before successful verification, the UI requests verification instead of claiming
the subscription expired. **Expired** requires provider evidence. A valid empty/free
subscription observation can clear old dates; a malformed response cannot.

## Storage and service activation

The task execution database remains read-only to the dashboard. Collection writes only the
separate `subscriptions.sqlite3` under TaskSpindle's state directory. It contains normalized
current records, observation history, queue state, and collector health; no browser secrets.

To prepare a service for review:

```sh
taskspindle subscriptions service-unit
```

This prints a systemd user service using the current interpreter, configuration, and state
locations. It does not write or enable a service. After reviewing and approving the exact
installed build, save the generated unit as `taskspindle-subscriptions.service` in the user
systemd directory and enable it. Collection then continues while the dashboard is closed,
whenever the host and its user services are running. No external notifications are sent.

## Verification

Run the Python subscription tests and the packaged helper's `npm test`. Live verification
must read each connected provider, close its dedicated browser, and refresh after reopening.
Use an already-cancelled subscription when available; fixture tests cover cancellation
without cancelling a subscription for testing. Report fixture and live evidence separately.
