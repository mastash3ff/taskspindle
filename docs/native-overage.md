# Native extra usage

TaskSpindle keeps native OAuth CLI sessions and lets provider account settings
control extra usage spending. It does not switch a failed task to another provider,
create API clients, buy credits, change a billing cap, or configure automatic top-up.

## Policy and authority

| Exact-profile policy | Admission after included exhaustion | Spending control |
| --- | --- | --- |
| `observe_only` (default) | Existing quota gates remain | AGY credits off; Claude/Grok account settings still apply |
| `provider_managed` | Bounded native attempt when eligible | Provider account allowance, credits, and account limits |

Set policies in `[native_overage]` in `config.toml`. Entries use exact profile IDs,
including aliases. Omitted entries default to `observe_only`. `allow_metered` remains
an independent API-key-only gate; setting it false does not prove an OAuth turn was
free. The dashboard observes policy and billing; it has no spending controls.

A turn captures its policy fingerprint and authentication context. TaskSpindle
rechecks pending authorization at dispatch and before prompting. Revocation prevents
pending overage admission; it does not interrupt a prompt already in flight. To stop
account-level spending, change the provider account settings as well.

## Admission and continuation

Only affirmative included-allowance exhaustion can qualify. Login or access errors,
model denial, short rate limits, unknown refusals, and exhausted paid allowance do
not become overage authorization. A first native attempt is claimed atomically for
its account context, model, quota evidence, and policy. Concurrent callers cannot
spend duplicate first attempts. Refusal remains blocking until eligibility changes;
an in-flight sibling success cannot erase it. Native overage success does not mark
the included allowance replenished.

```text
Included allowance available -> ordinary OAuth turn
Included allowance exhausted -> inspect exact policy and current eligibility
  observe_only               -> wait / report quota refusal
  provider_managed + eligible -> bounded native attempt
    accepted                 -> continue under provider account controls
    refused                  -> retain evidence and stop retrying
```

An eligible quota-failed task may use `continue_task` with its current state version
and a continuation prompt. Its worker must have stopped, and its original session,
provider, model, authentication context, and retained worktree must remain usable.
TaskSpindle preserves the partial result and prior turn history, then creates a new
turn in that session. Missing or changed context is a refusal, not permission to
replay the original request or transfer it elsewhere. Ordinary failed tasks remain
ineligible for this path.

## Observations

Turn records and task results expose `native_overage`: policy and fingerprint,
control scope, admission reason, eligibility, billing classification, observation
source and timestamp, and safe provider fields. Classification is `included`,
`native_overage`, `mixed`, or `unknown`; authorization alone never establishes a charge.
Historical turns remain unknown. Usage reports count all turns, including turns
without token telemetry, separately from token estimates.

Claude's native rate-limit events provide extra-usage status, disabled reason,
reset, and in-use flags. Missing, malformed, or conflicting in-use fields remain
unknown. Grok native diagnostics read prepaid balance, on-demand used/cap, and
optional automatic-top-up settings. Documented billing amounts use USD cents;
missing values remain unknown and explicit zero stays zero. These are shared account
observations, not per-task charges. The on-demand feature flag is not proof that an
account authorized spending. Grok `costUsdTicks` remains raw and unconverted.
AGY's isolated launcher enables `useG1Credits` only for an authorized turn and
retains its tool and sandbox restrictions. Credit permission alone does not identify
whether any credits were charged.

## Qualification and rollout

Offline verification covers default behavior, validation, admission and revocation,
claims, failed-task continuation, provider observations, migration, and reporting.
Live acceptance of native extra usage remains **unqualified** until a separately
authorized real task demonstrates it for each intended provider. A successful
fixture or CLI setup is not live billing evidence.

Before activation:

1. Review the local commits and test evidence. Drain active turns and capture the
   current executable/configuration versions and a consistent SQLite backup.
2. Install the reviewed code and managed work-pool guidance only after deployment
   approval. Schema 9 adds nullable turn metadata plus claim/observation tables;
   historical rows remain unknown. Dashboard reads never migrate the database.
3. Keep defaults first and verify ordinary included-allowance behavior. Enable an
   exact profile's `provider_managed` policy only under explicit spending authority;
   record the provider account settings used as the limit.
4. Qualify with one necessary real task, then verify the actual session, admission
   evidence, billing observations, account readback, and any charge independently.
   Do not create disposable prompts to test billing. Record unsupported/missing
   telemetry as unknown. Exercise refusal and revocation with offline fixtures.

For rollback, remove the opt-in entries (or set `observe_only`) to block pending
native admission. Drain in-flight work before reverting the executable. Do not drop
additive schema-9 data or restore a backup over newer task history; use the backup
only as an explicitly approved recovery with a reconciled task inventory. Disable
extra usage in provider account settings if account-level spending must stop.
