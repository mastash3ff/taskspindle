# Muse worker adapter

TaskSpindle includes an experimental first-class `muse` provider using the Muse
Session Protocol (MSP) over `muse serve` stdio. It remains **disabled for all task
modes** pending subscription-route and tool-containment qualification. Installation
and an offline handshake do not enable live dispatch.

## Install and inspect offline

Stage an explicit raw native executable, never the self-updating `muse` wrapper:

```sh
taskspindle setup --provider muse --binary /absolute/path/to/muse-bin-1.3.0-R3401.1
taskspindle providers --json
taskspindle auth muse
```

Setup verifies the exact build and embedded stable MSP schema in an isolated
temporary home, then atomically stages a regular executable at `runtime_dir/muse`.
It reports its SHA-256 byte identity. It does not log in, read credentials, call a
model, or change existing configuration. `auth muse` currently returns a nonzero
status explaining the qualification gate; it does not initiate login.

The supported build is `1.3.0-R3401.1`; stable schema fingerprint:
`sha256:7469c9e352e67def4a59df7e439984d7194fa351e1c8b7abb34060fd977ced81`.
Unknown protocol fingerprints fail closed. Requalify binary/protocol upgrades
explicitly; a matching version string alone is insufficient.

The Docker image accepts an optional `muse` raw executable in its named
`provider-binaries` build context. Managed Compose staging looks for the exact
versioned native binary. Neither route imports the user's Muse authentication,
configuration, or session directory. Existing three-provider contexts still work.

## Public behavior and lifecycle

- `capabilities` advertises `provider="muse"`, `auth="subscription"`, protocol
  `msp`, and a `qualification` object explaining why it is disabled. This is the
  required auth route, not an observed entitlement. Availability is `unqualified`.
- Default dispatch policy disables Muse and leaves the existing role preferences
  unchanged. Its default concurrency is one. Policy edits, `allow_metered`, and
  provider-status overrides cannot bypass qualification. Muse aliases are refused.
- The adapter uses task-owned durable HOME/XDG directories, saves session identity
  before a turn, and journals command intent before sending `turn/start`. It never
  automatically resends an uncertain command. Lost acknowledgments or transport
  loss during an unresolved turn produce `RECOVERY_AMBIGUOUS`.
- Resume requires the exact retained session and refuses unresolved active or
  pending state. It does not substitute a fresh conversation. Approval requests
  receive a protocol receipt and a separate denial; unknown actions never receive
  approval. User-input dialogs are disabled.
- Consult/review launch flags disable both shell execution and native filesystem
  writes. These controls alone do not qualify all tools. Implementation remains
  blocked pending an enforceable path-scope policy. The adapter is not a sandbox.
- Model selections remain separate from observed model identity. Missing telemetry
  remains unknown; subscription-usage observations are not billing-route proof.
  MSP usage uses the `muse_msp` source and does not borrow another provider's prices.

## Unresolved enablement gates

Official [authentication documentation](https://dev.meta.ai/docs/muse-code/auth)
states: "An API key always takes priority over a browser sign-in." The
[subscription documentation](https://dev.meta.ai/docs/muse-code/subscriptions)
binds the subscription to the onboarding-connected Muse Code API key; additional
keys can be pay-as-you-go. Thus neither browser login nor the presence of a key
establishes subscription-only execution.

Experimental `account/read` identifies the effective credential lane but does not
bind it to a current subscription. Stable `usage/read` is a last-observed usage
snapshot. Qualification still requires authoritative current entitlement bound to
the active credential and proof that another credential or metered route cannot
be selected. There is deliberately no local attestation or force-enable switch.

Separately, test enforcement of recursive-agent/workflow/MCP restrictions,
read-only behavior, protected control paths, and implementation path prefixes in
the actual container. `session/start.config` only recognizes `mcpServers` in this
schema; arbitrary control settings there are ignored. Ordinary approval modes do
not establish filesystem scope, and denying approval requests alone does not
prevent tools that run without requesting approval.

Only after these gates pass should a disposable-repository model smoke test cover
consult, implementation, independent review, acceptance, cancellation, and crash
recovery. Activation uses the managed cutover process without killing active MCP
transports. No live activation is part of staging this adapter.

## Language decision

The implementation stays in Python. A Go rewrite is outside this change. Measure
orchestration CPU, memory, startup, dispatch latency, and SQLite contention apart
from provider execution before proposing a component migration.
