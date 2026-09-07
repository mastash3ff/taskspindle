# Collector verification evidence

This file records evidence for the subscription-tracking candidate. No provider is marked
live-verified merely because a parser or browser helper exists.

| Provider | Acquisition design | Live verification |
| --- | --- | --- |
| ChatGPT | Authenticated billing response: `active_until` and `will_renew`; CodexBar reference | Dedicated Windows Connect attempted; no account/billing observation confirmed. Sign-in and reopen/refresh proof remain pending. |
| Claude | Authenticated billing page, verified account and explicit renewal/access-end wording | Pending dedicated-profile sign-in and observed page contract |
| Google AI | Google One billing page; one record for shared Gemini/AGY benefits | Pending dedicated-profile sign-in and observed page contract |
| Grok | Direct SuperGrok billing page; X/app-store billing excluded | Pending dedicated-profile sign-in and observed page contract |

ChatGPT source reference:
[CodexBar subscription capture](https://github.com/steipete/CodexBar/blob/main/Sources/CodexBarCore/OpenAIWeb/OpenAISubscriptionMetadata.swift).
The collector must preserve attribution for adapted MIT-licensed code.

OAuth token expiry and quota resets are not evidence of subscription expiry. Unknown
provider schemas must yield a visible verification error rather than invented dates.
Live cancellation behavior must remain explicitly unverified unless observed on an existing
cancelled subscription. Never cancel a subscription for validation.

## Observed integration evidence (2026-09-07)

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

## Candidate boundary

The tested wheel is `taskspindle-0.2.0-py3-none-any.whl`, SHA-256
`5aaee471de8a3c83d6e4c1f5230e4443b9b6da946a18849527af9d7aff0a0a27`.
All 53 packaged source/asset files match the feature checkout; the dedicated Windows helper
matches those wheel assets. The isolated preview uses the wheel and its own state/configuration.
The installed MCP runtime and the existing dashboard on port 8765 have not been activated from
this candidate. Activation/publication approval follows successful live provider verification.
