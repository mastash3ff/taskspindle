# Dispatch policy

TaskSpindle never chooses a provider. Codex names `provider` on every `start_task`, nothing is
retried on another provider, and a refusal is reported rather than worked around. The dispatch
policy does not change that. It is operator data that tells the caller how the operator would
like work spread across `claude`, `grok` and `agy`, which model and effort each role should
use on each provider, how many turns each provider may run at once, and where locally observed
usage stands against targets and budgets.

Most of it is advisory. Three things have a server-side effect, and each is mechanical:

- an **enforced budget** refuses admission of a new task on that provider until the window rolls;
- **`max_concurrent`** is how many slots a provider may hold, read at every dispatch;
- a task that names a **`role`** and leaves `model` or `effort` out has them **filled** from that
  role's selection, stepped along its ladder. A model or effort the caller sends is never
  changed.

The policy lives in the state database (`dispatch_policy`), not in `config.toml`. It is read
fresh on every `capabilities()` call, every `dispatch_policy` call, every `start_task`
admission and every dispatch, so an edit made in the dashboard or with `taskspindle policy`
takes effect without restarting the MCP server. `[concurrency]` and `[capacity]` stay in
`config.toml`: the first is the fallback wherever the policy sets no `max_concurrent`, the
second is the ceiling over whatever it sets. The dashboard shows both and writes neither. See
[concurrency.md](concurrency.md).

### Guarded model promotion

Automation can call `taskspindle policy promote stage REQUEST.json`, then `apply REQUEST.json`,
and, if needed, `rollback REQUEST.json`. Each command accepts one JSON request file and prints
a JSON result. The commands make no provider or Docker calls. The caller obtains the image and
binary identities independently, closes admission before a write, and supplies that assertion
with a nonempty `admission_evidence` string. The string is an operator record, not a check that
the dispatcher is actually closed.

The stage request has exactly these fields:

```json
{
  "record_path": "/path/to/staged-promotion.json",
  "source_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "image_identity": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "binary_digests": {"grok": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"},
  "expected_revision": 0,
  "expected_fingerprint": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "candidates": {"planner": {"grok": {"model": "grok-4.7", "effort": "medium"}}}
}
```

`record_path` must not exist. The staged file is created read-only and contains the prior
policy for rollback. Candidate role and provider keys must already exist; candidate models
and efforts must be advertised in the policy and pass the policy's selection rules. Metered
profiles cannot be promoted.

The apply request has `record_path`, `admission_closed: true`, a nonempty
`admission_evidence`, `runtime_image_identity`, and `runtime_binary_digests`. The runtime values
must match the staged values exactly. The rollback request has `record_path`,
`admission_closed: true`, and a nonempty `admission_evidence`. Apply and rollback both use an
expected policy revision compare-and-swap. A revision conflict exits with code 3; other
validation failures exit with code 1. Rollback is available only while the staged candidate
is still the current policy.

## Turning usage up

The knobs that raise how much of a subscription pool is used, from bluntest to finest:

1. **Intensity preset** (`conserve`, `balanced`, `max`) per provider: one click or
   `taskspindle policy preset <provider> <level>`. It fills that provider's `max_concurrent`
   (1, 4, 8) and, for each shipped role with a selection there, the selection and its ladder.
   A preset is a macro, not stored state, so every field stays editable and the page shows
   `custom` once you change one. `balanced` is the shipped role table; `conserve` writes nothing
   above it; `max` starts at the top with one step down.
2. **`max_concurrent`** per provider and **`max_concurrent_total`** for the pool: more turns at
   once. Only what you have qualified on this host; see [concurrency.md](concurrency.md).
3. **Selections** per role and provider: a larger model or more effort is more tokens per turn.
4. **Ladders and escalation**: let a provider that is under its target share step its roles up,
   and one that is over step down, within steps you chose.
5. **`fanout`** per role: a consult or review the coordinator would start anyway may go to that
   many independent provider families at once, as genuine second opinions.

