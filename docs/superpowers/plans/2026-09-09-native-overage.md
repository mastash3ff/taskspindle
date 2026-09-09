# Native overage implementation

Approved scope: provider-managed native extra usage for Claude, Grok, and Antigravity. Subscription-first; existing OAuth workers and provider billing controls. No new API routes, cross-provider transfer, custom billing caps, purchases, activation, or publication.

## Task 1: Backend policy, persistence, admission, and continuation

Ownership: src/taskspindle/{config,providers,models,store,quota,service,orchestrator,runner,agy_cli_policy}.py and new native_overage.py; relevant Python tests except test_usage.py and test_grok_checks.py. Do not edit usage.py, grok_checks.py, web/, docs/, examples/, or workspace-config.

Implement [native_overage] mapping exact profile IDs to observe_only (default) or provider_managed. Reject unknown IDs, malformed values, and native overage for API-key profiles. Exact-profile opt-in, not automatic alias inheritance. The account is the spending authority: observe_only preserves existing quota admission and AGY credits-off; it is NOT a Claude/Grok per-task charge prohibition. Keep allow_metered separate.

Snapshot effective policy and configuration fingerprint on every turn, recheck before dispatch/prompt/continuation. Do not silently change an in-flight turn. Revoked policy blocks pending overage work. A newly explicitly configured provider_managed policy may authorize a quota continuation on an older observe_only task, with a new per-turn snapshot; never rewrite historical authorization.

Add additive schema migration for turn authorization, safe native overage observations, and atomic first-attempt claims. Historical billing is unknown. Bind claims to provider/auth context/model-applicable exhaustion evidence and policy fingerprint. After confirmed successful native service under overage admission, normal concurrency may proceed for that evidence; after refusal, no repeated attempt against unchanged evidence. Crash/cancellation/restart must not duplicate inference or leave an unexplained perpetual claim. Preserve ambiguity until liveness is settled.

One shared admission projection for capabilities/start/dispatch/continue/prompt. Distinguish included-allowance exhaustion from paid-credit/cap exhaustion, short-term rate limiting and unknown failures. Only affirmative included-exhaustion evidence is overage-eligible. Preserve auth/access/model/hard throttle blocks, including Grok 402 and credit-specific 403. With standing provider_managed policy and included exhaustion, a bounded native attempt may rely on provider enforcement even when balance is not observable; never label balance available without evidence. Existing default quota retry/recovery contracts remain.

Consume Claude capture.rate_limits overage fields (overageStatus, overageDisabledReason, overageResetsAt, isUsingOverage / overageInUse) using an allowlist and normalization. Persist observations without arbitrary raw text or credentials. An overage-funded/admitted success never clears included exhaustion or a newer sibling refusal. Separate service success from included allowance recovery. Existing normal reset/recovery rules may still resolve included restrictions.

Pass effective policy to AGY isolated launcher to set useG1Credits only for provider_managed; retain sandbox/permissions. Source API callers retain default false for compatibility.

Narrow continue_task support for an eligible included-quota FAILED task, unchanged identity, resumable original session, prior worker definitely terminated and valid state_version. Preserve task/session/worktree/partial files/output and earlier error event; add a new turn and continuation prompt, never replay original. No generic FAILED retry. Caller may invoke under standing authorization, without a new human spending approval. A repeated native refusal ends the attempt until changed eligibility. Cancellation and ambiguous liveness prevail.

Expose additive native_overage objects through capabilities, task_status and task_result: policy, policy_fingerprint, control_scope (account for Claude/Grok, worker for AGY), eligibility (included/overage/blocked/unknown), admission_reason, billing_classification (included/native_overage/mixed/unknown), source/time and observed overage fields. Absence remains unknown, not included/free. Observe-only must not hide an observed native charge. Send root exact exported projection/store interface early so root can implement usage report and UI without overlapping your files.

Meaningful focused tests: default/opt-in/invalid config; migration preservation; launch settings; included vs hard blocks; shared native/stored quota projection; concurrency/restarts/cancellation; overage success preserving exhaustion/newer failures; revalidation on resume; same-session partial-work continuation and duplicates; missing session and ambiguous liveness; historical unknowns. Do not run live providers. Run focused tests and Ruff on owned files; root runs the integrated full suite once. Do not commit shared working tree; report changed files/tests in scratch report. Root owns commits.

## Task 2: Diagnostics, usage reports, and UI (coordinator)

Extend Grok native billing diagnostics to safely retain documented balance/cap/auto-topup fields; keep unknown units unknown. Reuse existing read-only RPCs. Never mutate provider settings. Aggregate per-turn native-overage snapshots in usage_report, preserving existing estimates and filters, separating account balance from task charges. UI Workers/Usage exposes policy, control scope, eligibility, safe native fields, freshness, unknown/historical states, and unverified live qualification.

## Task 3: Documentation and work-pool instructions (coordinator)

Configuration examples and documentation explain account-authoritative opt-in, default behavior, provider-specific limits, native rollover vs error recovery, no purchase automation. Update workspace-config managed work-pool source to consume explicit native_overage eligibility while preserving hard blocks, API key prohibition, model/provider constraints, and one eligible continuation under standing opt-in. Prefer suitable included-allowance providers for new unconstrained work; retain ongoing worker identity. Do not activate installed skill.

## Task 4: Review, combined verification, and delivery

Independent consequential review followed by fixing/retesting findings. Run uv run ruff check ., uv run pytest -q, node --test tests/web_ui/*.test.mjs and applicable workspace-config validation. Deliver local commits plus qualification checklist and rollback/activation instructions. No live migration/runtime activation/billing changes/push/PR. Real paid qualification remains separately approved and unverified until observed.
