# Collector verification evidence

Current candidate evidence was collected on 2026-09-07 using the installed Windows Node,
a selected normal Chrome profile, and a privately paired Playwright extension 0.4.0.
The installed MCP runtime and dashboard on port 8765 remain unchanged.

| Provider | Acquisition | Verified result |
| --- | --- | --- |
| ChatGPT | Exact `/backend-api/subscriptions` fields `plan_type`, `active_until`, `will_renew`, `is_processor_stripe`; account email from `client-bootstrap`, hashed in the page | Packaged API Connect and Refresh succeeded, including refresh after collector restart. A recognized active personal plan and renewal boundary were verified. |
| Claude | Exact organization bootstrap `account.email_address` and `/subscription_details`; current plan from the isolated Billing dialog | Packaged API Connect and Refresh succeeded, including refresh after collector restart. A recognized active personal plan and next-charge boundary were verified. |
| Grok | Exact `/api/auth/session` identity and the isolated Billing dialog | Packaged API Connect and Refresh succeeded, including refresh after collector restart. A recognized active paid plan and date-only renewal boundary were verified. |
| Google AI | Google One settings, followed by an account-bound Google Play subscription lookup when settings have no date | The exact Google One product row was identified automatically. The candidate reports Unsupported billing channel. Direct-web Google AI date collection remains unverified; purchase-channel clarification is pending. |

The candidate's SQLite store contains successful observations for the three verified providers.
No fake snapshots were inserted to establish these results. Manual requests coalesced, account
identifiers remained stable, and the user service resumed successful collection after restart.
Each operation reattached through the extension and closed only its own billing tab. The user's
whole Chrome browser was not shut down, so whole-browser restart validation remains open.
Normal-mode background checks require Chrome to be running in the selected profile.

## Source and interpretation boundaries

