# Collector verification evidence

This file records evidence for the subscription-tracking candidate. No provider is marked
live-verified merely because a parser or browser helper exists.

| Provider | Acquisition design | Live verification |
| --- | --- | --- |
| ChatGPT | Authenticated billing response: `active_until` and `will_renew`; CodexBar reference | Earlier dedicated Connect did not confirm billing. Normal-profile extension connection, billing extraction, and reconnect/refresh remain pending. |
| Claude | Authenticated billing page, verified account and explicit renewal/access-end wording | Pending normal-profile extension connection and observed page contract |
| Google AI | Google One billing page; one record for shared Gemini/AGY benefits | Pending normal-profile extension connection and observed page contract |
| Grok | Direct SuperGrok billing page; X/app-store billing excluded | Pending normal-profile extension connection and observed page contract |

ChatGPT source reference:
[CodexBar subscription capture](https://github.com/steipete/CodexBar/blob/main/Sources/CodexBarCore/OpenAIWeb/OpenAISubscriptionMetadata.swift).
The collector must preserve attribution for adapted MIT-licensed code.

OAuth token expiry and quota resets are not evidence of subscription expiry. Unknown
provider schemas must yield a visible verification error rather than invented dates.
Live cancellation behavior must remain explicitly unverified unless observed on an existing
cancelled subscription. Never cancel a subscription for validation.

## Previous dedicated-mode integration evidence (2026-09-07)

The following checks describe the earlier dedicated-profile build (`0e1849a`). They do not
establish successful normal-profile collection through the newly selected extension bridge.

- Windows Node `v24.14.0`, installed Windows Chrome, and pinned `playwright-core 1.63.0`
  successfully provisioned and launched the dedicated collector runtime.
- A real systemd user unit successfully discovered and executed Windows Node without a
  manually supplied `WSL_INTEROP` socket.
- Windows Chrome rendered the isolated dashboard at desktop and mobile widths. Synthetic
  API fixtures showed all four providers and renewal, cancellation, stale, reconnect, and
  unsupported-channel states without JavaScript errors or horizontal overflow.
- The actual Python API, queue, worker, and store were exercised together: duplicate requests
  coalesced, a cancelled observation produced a calendar-day countdown, and a subsequent
  authentication failure retained the prior result while task-database bytes stayed unchanged.
- Packaged JavaScript results are checked against Python validation to detect schema drift.
- The final wheel passed **860 Python tests**, with one existing opt-in real task-unit test
  skipped. The browser helper passed **23 Node tests**. Ruff, JavaScript syntax, wheel-asset
  inspection, and the generated systemd service definition also passed validation.
- A separate disposable Windows profile passed a real collector stop/restart check: SIGTERM
  stopped the owned browser tree, a restart recovered the stale ownership record, and no
  matching Node/Chrome process or collector lock remained. This tested process lifecycle,
  not authenticated billing collection.

These are infrastructure and fixture checks, not proof of a provider's authenticated billing
contract. Claude, Google AI, and Grok DOM fixtures are explicitly synthetic; their selectors and
wording still need verification on the user's signed-in billing pages. No provider has passed
the required successful billing collection followed by browser close/reopen and refresh.

The first ChatGPT attempt used the initial helper and returned `PARSE_CHANGED`; later hardening
added stricter settings-container isolation and explicit logged-out navigation detection. No
credentials, raw billing responses, payment details, or screenshots of authenticated billing
pages were saved as fixtures. Existing-cancelled-subscription live proof remains outstanding.

## Normal Chrome change

The user requested Connect use normal Chrome with its Google profile and saved passwords.
The default browser mode now uses the official Playwright Chrome extension; the previous
dedicated helper remains opt-in. A read-only check found one signed-in Windows Chrome
profile, `Default`, and no installed Playwright extension. No Chrome password or cookie
database was read. Extension installation and the private connection token are prerequisites
for live automatic collection. Opening a normal billing tab without them is not a successful
subscription connection.

Normal-mode source checks passed: **99 Python tests** across subscriptions and the existing
web API, and **35 Node tests** on both Linux Node and actual Windows Node. These cover the
private token handoff, regular Chrome launch arguments, preserved task read-only behavior,
account mismatch/authentication failure, only-owned-tab cleanup, late tab creation during
cancellation, and the pinned Playwright extension factory. They do not replace an authenticated
live extension connection. Ruff, JavaScript syntax, and diff checks also passed.

The supported existing-profile mechanism and per-profile connection token are documented in
the [official extension guide](https://github.com/microsoft/playwright/blob/main/packages/extension/README.md).
Chrome restricts debugging switches against its ordinary profile; changing the persistent
context's profile path alone cannot supply this behavior.

## Candidate boundary

Exact build checksums and current verification results are recorded outside the wheel in the
local candidate build ledger. The preview uses the candidate wheel and its own state/configuration.
The installed MCP runtime and the existing dashboard on port 8765 have not been activated from
this candidate. Activation/publication approval follows successful live provider verification.
