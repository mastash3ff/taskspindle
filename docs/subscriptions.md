# Subscription tracking

The Subscriptions page records billing observations for `chatgpt`, `claude`, `google_ai`,
and `grok`. It is independent of worker configuration: ChatGPT is listed directly, and
Google AI appears once for a subscription shared by Gemini/AGY. Initial connections use
one personal account per provider. Account changes require verification and cannot overwrite
another account's saved subscription silently.

The Connect destinations are:

| Record | Provider settings |
| --- | --- |
| ChatGPT | <https://chatgpt.com/#settings/Billing> |
| Claude | <https://claude.ai/new#settings/billing> |
| Grok | <https://grok.com/?_s=usage> |
| Google AI (Gemini and Antigravity) | <https://one.google.com/settings> |

Personal Antigravity benefits are included in Google AI Pro/Ultra; this is the same
membership used by Gemini, managed through Google One. See the official
[Antigravity plans](https://antigravity.google/pricing) and
[Google AI subscription management](https://support.google.com/googleone/answer/16476748?hl=en).
Grok collection requires an explicit subscription date from its Billing dialog. The currently
observed Usage UI displays credits, so the collector opens
`https://grok.com/?_s=billing` internally while preserving the user-provided Usage page as the
visible Connect link. It reads account identity only from the exact same-origin
`/api/auth/session` response. Usage, credit, and quota-reset dates never become renewal or
access-end dates.

Only subscriptions purchased directly from provider websites are in scope. Apple, Google
Play, and X Premium-derived subscriptions are reported as unsupported billing channels.
An adapter being installed does not prove that a live provider's billing data was readable.
See [collector evidence](subscriptions/evidence.md) for observed validation and limitations.

## Setup and operation

```sh
taskspindle subscriptions setup-browser
taskspindle subscriptions setup-extension
taskspindle subscriptions watch
```

Install the [official Playwright Chrome extension](https://github.com/microsoft/playwright/blob/main/packages/extension/README.md)
in your usual signed-in Chrome profile before running `setup-extension`. Open the extension's
status page and copy its `PLAYWRIGHT_MCP_EXTENSION_TOKEN` into the command's hidden terminal
prompt. This permits automatic connections to that profile. Do not paste the token into
the dashboard, chat, a command argument, or the TaskSpindle config file.

Keep the collector running in its own terminal initially. In another terminal, or using
the dashboard's Connect buttons:

```sh
taskspindle subscriptions connect chatgpt
taskspindle subscriptions status --json
taskspindle subscriptions refresh chatgpt
taskspindle subscriptions refresh
```

Connect opens the provider's billing page in your normal Chrome profile, with its existing
sign-ins and Chrome password manager available. It does not create an isolated profile.
The browser must read billing data and verify the account before the connection succeeds.
Without extension setup, Connect still opens the billing page and reports **Setup required**;
opening a page alone never marks the account connected. Reconnect verifies the same account.

Refresh never opens an interactive login prompt. The collector refreshes connected accounts
after six hours and handles explicitly requested refreshes immediately when it is available.
Duplicate requests coalesce; collection operations cannot drive the same account concurrently. Authentication
and account-mismatch failures wait for explicit action. `watch --once` processes at most one
queued or due job, prints its normalized result, and returns a nonzero exit status when
collection fails. With no work queued or due, it exits successfully without starting a browser.

No model request is used to collect billing information. No collector may cancel a plan,
change billing details, purchase credits, or alter TaskSpindle provider availability.

## Browser runtime and configuration

Setup provisions the packaged, lockfile-pinned Node/Playwright helper. It uses an
installed Chrome executable rather than downloading a browser. On WSL, the helper runs
through Windows Node and Chrome. Its runtime lives under
`%LOCALAPPDATA%\TaskSpindle\subscriptions\runtime`.
Normal-profile collection in this version supports Windows Chrome through WSL. Native Linux
retains the explicit `browser_mode = "dedicated"` collector; it does not launch a regular
desktop Chrome process under the collector's systemd service.

Browser cookies and passwords stay in Chrome. TaskSpindle does not import credential files
or read Chrome password/cookie databases. The helper returns only normalized
billing metadata and safe errors, not raw provider responses, payment details, or credentials.
The extension connection token is stored separately in a private `0600` file at
`<TaskSpindle data directory>/subscriptions/extension-token`; it never enters the subscription
database or dashboard responses. The official extension requests browser debugging access;
TaskSpindle's fixed collector code uses its own billing tabs and does not close your other tabs.
Unattended refresh requires the extension connection to be available. Browser or connection
failures retain the last verified observation and eventually show it as stale.
The current extension may focus Chrome or briefly show a billing tab during a scheduled
refresh. Refresh does not wait for sign-in or a connection-approval prompt; unavailable
authentication is reported for reconnection.

The proved normal-profile transport uses Playwright Chrome extension **0.4.0**, paired in the
selected Windows Chrome profile with a private token. On WSL, candidate runtime code forwards
only the named token through a bounded `WSLENV` entry to Windows Node. The token is never placed
in request JSON, command arguments, logs, the subscription database, or this documentation.
Pairing alone is not a completed provider checkpoint or restart test.

Optional settings in the normal TaskSpindle config:

```toml
[subscriptions]
platform = "auto" # auto, windows, or native
browser_mode = "normal" # default; "dedicated" retains the previous separate-profile collector
chrome_profile = "Default" # Chrome profile directory, e.g. "Default" or "Profile 1"
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
Use the profile directory shown at `chrome://version`, not your Google account name.
Modern Chrome does not support launching its ordinary profile with Playwright's persistent
context debugging flags; the extension provides the supported connection to that profile.
The optional `dedicated` mode keeps profiles under
`%LOCALAPPDATA%\TaskSpindle\subscriptions\profiles\<provider>` on Windows, and does not use
your normal Chrome passwords or require an extension token.

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

Current live source boundaries are deliberately narrow:

- ChatGPT requires a recognized `plan_type`, `active_until`, `will_renew`,
  `is_processor_stripe`, and identity from `client-bootstrap`.
- Claude uses its organization bootstrap email and structured subscription response with
  `next_charge_at`. Its automated live diagnostic passed with the current Max plan. The
  collector tracks the current plan only; a scheduled downgrade shown elsewhere in the UI is
  not projected into the current snapshot.
- Grok uses `/api/auth/session` identity and the Billing dialog's actual subscription date;
  its Usage display is credit information.
- The current Google One settings page exposed no subscription date. A Google Play Google One
  card showed cancellation and a future access end, but its purchase channel remains unresolved,
  so app-store billing is still unsupported.

The Google Play observation is documented without its real date; fixtures use shifted dates.
Live cancelled-subscription cases for the other providers remain a stated evidence gap. Never
cancel a subscription merely to fill that gap.

Packaged Connect and Refresh now pass for ChatGPT, Grok, and Claude, including refresh after
restarting the collector. Google returns the unsupported-channel result. Whole-Chrome restart
and live cancellation-state transitions for supported direct-web accounts remain unverified.
See [verification evidence](subscriptions/evidence.md) for the exact scope.

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
whenever the host and its user services are running. Normal-profile collection additionally
requires Chrome to be running in the selected profile. No external notifications are sent.
The installed runtime remains unchanged until the user approves activation of the reviewed
candidate. Extension pairing and provider inspection do not grant that approval.

## Verification

Run the Python subscription tests and the packaged helper's `npm test`. Live verification
must read each connected provider, record its checkpoint, and refresh after closing/reopening
the normal Chrome path. Do not describe provider checkpoint or restart proof as complete until
the controller records that readback.
Do not close unrelated Chrome windows or tabs to run a test.
Use an already-cancelled subscription when available; fixture tests cover cancellation
without cancelling a subscription for testing. Report fixture and live evidence separately.