None of this invents work. Slots left free stay free; see "Free slots" in
[concurrency.md](concurrency.md).

## What the policy can express

| Knob | Where | Effect |
| --- | --- | --- |
| `enabled` | per provider | `false` reports the provider as `paused`. Advisory: Codex skips it; the server still admits an explicit task. |
| `target_share` | per provider, 0–100 | Desired share of turns. Compared with the observed share over `share_window`; produces `share_state` and `under_target_order`. Enabled shares may sum to less than 100; they may not exceed it. |
| `budgets.<day|week>` | per provider | `turns` and/or `tokens` ceilings over a rolling 24 h / 7 d window of this host's own recorded turns. Advisory unless `enforce`. |
| `budgets.<window>.enforce` | per budget | An exhausted enforced budget refuses **new** `start_task` admission on that provider with `POLICY_BUDGET_EXHAUSTED` until the window rolls. Already queued or running tasks are untouched. |
| `allowed_modes` | per provider | Advisory hint narrowing `consult`/`review`/`implement`; must be a subset of what the profile serves. |
| `advertised_models`, `advertised_efforts`, `models_without_effort` | per provider | The value lists role selections are validated against for Claude and Grok, and the choices the dashboard offers. AGY selections must be a numeric Gemini ID or an identifier listed on `providers.agy.advertised_models` (Claude/GPT third-party IDs). |
| `max_concurrent` | per provider, 1–16 | **Enforced.** Slots the provider may hold at once, capped by `[capacity] per_provider_max`. Unset falls back to `[concurrency]` in `config.toml`. Applies to the next dispatch; lowering it never stops a running turn. |
| `note` | per provider | Free text for the operator. |
| `roles.<name>.provider_preference` | per role | Ordered list of provider ids to try first. |
| `roles.<name>.selections.<provider>` | per role | `model` and `effort` for that provider. **Filled by the server** when `start_task` names the role and leaves one or both out; step 0 of the ladder. |
| `roles.<name>.ladders.<provider>` | per role | `below` and `above`: up to three `{model, effort}` steps either side of the selection, nearest first. Escalation moves along them. Built-in subscription profiles only. |
| `roles.<name>.fanout` | per role, 1–3 | Advisory: how many independent provider families one consult or review may go to. See "Fan-out". |
| `roles.<name>.timeout_s` | per role | Suggested `timeout_s` (60–14400). |
| `roles.<name>.brief` | per role | The assignment brief the caller includes in the prompt. |
| `share_window` | document | `day` or `week`: which window `share_state` is judged on. |
| `max_concurrent_total` | document, 1–48 | **Enforced.** Slots the whole pool may hold at once, across providers; the lower of this and `[capacity] total_max` applies. |
| `escalation` | document | When a ladder steps; off by default. See "Escalation". |

Tokens are prompt plus completion tokens from recorded telemetry (the same definition as the
Usage page). Turns without telemetry still count as turns. Shares are computed across enabled
providers only. **Observed usage is what this host recorded from its own turns; it is not a
provider quota reading.** Budgets are operator limits, not spending controls.

Out of scope: per-repository overrides, automatic provider selection, overriding a model or
effort the caller sent, and any change to provider availability, quota or recovery accounting.

### Server-side fill

`start_task` fills only what the caller left out, from the role's current ladder step on the
named provider:

| Caller sent | Result |
| --- | --- |
| model and effort | kept as sent (`source: caller`) |
| neither | the step's model and effort as a pair (`source: policy`) |
| model only | the step's effort, but only when the step is for that same model and the model takes an effort (`source: mixed`); otherwise nothing is added |
| effort only | the step's model, unless that model takes no effort or the pair is not valid for the provider |

The fill never refuses a task. It is skipped, and the reason recorded, when no role was named,
the role is not in the policy or has no selection for the provider, or the profile is not a
built-in subscription one: a per-task override is only supported there, and a metered profile
is never stepped by a share target. The task then runs on the profile's own defaults, as it
did before. `start_task` returns the outcome as `selection: {model, effort, source, role,
ladder_level, policy_revision, reason}`, and `capabilities.dispatch_policy.server_fill` is
`true` on a server that does this.

