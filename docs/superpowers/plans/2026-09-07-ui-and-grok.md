# TaskSpindle UI overhaul and Grok hardening

Approved implementation plan, 2026-09-07. Work starts from `2179a5d` on
`feature/taskspindle-ui-and-grok`. Installed runtime activation and publication
require approval of the tested candidate.

## Outcomes

- A dark operator console across Overview, Tasks, task detail, Workers,
  Subscriptions, and Usage, with an accessible responsive shell, local assets,
  themes, command search, useful status hierarchy, and stable polling.
- Read-only overview aggregates and lists; existing task URLs, candidate revision
  checks, review finding links, and task/database protections remain intact.
- Native-only Grok quota checks before dispatch, cached for five minutes with
  cross-process coalescing and credential-metadata invalidation. No inference,
  browser, direct-HTTP fallback, credential copying, or automatic CLI upgrade.
- Correct scoped Grok refusal recognition and sanitized diagnostics, without
  changing cancellation/continuation or interpreting billing refusals as expiry.
- Tested isolated preview, Windows visual/interaction evidence, and explicit
  live-validation limits. No new task controls or paid-worker fallback.

## Work packages and ownership

1. Frontend: `subscription_web` owns packaged static modules and visual design.
2. Native evidence: `subscription_browser` owns checker/cache, store migration,
   availability, MCP, and CLI integration.
3. Refusals: `subscription_browser_review` owns classifier, ACP capture, runner.
4. Overview: `source_status` owns read-only dashboard API/database projections.
5. Guidance: `subscription_runtime` owns source work-pool guidance and docs.
6. Root owns integration, build metadata, independent reviews, browser QA,
   isolated packaging/preview, billing verification, and delivery evidence.

Workers do not edit installed configuration, publish, commit mixed work, or
delegate further. Task database migration is tested only in isolated state.

## Shared contracts

`GET /api/overview` returns `generated_at`, global `counts` (`total`, `active`,
`attention`, `awaiting_review`), bounded `active_tasks` and `attention_tasks`,
`limit`, and `truncated` flags. Default limit is 20, maximum 100. Attention is
RESULT_READY, INTERRUPTED, RECOVERY_AMBIGUOUS, or FAILED with incomplete cleanup.

MCP `capabilities(check_providers=["grok"])` optionally refreshes cached native
observations; default calls are cached-only. CLI `providers --check --provider
grok` uses the same check. Dashboard GETs never start native checks or billing
collection. Native observation fields include source/version, last attempt and
success, usage/window/reset, freshness, and safe error information. All existing
response fields remain. Optional observation writes have honest MCP annotations.

Only explicit current included-quota exhaustion gates new work. Passed reset or
stale observations permit an ordinary attempt unless task evidence still refuses
it. Native checks never clear task auth/access/model refusals. Unsupported native
methods retain unknown quota and explain the installed-version limitation.

## Verification and delivery

Test scope/error handling, unknown and malformed quota, TTL/reset boundaries,
concurrency, invalidation, process cleanup, paid-route exclusion, old-schema
read compatibility, and unchanged task records. Verify all routes and states in
Windows Chrome at mobile/tablet/desktop/wide widths, keyboard/focus/reduced motion,
polling preservation, large diffs, safe rendering, and no unintended requests.

Billing Connect/Refresh and collector restart use isolated subscription state;
whole-Chrome restart is user-performed. Cancellation/expiry transitions use
fixtures unless an existing naturally cancelled account is available. Never
cancel a subscription or close unrelated Chrome windows for testing. Preserve
date-only precision and separate browser/CLI account evidence.

Evidence and preview state are private under
`~/.local/state/taskspindle-ui-grok/20260907/`. That directory holds before/after
checks, sanitized validation results, build provenance, and screenshots.
