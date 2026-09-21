# Dormant provider enrollment

OpenCode Go and native Muse are prepared but disabled. No policy edit,
`first_class` flag, credential, `allow_metered`, or enrollment evidence JSON can
activate them. Dispatch, catalog/access checks and doctor do not start either
binary. The existing Muse MSP fixture tests remain offline protocol tests.

Run `taskspindle enrollment-check opencode-go` or
`taskspindle enrollment-check muse` to see required evidence. `--evidence FILE`
accepts local JSON references and reports missing evidence and inconsistent
billing claims; it neither verifies those claims nor changes any state. Each
reference has `source` and `sha256`. The Go `zen_use_balance_disabled` reference
also needs `value: true`. Evidence files must contain no credentials.

A later enrollment implementation must verify the pinned native binary,
account and credential binding, subscription-only billing, model catalog,
account/quota scope, nonrecursive execution, tool containment, cancellation and
continuation. Muse additionally requires proof that native login is selected
instead of an API key. Its existing API-key precedence safeguards remain in
force. Only the native CLI route is prepared.

OpenCode's official CLI documentation describes `opencode acp` as an ACP server
that communicates over stdin/stdout. TaskSpindle's dormant command adds
`--pure`, documented as “Run without external plugins”. The candidate environment
in `opencode_go.isolated_environment` uses an empty private HOME/XDG tree,
Go-only providers, no MCP/plugin declarations, disabled model fetching and
updates, and denies all tools including subagent tasks. It copies no user
configuration or credentials. This is preparation only: a future sandbox must
hide project `.opencode` directories, and qualification must prove that the
pinned version cannot load user hooks, plugins, MCP servers or recursively
spawn agents. No OpenCode binary has been executed to qualify this proposal.

The official Go documentation says “You subscribe to OpenCode Go and get your
API key.” Authentication syntax does not identify billing type. Go model IDs
must use `opencode-go/<model-id>` and the credential must be bound to Go.
The Zen console's **Use balance** option must be verified disabled: when enabled,
Go can charge Zen balance after subscription limits. No credential is enrolled,
account purchased, billing setting changed, or model inferred by this change.

Sources checked 2026-09-21:

- https://opencode.ai/docs/cli/#acp
- https://opencode.ai/docs/cli/#global-flags
- https://opencode.ai/docs/go/#how-it-works
- https://opencode.ai/docs/go/#usage-beyond-limits
- https://opencode.ai/docs/go/#endpoints

## Profile metadata and compatibility

`Profile.auth` retains the existing API (`oauth`, `api_key`, legacy
`subscription`). `auth_method` normalizes legacy `subscription` to
`native_login`. New fields are `billing_type` (`unknown`, `subscription`,
`metered`), `account_scope`, `quota_scope`, and `model_family`.

The existing Claude, Grok and AGY builtins explicitly declare subscription
billing. Dormant routes remain unknown. Custom profiles default to unknown;
unknown and metered billing fail closed with `allow_metered=false`, even with
OAuth authentication. Existing aliases retaining the same OAuth adapter and
environment inherit their builtin's billing metadata. A subscription declaration on
other custom commands is normalized to unknown. An API-key profile cannot gain
subscription eligibility from handwritten metadata. No active API-key subscription
route is registered yet. Billing metadata is not evidence that an account actually
has an entitlement. Existing authentication and
credential context checks still apply. Quota/account metadata is exposed for
future enrollment; existing durable status keys are retained.

`Profile.family` continues to identify the transport for persisted records.
`underlying_family` uses explicit `model_family` or recognized model lineage
for review independence, so Grok through Go cannot review native Grok and
Claude through AGY cannot review native Claude. Unknown lineage is not proof
of independence; dormant transports remain excluded from admission.

## Catalog and model promotion

`policy_promotion.stage` and `taskspindle policy promote stage REQUEST.json`
accept optional `verified_catalogs` alongside the existing request fields:

```json
{
  "grok": {
    "advertised_models": ["grok-4.7", "EXAMPLE-NEW-ID"],
    "advertised_efforts": ["medium", "high"],
    "source": "path-or-primary-source-reference",
    "evidence_sha256": "64-lowercase-hex-characters"
  }
}
```

The caller must independently verify these inputs; staging never fetches a
catalog. Include every model/effort still used by existing roles and ladders.
Catalog and selection are validated together and bound in the immutable staged
record. Apply checks the runtime image/binary identities and revision CAS;
rollback restores the captured catalog and selections. Existing stage records
without `verified_catalogs` remain readable. Promotion requires subscription
billing, a registered builtin route identity and account/quota binding, and a
recognized adapter with model-selection capability. Aliases retain existing
admission and steering behavior. `first_class` and OAuth alone no longer admit
arbitrary commands. Adding another adapter
requires implementation and qualification, not a configuration boolean.
