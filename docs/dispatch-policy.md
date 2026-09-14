# Dispatch policy

TaskSpindle never chooses a provider. Codex names `provider` on every `start_task`, nothing is
retried on another provider, and a refusal is reported rather than worked around. The dispatch
policy does not change that. It is operator data that tells the caller how the operator would
like work spread across `claude`, `grok` and `agy`, which model and effort each role should
use on each provider, and where locally observed usage stands against targets and budgets.

The policy lives in the state database (`dispatch_policy`, schema 11), not in `config.toml`.
It is read fresh on every `capabilities()` call, every `dispatch_policy` call and every
`start_task` admission, so an edit made in the dashboard or with `taskspindle policy` takes
effect without restarting the MCP server. `[concurrency]`, `[native_overage]` and
`[provider_recovery]` stay in `config.toml`; the dashboard shows them and never writes them.

## What the policy can express

| Knob | Where | Effect |
| --- | --- | --- |
| `enabled` | per provider | `false` reports the provider as `paused`. Advisory: Codex skips it; the server still admits an explicit task. |
| `target_share` | per provider, 0–100 | Desired share of turns. Compared with the observed share over `share_window`; produces `share_state` and `under_target_order`. Enabled shares may sum to less than 100; they may not exceed it. |
| `budgets.<day|week>` | per provider | `turns` and/or `tokens` ceilings over a rolling 24 h / 7 d window of this host's own recorded turns. Advisory unless `enforce`. |
| `budgets.<window>.enforce` | per budget | An exhausted enforced budget refuses **new** `start_task` admission on that provider with `POLICY_BUDGET_EXHAUSTED` until the window rolls. Already queued or running tasks are untouched. |
| `allowed_modes` | per provider | Advisory hint narrowing `consult`/`review`/`implement`; must be a subset of what the profile serves. |
| `advertised_models`, `advertised_efforts`, `models_without_effort` | per provider | The value lists role selections are validated against for Claude and Grok, and the choices the dashboard offers. AGY selections are validated by the Gemini ID grammar instead. |
| `note` | per provider | Free text for the operator. |
| `roles.<name>.provider_preference` | per role | Ordered list of provider ids to try first. |
| `roles.<name>.selections.<provider>` | per role | `model` and `effort` to send on `start_task` for that provider. |
| `roles.<name>.timeout_s` | per role | Suggested `timeout_s` (60–14400). |
| `roles.<name>.brief` | per role | The assignment brief the caller includes in the prompt. |
| `share_window` | document | `day` or `week`: which window `share_state` is judged on. |

Tokens are prompt plus completion tokens from recorded telemetry (the same definition as the
Usage page). Turns without telemetry still count as turns. Shares are computed across enabled
providers only. **Observed usage is what this host recorded from its own turns; it is not a
provider quota reading.** Budgets are operator limits, not spending controls.

Out of scope for this version: per-repository overrides, automatic provider selection, and
any change to provider availability, quota or recovery accounting.

## Document

```json
{
  "version": 1,
  "share_window": "week",
  "providers": {
    "claude": {
      "enabled": true,
      "target_share": 50,
      "budgets": {"week": {"turns": null, "tokens": 20000000, "enforce": false}},
      "allowed_modes": null,
      "note": "",
      "advertised_models": ["haiku", "sonnet", "opus[1m]"],
      "advertised_efforts": ["low", "medium", "high", "xhigh"],
      "models_without_effort": ["haiku"]
    }
  },
  "roles": {
    "planner": {
      "brief": "Produce a decision-ready plan ...",
      "provider_preference": ["claude", "grok", "agy"],
      "selections": {
        "claude": {"model": "opus[1m]", "effort": "xhigh"},
        "grok": {"model": "grok-4.6", "effort": "high"},
        "agy": {"model": "gemini-3.1-pro-high", "effort": "high"}
      },
      "timeout_s": null
    }
  }
}
```

Role keys match `^[a-z][a-z0-9_-]{0,31}$`, at most 32 roles. Identifiers match
`^[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}$`. Notes and briefs are at most 512 characters. Lists of
advertised values hold at most 64 entries. Unknown keys are rejected.

The defaults, in force until something is saved, give every loaded profile an entry seeded with
the values above and carry the six roles the Codex work-pool skill used before the policy
existed: `mechanic`, `explorer`, `implementer`, `planner`, `debugger`, `reviewer`.

### Validation

Shape errors and cross-field errors are reported as a list of `{loc, msg, code}` entries where
`loc` is the JSON path, for example `["roles", "planner", "selections", "agy"]`. Cross-field
rules: every provider id must be a loaded profile; `allowed_modes` must be served by the
profile; enabled `target_share` values may not sum above 100; Claude and Grok selections must
use advertised models and efforts, and no effort for a model in `models_without_effort`; AGY
selections must be a numeric Gemini ID with an optional `-low|-medium|-high` suffix that does
not contradict `effort`.

## Status

`status` is computed on read from the `turns` and `turn_usage` tables:

