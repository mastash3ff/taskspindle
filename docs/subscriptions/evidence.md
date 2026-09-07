# Collector verification evidence

Current candidate evidence was collected on 2026-09-07 using the installed Windows Node,
normal Chrome `Default` profile, and the privately paired Playwright extension 0.4.0.
The installed MCP runtime and dashboard on port 8765 remain unchanged.

| Provider | Acquisition | Verified result |
| --- | --- | --- |
| ChatGPT | Exact `/backend-api/subscriptions` fields `plan_type`, `active_until`, `will_renew`, `is_processor_stripe`; account email from `client-bootstrap`, hashed in the page | Packaged API Connect and Refresh succeeded, including refresh after collector restart. The current personal Pro plan and renewal boundary were verified. |
| Claude | Exact organization bootstrap `account.email_address` and `/subscription_details`; current plan from the isolated Billing dialog | Packaged API Connect and Refresh succeeded, including refresh after collector restart. The current Max plan and `next_charge_at` were verified. |
| Grok | Exact `/api/auth/session` identity and the isolated Billing dialog | Packaged API Connect and Refresh succeeded, including refresh after collector restart. SuperGrok and its date-only renewal boundary were verified. |
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
regression fixtures. Installed-runtime activation and publication require separate approval.