### Escalation

With `escalation.enabled`, `status` gives each provider one ladder level, and every role on
that provider uses the step at that level, stopping at the end of its own ladder. The level
comes from the turn-share deficit over `share_window`, in percentage points:

- at or above `step_up_points[0]` (default 5) under target the level is +1, at or above the
  next value (12) it is +2; the same distances over target give −1 and −2;
- a level is only left once the deficit is `release_points` (3) back inside its threshold,
  measured from the level the provider's last filled task used, so a share sitting on a
  threshold cannot flip the step from one task to the next;
- below `min_turns` (10) turns in the window the share is noise and the level is 0.

Brakes only ever lower the level: a provider that is throttled or logged out is held at 0; a
provider-reported usage window at `window_hold_percent` (80) holds at 0 and at
`window_down_percent` (90) steps down to −1; a budget at `budget_hold_ratio` (0.8) or
`budget_down_ratio` (0.95) does the same. Only Claude reports a usage window, so for Grok and
AGY the brakes are a refusal and whatever budget you set. `status` reports
`escalation: {level, previous_level, reason, signal}` per provider, with a plain reason such as
`step +1: 8 points under target`.

This is deliberately open-loop on turns: a larger model does not change a turn share, so
routing and fan-out are what close a share gap, and the brakes are what stop the spend.

### Fan-out

`roles.<name>.fanout` tells the coordinator it may ask the same question of that many provider
families. It passes one `fanout_group` (a short id it makes up) on each `start_task` of the
set. The server checks two things: `fanout_group` is refused on `implement`, because a writer
is never fanned out; and a group takes one live member per provider family, so a second
`claude`-family task in a group is refused with `FANOUT_NOT_INDEPENDENT` while the first is
still able to give an opinion. `dispatch_policy(action="status")` and `usage_report` report
how wide the groups actually were.

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
      "models_without_effort": ["haiku"],
      "max_concurrent": 4
    }
  },
  "roles": {
    "planner": {
      "brief": "Produce a decision-ready plan ...",
      "provider_preference": ["claude", "grok", "agy"],
      "selections": {
        "claude": {"model": "opus[1m]", "effort": "xhigh"},
        "grok": {"model": "grok-4.7", "effort": "high"},
        "agy": {"model": "gemini-3.1-pro-high", "effort": "high"}
      },
      "timeout_s": null,
      "ladders": {"claude": {"below": [{"model": "sonnet", "effort": "high"}], "above": []}},
      "fanout": 1
    }
  },
  "max_concurrent_total": 12,
  "escalation": {"enabled": false, "step_up_points": [5, 12], "release_points": 3, "min_turns": 10,
                 "window_hold_percent": 80, "window_down_percent": 90,
                 "budget_hold_ratio": 0.8, "budget_down_ratio": 0.95}
}
```

`max_concurrent`, `ladders`, `fanout`, `max_concurrent_total` and `escalation` were added after
the first release. The stored document omits each of them while it is at its default, so a
policy that uses none of them is byte-for-byte what the first release wrote: its fingerprint
does not move, and an older image still parses it. Setting any of them is what makes a document
newer; an older image then reports `document_error` and runs on the defaults until it is
upgraded, which is why `taskspindle policy export` belongs before a rollback. Readers always get
every field: `get`, `show --json` and the dashboard fill the defaults back in.

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
not contradict `effort`, or an exact third-party ID listed on `providers.agy.advertised_models`.
Ladder steps follow the same rules as selections, must name a model, need a selection for the
same provider, and are refused on a profile that is not a built-in subscription one
(`ladder_metered`).
A bare AGY start still defaults to Gemini; third-party IDs are only used when explicitly
selected.

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
      "advertised_models": ["grok-4.7"], "advertised_efforts": ["low", "medium", "high"],
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
adds `status`, `file_managed: {config_file, concurrency, capacity}`, `limits` (the slot limits
this server dispatches by, each with its `source` and `ceiling`), `utilization` (slot use and
queue wait for the rolling day and week) and `fanout` (asked for and realized, per role).
There is no `set` through MCP: the caller being steered does not rewrite its own steering.

**`start_task(role=…)`** records the role on the task and fills `model` and `effort` from it
as described under "Server-side fill"; `usage_report(group_by="role")` rolls token use up by
provider and role.

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
taskspindle policy set providers.claude.target_share 50 [--if-revision N]
taskspindle policy set providers.grok.max_concurrent 6
taskspindle policy set roles.explorer.ladders.claude.above '[{"model": "opus[1m]", "effort": "high"}]' --create-parents
taskspindle policy preset agy max [--dry-run] [--if-revision N]
taskspindle policy reset [--if-revision N]
```