ChatGPT collection adapts the MIT-licensed
[CodexBar subscription approach](https://github.com/steipete/CodexBar/blob/main/Sources/CodexBarCore/OpenAIWeb/OpenAISubscriptionMetadata.swift).
Attribution is retained in the packaged NOTICE. Only recognized personal plans and typed billing
fields are projected; account email is hashed and masked inside the provider page.

Claude bootstrap capture permits only the observed query keys `statsig_hashing_algorithm`,
`growthbook_format`, `cache_bust`, and `include_system_prompts`. Billing permits `cached`.
Values and unrelated response contents are never retained. Renewing requires active status,
explicitly null ending fields, and a valid next charge. Pending cancellation from a non-null
`plan_ending_at` or `plan_ending_before` follows the independent
[Aliax implementation](https://github.com/LandonDev/aliax/blob/df34e2771d7e3fe82aba11e39c9b57cbeab93003/src/main/adapters/claude.ts#L161-L205)
and [ClaudeTuner implementation](https://github.com/chaehyun2/claudetuner/blob/064b53d7440fb56dfbbb5888d14007eabafa0ac1/bg/plan.js#L36-L60).
This mapping has synthetic regression coverage, not live cancellation proof or a published
Anthropic schema. Conflicting ending fields fail closed. A positively labeled scoped DOM
cancellation/end date can supply the fallback after captured account identity is verified.
Scheduled downgrades do not replace the current plan in the snapshot.

Grok's user-provided Connect link remains `?_s=usage`. Collection uses `?_s=billing` because
Usage contains credit information rather than the subscription boundary. Credit expiry and
quota resets never supply billing dates.

Google One remains one record for shared Gemini/Antigravity personal benefits. Its settings
page exposed no date. A Google Play Google One card visibly showed cancellation and a future
access end, but app-store billing remains outside this version. The classifier identifies only
the exact Google One product row, verifies the same account across both Google origins, and
returns the unsupported-channel status without projecting that date. Other expired app rows
are ignored. Apple and X Premium-derived billing also remain unsupported.

## Validation

- **105 Python tests passed** across subscriptions, existing read-only web APIs, and configuration.
- **86 browser tests passed on Linux and Windows Node v24.14.0**. Windows ran each test module
  through a file-URL import because Node's test discovery did not resolve the UNC source paths.
- The packaged preview rejected invalid CSRF tokens and foreign origins; duplicate Connect
  and Refresh calls returned the same queued job identifiers.
- Real Connect, Refresh, and refresh after a collector restart succeeded for ChatGPT, Claude,
  and Grok. Google returned the expected unsupported-channel result.
- The Windows dashboard renders the three verified renewal states and the Google limitation;
  it exposes three usable Refresh controls, masked account labels, and no JavaScript errors.
- Packaged source and installed files are compared byte-for-byte, and Windows helper assets
  are checked against the wheel. Build hashes and local evidence paths live in the candidate ledger.

The earlier implementation also passed 860 Python tests (one pre-existing opt-in task-unit test
skipped), desktop/mobile fixture rendering, stale-result retention, countdown boundaries,
provider/account changes, queue concurrency, and task-database read-only checks. These historical
checks complement the focused current suite; they are not represented as a new full-suite run.

Live cancellation and expired/free/no-subscription transitions for supported direct-web accounts
remain unverified. Their regression fixtures use synthetic identities and shifted dates.
No subscription was cancelled or modified for testing. No credentials, raw provider payloads,
payment details, real account identifiers, or real billing dates are included in these docs or
regression fixtures. At the time of this evidence capture, activation and publication had not occurred.

## Opt-in scheduling and warning follow-up

The lightweight follow-up makes scheduled browser collection opt-in through the strict boolean
`[subscriptions] scheduled_refresh` setting, defaulting to false. Manual Connect and Refresh
remain available. Queue origin is persisted as manual or scheduled; older unclassified Refresh
jobs fail closed until a new manual request, and disabling scheduling skips outstanding automatic
work without changing current snapshots, history, or provider errors.

Fresh confirmed access-end observations add normalized seven-day and one-day warning values.
Date-only warnings use the provider timezone. Renewal, stale, passed, expired, missing, and
unsupported records cannot produce an upcoming-end warning, and subscription state remains
separate from native worker availability.

## Subscription-aware worker selection verification

The worker-selection candidate passed **936 Python tests**, with one optional real-systemd
worker-unit test skipped because `TASKSPINDLE_REAL_SYSTEMD` was not enabled. Ruff and diff
whitespace checks passed. Coverage includes personal Claude Pro/Max authentication, scoped
account/model refusals, quota-reset eligibility, stale/unknown observations, concurrent status
updates, startup refusal checks, and read-only access to the existing task database format.

Noninteractive native checks on the validation host reported cached eligible Claude personal-plan
authentication and a nonempty AGY Gemini catalog. These checks establish neither current entitlement
nor browser/CLI account identity. Grok has no supported noninteractive native status check;
its availability still uses observed task outcomes. No synthetic inference requests were sent.

The rebuilt isolated dashboard passed desktop and mobile rendering checks in headless Windows
Chrome with no page errors, horizontal overflow, or mutation requests. Clearly labeled browser
fixtures verified the one-day/seven-day warnings, expired authentication, and elapsed quota
reset display. This follow-up did not open extension connection tabs or refresh provider billing.
Cancellation and other subscription transitions retain fixture coverage rather than new live proof.

The packaged preview has scheduled collection disabled, no active collection jobs, and preserves
the earlier confirmed subscription snapshots and observation history. Its isolated task database
is absent, so worker availability correctly displays unknown. Source, wheel, and installed preview
files were compared byte-for-byte across 65 packaged files. The production dashboard remains
read-only against schema 4, and the installed MCP runtime and installed work-pool skill are unchanged.

The exact build ledger, full test output, normalized native-check results, Windows screenshots,
and browser verification script are retained in the local delivery directory
`~/.local/state/taskspindle-subscription-implementation/20260907/worker-selection/`.
At the time of this evidence capture, candidate task-database schema 5 had been tested but had not
been activated in the installed runtime, and the revised work-pool guidance was not installed.