```json
{
  "share_window": "week",
  "window_start": {"day": "...Z", "week": "...Z"},
  "observed_at": "...Z",
  "under_target_order": ["grok", "agy"],
  "providers": {
    "grok": {
      "enabled": true,
      "state": "active",
      "enforced_exhaustion": false,
      "target_share": 30,
      "target_share_normalized": 0.3,
      "share_state": "under_target",
      "observed": {
        "day": {"turns": 3, "telemetry_turns": 3, "tokens": 41000, "share_turns": 0.25, "share_tokens": 0.31},
        "week": {"turns": 12, "telemetry_turns": 11, "tokens": 180000, "share_turns": 0.2, "share_tokens": 0.22}
      },
      "budgets": {
        "day": {
          "turns": {"limit": 20, "used": 3, "remaining": 17, "exhausted": false},
          "tokens": {"limit": null, "used": 41000, "remaining": null, "exhausted": false},
          "enforce": true, "exhausted": false, "window_start": "...Z"
        }
      },
      "allowed_modes": null, "note": "",
      "advertised_models": ["grok-4.6"], "advertised_efforts": ["low", "medium", "high"],
      "models_without_effort": []
    }
  },
  "note": "..."
}
```

- `state` is `paused`, `budget_exhausted` or `active`.
- `share_state` is `paused`, `untracked` (no target), `under_target`, `on_target` or
  `over_target`, judged on the turn share over `share_window` against the normalized target with
  a two-point band.
- `under_target_order` lists enabled providers with a target, most under target first.
- `enforced_exhaustion` is true when an exhausted budget on any window has `enforce`.

## Where it appears

**`capabilities()`** adds a top-level block and a per-provider block:

```json
{
  "dispatch_policy": {
    "revision": 3, "fingerprint": "…", "updated_at": "…Z", "updated_by": "web",
    "source": "store", "document_error": null, "share_window": "week",
    "roles": {"planner": {"brief": "…", "provider_preference": ["claude", "grok", "agy"],
              "selections": {"claude": {"model": "opus[1m]", "effort": "xhigh"}}, "timeout_s": null}},
    "under_target_order": ["grok", "agy"]
  },
  "providers": [{"id": "grok", "policy": {"…the provider entry of status…"}}]
}
```

`source` is `defaults` when nothing has been saved or the stored document no longer parses; in
the latter case `document_error` says why and `revision` still reports the stored revision.

**`dispatch_policy(action="get"|"status")`** is a read-only tool. `get` returns
`{policy, revision, fingerprint, updated_at, updated_by, source, document_error}`; `status`
adds `status` and `file_managed: {config_file, concurrency, native_overage, provider_recovery}`.
There is no `set` through MCP: the caller being steered does not rewrite its own steering.

**`start_task(role=…)`** records the role the policy was applied for on the task, purely for
reporting; `usage_report(group_by="role")` rolls token use up by provider and role.

**`POLICY_BUDGET_EXHAUSTED`** (retryable) is returned by `start_task` when the named provider has
an exhausted, enforced budget. `details` carries `provider`, `window`, `kind` (`turns` or
`tokens`), `limit`, `used`, `window_start` and `policy_revision`.

## Editing

**Dashboard.** `taskspindle web`, page **Policy**. The page holds a draft while you edit (polling
pauses), validates locally, and saves the whole document with the revision it was loaded from.
A save against a moved revision is refused with `409 POLICY_REVISION_CONFLICT`; reload and
reapply. Reset restores the defaults as a new revision. Every save is kept in history.

**CLI.**

```sh
taskspindle policy show [--status] [--json]
taskspindle policy export > policy.json
taskspindle policy import policy.json [--if-revision N]
taskspindle policy set providers.claude.target_share 50
taskspindle policy set roles.planner.timeout_s 3600
taskspindle policy reset [--if-revision N]
```

`set` takes a dotted path and a JSON value. Invalid documents exit 1 and print the located
errors; a revision conflict exits 3.

### HTTP API

Policy routes are the only mutating routes of the dashboard. Writes require a loopback peer and
`Host`, a same-origin `Origin`, the per-process `X-TaskSpindle-CSRF` token served by the GET, a
JSON object body of at most 64 KiB, and a state database already at schema 11 (the dashboard
never migrates; `503 POLICY_UNAVAILABLE` otherwise). The dashboard's write connection is limited
by a SQLite authorizer to the two policy tables; every other table stays read-only.

| Route | Method | Returns |
| --- | --- | --- |
| `/api/policy` | GET | `{policy, revision, fingerprint, updated_at, updated_by, source, document_error, status, defaults, profiles, file_managed, writable, csrf_token}` |
| `/api/policy` | PUT `{if_revision, policy}` | the GET payload after saving; `400 POLICY_INVALID` with `details.errors`, `409 POLICY_REVISION_CONFLICT` with `current_revision` |
| `/api/policy/reset` | POST `{if_revision}` | the GET payload after saving the defaults |
| `/api/policy/history?limit=50` | GET | `{history: [{revision, updated_at, updated_by, fingerprint, reason}]}` |
| `/api/policy/history/{revision}` | GET | `{revision, policy, updated_at, updated_by, reason}`; `404 POLICY_REVISION_NOT_FOUND` |

`profiles` lists `{id, family, first_class, auth, modes}` for every loaded profile so the page can
offer only real providers. `file_managed` mirrors the `config.toml` tables read on this request.

## How Codex uses it

Before every `start_task`, the work-pool skill refreshes `capabilities()` and, when
`dispatch_policy` is present, walks `roles[role].provider_preference`, drops providers that are
`paused`, have `enforced_exhaustion`, exclude the task mode, or fail the existing availability,
quota, recovery, grant, capacity and reviewer-independence rules, prefers an `under_target`
provider over an `over_target` one, and sends the role's `model`, `effort`, `timeout_s` and
`role`. An advisory `budget_exhausted` provider is used only when nothing else is eligible, and
the override is recorded. None of this adds a fallback: an explicit provider from the user is
kept, and a provider that refuses is reported, not replaced.
