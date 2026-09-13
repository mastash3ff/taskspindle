# Native availability and hybrid recovery

User-approved implementation plan. The conversation approval covers implementation,
verification, browser-feature retirement, managed policy changes, and staged runtime rollout.

## Global constraints

- Use existing OAuth CLI checks and real necessary work only: no browser billing,
  synthetic model probes, manual dates, API-key fallback, or account spending changes.
- Preserve task/provider/model attribution, partial work, grants, quality profiles,
  shared capacity, explicit provider/model choices, and independent review.
- Initial access refusal permits three recovery retries, after 5, 15, and 60 minutes
  measured from the preceding refusal. Then hold indefinitely until new relevant
  positive evidence authorizes one trial. Failure of that trial holds again.
- Fresh native denials and future quota resets cannot be bypassed. Native status
  checks may continue while model work is held. No pending work means no trial.
- Refresh timestamps, unchanged login/catalog, credential metadata changes alone,
  and elapsed reset deadlines never release an indefinite hold.
- Only successful model access resolves the applicable refusal; code verification
  failure is not provider-access failure. Persist claims/counters through restart.
- One shared-account trial across clients and aliases, enforcing scope on starts,
  queued execution and continuations. Keep account and exact-model refusal separate.
- Remove browser billing code/UI entirely. Preserve historical private data inert
  and do not delete browser profiles or shared extensions.
- Keep one-line imperative commits. Tests never call a live model.

## Task 1: Native checks and hybrid recovery

Own native-check normalization/caching, provider error persistence, durable recovery
episodes and admission across CLI/MCP worker execution. Implement tests first with
real stores/fake native transports and controlled clocks.

Cache Claude auth status, Grok session-free billing, and AGY authenticated model
catalog results for five minutes across clients. Keep safe explicit auth-negative
classification distinct from timeout/malformed/unsupported unknown evidence. Retain
AGY model IDs to distinguish requested-model membership. Fix normalized AGY access
error persistence. No new provider CLI or direct HTTP integrations.

Add [provider_recovery] exact-profile policies "manual" (default) and "hybrid".
Managed deployment enables hybrid for claude/grok/agy. Reuse manual APIs compatibly.
Expose availability.automatic_recovery and exact model projections with fields:
policy, state (eligible/cooldown/trial_ready/trial_running/held), attempts_used,
attempts_remaining, next_attempt_at, hold_reason, active_task_id, episode_id,
evidence_revision. Additional internal fields are allowed where required.

Three failed recovery turns exhaust an episode. Do not reset the episode on each
refusal. Positive evidence after hold is scoped: explicit native unauthenticated
to authenticated; applicable exhausted quota to available (or an available new
period); an absent exact model becomes advertised; successful real model turn.
Consume each relevant semantic transition once; unrelated or unknown-to-positive
diagnostics cannot release a hold. Capability reads do not create tasks or claims.
Known native negatives continue blocking; their diagnostic checks consume no model
retry allowance. Retry admission must be atomic with the task/turn claim and be
rechecked before prompting. Finish/reconcile claims on all terminal paths.

Add an additive schema migration preserving existing restrictions/history. Existing
unresolved refusals get zero new automatic attempts consumed. Check concurrency,
aliases, reset windows, start/queue/continue, restarts, and failure/success scope.
Own core Python files, web/db.py only if projection needs it, and focused core tests.
Do not edit cli.py, web/app.py, web static files, subscription files/tests, or docs;
those are owned by Tasks 2/3 and the coordinator.

## Task 2: Retire browser billing and display worker recovery

Remove subscriptions Python package, bundled _subscription_browser resources,
subscription CLI dispatch/parser, web APIs/guards, dashboard page/nav/overview strip,
unused POST helper, subscription-specific tests and current-feature documentation.
Retain native CLI quota/billing/overage telemetry, Workers, tasks, and usage features.
Move existing provider-projection tests out of subscription tests before deletion.
Redirect #/subscriptions to #/workers; billing API returns 404; old CLI fails without
browser activity. Old config may be harmlessly ignored; preserve historical docs.

Extend Workers UI with the Task 1 availability.automatic_recovery fields (policy,
state, attempts_remaining, next_attempt_at, hold_reason, active_task_id). Gracefully
handle absent fields on old runtime responses. No new browser automation or polling
service. Keep dashboard GETs read-only. Add focused UI/route/API tests first.

Own subscription packages, cli.py subscription removal only, web/app.py, web/static,
subscription/web tests and user documentation except the coordinator's plan file.
Do not edit native core modules or managed repositories. Tests never call models.

## Task 3: Integrate managed routing and deploy verified runtime

Update workspace-config work-pool policy and deployed copies: refresh native checks
for all compatible candidates, honor automatic recovery eligibility and scoped
claims without manual permits, choose eligible alternatives, inspect partial work,
record replacements, keep exact user constraints and subscription-only defaults.
Update CLI recovery rendering and task status public projection as needed after
integrating Tasks 1/2. Enable hybrid exact-profile policies in codex-runtime, remove
subscription browser settings and obsolete activation instructions.

Test policy consuming agents on representative controlled scenarios, managed scripts
against fixtures, and all combined Python/JS tests/lint/build once integrated.
Review each task and the whole final change independently. Stage immutable revision
runtime on isolated state; preserve live databases/config/registrations, drain
workers, retire any collector service, align CLI/MCP/dashboard WSL and Windows pins.
Verify running executable, schema, retained state, fresh native checks, and UI/API
readback. No synthetic model tasks, publication, shared-branch merges or destructive
cleanup is implied. Record concrete blockers if external authority is required.