`set` takes a dotted path and a JSON value; `--create-parents` makes the tables along a path
that does not exist yet. `preset` prints each field it changes, and with `--dry-run` writes
nothing. `show --status` adds each provider's slots in force and their source, its preset, its
ladder level, and the last day's slot use and queue. Invalid documents exit 1 and print the
located errors; a revision conflict exits 3.

### HTTP API

Policy routes are the only mutating routes of the dashboard. Writes require a loopback peer and
`Host` (or, with `[web] allow_remote_policy`, an allowed host: `[capacity]` is then what bounds
the concurrency a remote edit can ask for), a same-origin `Origin`, the per-process `X-TaskSpindle-CSRF` token served by the GET, a
JSON object body of at most 64 KiB, and a state database already at schema 11 (the dashboard
never migrates; `503 POLICY_UNAVAILABLE` otherwise). The dashboard's write connection is limited
by a SQLite authorizer to the two policy tables; every other table stays read-only.

| Route | Method | Returns |
| --- | --- | --- |
| `/api/policy` | GET | `{policy, revision, fingerprint, updated_at, updated_by, source, document_error, status, defaults, profiles, file_managed, limits, utilization, fanout, presets, preset_matches, bounds, writable, csrf_token}` |
| `/api/policy` | PUT `{if_revision, policy}` | the GET payload after saving; `400 POLICY_INVALID` with `details.errors`, `409 POLICY_REVISION_CONFLICT` with `current_revision` |
| `/api/policy/reset` | POST `{if_revision}` | the GET payload after saving the defaults |
| `/api/policy/history?limit=50` | GET | `{history: [{revision, updated_at, updated_by, fingerprint, reason}]}` |
| `/api/policy/history/{revision}` | GET | `{revision, policy, updated_at, updated_by, reason}`; `404 POLICY_REVISION_NOT_FOUND` |

`profiles` lists `{id, family, first_class, auth, modes}` for every loaded profile so the page can
offer only real providers. `file_managed` mirrors the `config.toml` tables read on this request.
`presets` is, per provider and level, the list of `{path, value}` patches that level writes, so
the page applies exactly what `policy preset` would; `preset_matches` names the level each
provider currently equals, or `custom`.

## How Codex uses it

Before every `start_task`, the work-pool skill refreshes `capabilities()` and, when
`dispatch_policy` is present, walks `roles[role].provider_preference`, drops providers that are
`paused`, have `enforced_exhaustion`, exclude the task mode, or fail the existing availability,
quota, recovery, grant, capacity and reviewer-independence rules, prefers an `under_target`
provider over an `over_target` one, and sends `role` and the role's `timeout_s`. It leaves
`model` and `effort` out unless the user named one, so the server fills them from the role's
current step; against a server without `server_fill` it sends the role's selection itself. For
a role with `fanout` above 1 it may start the same consult or review on that many provider
families under one `fanout_group`. An advisory `budget_exhausted` provider is used only when nothing else is eligible, and
the override is recorded. None of this adds a fallback: an explicit provider from the user is
kept, and a provider that refuses is reported, not replaced.
