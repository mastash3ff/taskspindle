import test from "node:test";
import assert from "node:assert/strict";

function attrToDatasetKey(attr) {
  return attr.replace(/^data-/, "").replace(/-([a-z0-9])/g, (_, c) => c.toUpperCase());
}

function singleMatches(node, part) {
  const trimmed = part.trim();
  const m = /^([a-zA-Z0-9]*)\[([a-zA-Z0-9-]+)\]$/.exec(trimmed);
  if (!m) return false;
  const [, tag, attr] = m;
  if (tag && node.tag !== tag) return false;
  if (attr === "id") return Boolean(node.id);
  const key = attrToDatasetKey(attr);
  return node.dataset ? Object.prototype.hasOwnProperty.call(node.dataset, key) : false;
}

class FakeNode {
  constructor(tag = "", text = "") {
    this.tag = tag;
    this.textContent = text;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.className = "";
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  get childNodes() { return this.children; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(kind, handler) { this.listeners[kind] = handler; }
  contains(node) {
    if (!node) return false;
    const walk = (n) => (n.children || []).some((c) => c === node || walk(c));
    return walk(this);
  }
  querySelectorAll(selector) {
    const parts = selector.split(",").map((s) => s.trim());
    const results = [];
    const walk = (n) => {
      for (const child of n.children || []) {
        if (parts.some((part) => singleMatches(child, part))) results.push(child);
        walk(child);
      }
    };
    walk(this);
    return results;
  }
}

class FakeInput {}
class FakeTextArea {}
globalThis.HTMLInputElement = FakeInput;
globalThis.HTMLTextAreaElement = FakeTextArea;
globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (text) => new FakeNode("#text", text),
  activeElement: null,
};
globalThis.window = { scrollX: 0, scrollY: 0, scrollTo() {}, getSelection() { return null; } };
globalThis.location = { hash: "#/policy" };
globalThis.requestAnimationFrame = () => {};

const policyModule = await import("../../src/taskspindle/web/static/views/policy.js");
const { renderPolicy, __test__ } = policyModule;
const { applyPath, applyPatches, getPath, presetOf, isDirty, localValidate, orderByUnderTarget, errorsByLoc, normalizeTargets, resetDraft } = __test__;
const aiPolicyModule = await import("../../src/taskspindle/web/static/ai-policy.js");
const { resetAiPolicyUi, isAiPolicyApplying } = aiPolicyModule;

const find = (node, predicate) => {
  const result = predicate(node) ? [node] : [];
  return result.concat(...(node.children || []).map((child) => find(child, predicate)));
};
const byFocusKey = (root, key) => find(root, (node) => node.dataset?.focusKey === key)[0];
const textOf = (node) => (node?.textContent || "") + (node?.children || []).map(textOf).join("");

// -- pure helpers ---------------------------------------------------------------------

test("applyPath sets a nested value without mutating the original draft", () => {
  const base = { providers: { claude: { target_share: 10, note: "x" } } };
  const next = applyPath(base, ["providers", "claude", "target_share"], 40);
  assert.equal(next.providers.claude.target_share, 40);
  assert.equal(base.providers.claude.target_share, 10);
  assert.equal(next.providers.claude.note, "x");
  assert.notEqual(next, base);
  assert.notEqual(next.providers, base.providers);
});

test("applyPath creates intermediate objects that do not exist yet", () => {
  const next = applyPath({}, ["roles", "planner", "timeout_s"], 600);
  assert.equal(next.roles.planner.timeout_s, 600);
});

test("isDirty compares canonically, ignoring key order", () => {
  const base = { a: 1, b: { c: 2, d: 3 } };
  const reordered = { b: { d: 3, c: 2 }, a: 1 };
  assert.equal(isDirty(base, reordered), false);
  assert.equal(isDirty(base, { a: 1, b: { c: 2, d: 4 } }), true);
});

test("localValidate reports enabled target shares over 100", () => {
  const draft = { providers: { claude: { enabled: true, target_share: 70 }, grok: { enabled: true, target_share: 40 } }, roles: {} };
  const profiles = [{ id: "claude", family: "claude" }, { id: "grok", family: "grok" }];
  const errors = localValidate(draft, profiles);
  assert.ok(errors.some((err) => err.code === "SHARE_OVER_100"));
});

test("localValidate ignores disabled providers when summing shares", () => {
  const draft = { providers: { claude: { enabled: false, target_share: 70 }, grok: { enabled: true, target_share: 40 } }, roles: {} };
  const profiles = [{ id: "claude", family: "claude" }, { id: "grok", family: "grok" }];
  assert.deepEqual(localValidate(draft, profiles).filter((err) => err.code === "SHARE_OVER_100"), []);
});

test("localValidate rejects invalid role keys and out-of-range timeouts", () => {
  const draft = { providers: {}, roles: { Bad_Key: { timeout_s: 30 }, ok_role: { timeout_s: 20000 } } };
  const errors = localValidate(draft, []);
  assert.ok(errors.some((err) => err.code === "ROLE_KEY_INVALID" && err.loc.join(".") === "roles.Bad_Key"));
  assert.ok(errors.some((err) => err.code === "TIMEOUT_OUT_OF_RANGE" && err.loc.join(".") === "roles.Bad_Key.timeout_s"));
  assert.ok(errors.some((err) => err.code === "TIMEOUT_OUT_OF_RANGE" && err.loc.join(".") === "roles.ok_role.timeout_s"));
});

test("localValidate rejects identifiers outside the allowed grammar", () => {
  const draft = { providers: { claude: { enabled: true, target_share: 0, advertised_models: ["ok-1", "bad model!"] } }, roles: {} };
  const errors = localValidate(draft, [{ id: "claude", family: "claude" }]);
  assert.ok(errors.some((err) => err.code === "IDENTIFIER_INVALID" && err.loc.join(".") === "providers.claude.advertised_models"));
});

test("localValidate clears effort for models without effort", () => {
  const draft = {
    providers: { claude: { enabled: true, target_share: 0, models_without_effort: ["haiku"] } },
    roles: { planner: { timeout_s: null, selections: { claude: { model: "haiku", effort: "low" } } } },
  };
  const errors = localValidate(draft, [{ id: "claude", family: "claude" }]);
  assert.ok(errors.some((err) => err.code === "EFFORT_NOT_ALLOWED" && err.loc.join(".") === "roles.planner.selections.claude.effort"));
});

test("orderByUnderTarget puts the server order first, then ranks the remainder", () => {
  const status = {
    under_target_order: ["grok"],
    providers: {
      grok: { share_state: "under_target" },
      claude: { share_state: "on_target" },
      agy: { share_state: "over_target" },
    },
  };
  assert.deepEqual(orderByUnderTarget(status), ["grok", "claude", "agy"]);
});

test("errorsByLoc groups messages by joined location", () => {
  const map = errorsByLoc([
    { loc: ["providers", "claude", "target_share"], msg: "too high" },
    { loc: ["providers", "claude", "target_share"], msg: "also invalid" },
    { loc: ["roles", "planner"], msg: "bad key" },
  ]);
  assert.deepEqual(map.get("providers.claude.target_share"), ["too high", "also invalid"]);
  assert.deepEqual(map.get("roles.planner"), ["bad key"]);
});

test("normalizeTargets converts percentages to clamped fractions", () => {
  const result = normalizeTargets({ claude: { target_share: 150 }, grok: { target_share: 30 }, agy: {} });
  assert.equal(result.claude, 1);
  assert.equal(result.grok, 0.3);
  assert.equal(result.agy, 0);
});

// -- utilization tuning helpers -----------------------------------------------------------

const PROFILES = [
  { id: "claude", family: "claude", first_class: true, auth: "oauth" },
  { id: "agy", family: "agy", first_class: true, auth: "oauth" },
  { id: "metered", family: "claude", first_class: false, auth: "api_key" },
];

test("applyPath walks into a list by index without turning it into a map", () => {
  const draft = { ladders: { above: [{ model: "a", effort: null }, { model: "b", effort: null }] } };
  const next = applyPath(draft, ["ladders", "above", 1, "effort"], "high");
  assert.deepEqual(next.ladders.above, [{ model: "a", effort: null }, { model: "b", effort: "high" }]);
  assert.ok(Array.isArray(next.ladders.above));
  assert.equal(draft.ladders.above[1].effort, null);
});

test("presetOf names the level the draft already equals, custom otherwise, null without presets", () => {
  const presets = {
    conserve: [{ path: ["providers", "claude", "max_concurrent"], value: 1 }],
    max: [{ path: ["providers", "claude", "max_concurrent"], value: 8 }, { path: ["roles", "r", "ladders", "claude"], value: { below: [], above: [] } }],
  };
  const draft = { providers: { claude: { max_concurrent: 4 } }, roles: { r: {} } };
  assert.equal(presetOf(draft, presets), "custom");
  const applied = applyPatches(draft, presets.max);
  assert.equal(getPath(applied, ["providers", "claude", "max_concurrent"]), 8);
  assert.equal(presetOf(applied, presets), "max");
  assert.equal(draft.providers.claude.max_concurrent, 4, "applyPatches never mutates the draft it was given");
  assert.equal(presetOf(draft, {}), null);
  assert.equal(presetOf(draft, undefined), null);
});

test("localValidate mirrors the server's bounds on slots, fan-out and escalation", () => {
  const codes = (draft) => localValidate(draft, PROFILES).map((err) => err.code);
  assert.deepEqual(codes({ providers: { claude: { max_concurrent: 4 } }, max_concurrent_total: 12, roles: { r: { fanout: 3 } } }), []);
  assert.deepEqual(codes({ providers: { claude: { max_concurrent: 17 } } }), ["CONCURRENCY_OUT_OF_RANGE"]);
  assert.deepEqual(codes({ providers: { claude: { max_concurrent: 1.5 } } }), ["CONCURRENCY_OUT_OF_RANGE"]);
  assert.deepEqual(codes({ max_concurrent_total: 49 }), ["CONCURRENCY_OUT_OF_RANGE"]);
  assert.deepEqual(codes({ roles: { r: { fanout: 4 } } }), ["FANOUT_OUT_OF_RANGE"]);
  const rules = { step_up_points: [5, 12], release_points: 3, window_hold_percent: 80, window_down_percent: 90, budget_hold_ratio: 0.8, budget_down_ratio: 0.95 };
  assert.deepEqual(codes({ escalation: rules }), []);
  assert.deepEqual(codes({ escalation: { ...rules, step_up_points: [12, 5] } }), ["ESCALATION_STEPS"]);
  assert.deepEqual(codes({ escalation: { ...rules, release_points: 5 } }), ["ESCALATION_RELEASE"]);
  assert.deepEqual(codes({ escalation: { ...rules, window_down_percent: 70, budget_down_ratio: 0.5 } }), ["ESCALATION_WINDOW", "ESCALATION_BUDGET"]);
});

test("localValidate checks ladder steps like selections and refuses them on a metered profile", () => {
  const providers = { claude: { models_without_effort: ["haiku"] }, agy: { advertised_models: ["claude-sonnet-4-6"] }, metered: {} };
  const role = (extra) => ({ providers, roles: { r: { selections: { claude: { model: "sonnet" }, agy: { model: "claude-sonnet-4-6" }, metered: { model: "x" } }, ...extra } } });
  const found = (draft) => localValidate(draft, PROFILES).map((err) => `${err.loc.join(".")}:${err.code}`);

  assert.deepEqual(found(role({ ladders: { claude: { below: [{ model: "haiku", effort: null }], above: [{ model: "opus[1m]", effort: "high" }] } } })), []);
  // An AGY model the operator advertised is accepted, as the server accepts it.
  assert.deepEqual(found(role({})), []);
  assert.deepEqual(found(role({ ladders: { claude: { above: [{ model: null, effort: "high" }, { model: "haiku", effort: "low" }] } } })), [
    "roles.r.ladders.claude.above.0.model:LADDER_STEP_EMPTY", "roles.r.ladders.claude.above.1.effort:EFFORT_NOT_ALLOWED",
  ]);
  assert.deepEqual(found(role({ ladders: { metered: { above: [{ model: "y" }] } } })), ["roles.r.ladders.metered:LADDER_METERED"]);
  assert.deepEqual(found({ providers, roles: { r: { selections: {}, ladders: { claude: { above: [{ model: "sonnet" }] } } } } }), ["roles.r.ladders.claude:LADDER_WITHOUT_SELECTION"]);
  assert.deepEqual(found(role({ ladders: { claude: { above: Array(4).fill({ model: "sonnet" }) } } })), ["roles.r.ladders.claude.above:LADDER_TOO_LONG"]);
  // An empty ladder is the default and says nothing.
  assert.deepEqual(found(role({ ladders: { metered: { below: [], above: [] } } })), []);
});

// -- rendering --------------------------------------------------------------------------

function fixture(overrides = {}) {
  return {
    policy: {
      version: 1, share_window: "day",
      providers: { claude: { enabled: true, target_share: 50, budgets: { day: { turns: null, tokens: null, enforce: false }, week: { turns: null, tokens: null, enforce: false } }, allowed_modes: null, note: "", advertised_models: ["haiku", "sonnet"], advertised_efforts: ["low", "medium"], models_without_effort: ["haiku"] } },
      roles: { planner: { brief: "Plan it", provider_preference: ["claude"], selections: { claude: { model: "sonnet", effort: "medium" } }, timeout_s: 600 } },
    },
    revision: 5, fingerprint: "fp1", updated_at: "2030-01-01T00:00:00Z", updated_by: "web", source: "store", document_error: null,
    status: {
      share_window: "day", window_start: {}, observed_at: "2030-01-01T00:00:00Z", under_target_order: ["claude"],
      providers: { claude: { enabled: true, state: "active", enforced_exhaustion: false, target_share: 50, target_share_normalized: 0.5, share_state: "on_target", observed: { day: { turns: 4, telemetry_turns: 4, tokens: 400, share_turns: 0.5, share_tokens: 0.5 }, week: { turns: 4, telemetry_turns: 4, tokens: 400, share_turns: 0.5, share_tokens: 0.5 } }, budgets: { day: { turns: { limit: null, used: 0, remaining: null, exhausted: false }, tokens: { limit: null, used: 0, remaining: null, exhausted: false }, enforce: false, exhausted: false, window_start: "2030-01-01T00:00:00Z" }, week: { turns: { limit: null, used: 0, remaining: null, exhausted: false }, tokens: { limit: null, used: 0, remaining: null, exhausted: false }, enforce: false, exhausted: false, window_start: "2030-01-01T00:00:00Z" } }, allowed_modes: null, note: "", advertised_models: ["haiku", "sonnet"], advertised_efforts: ["low", "medium"], models_without_effort: ["haiku"] } },
    },
    defaults: {}, file_managed: { config_file: "/x/config.toml", concurrency: {} },
    profiles: [{ id: "claude", family: "claude", first_class: true, auth: "oauth", modes: ["consult", "review", "implement"] }],
    writable: true, csrf_token: "tok",
    ...overrides,
  };
}

test("rendering a policy fixture then editing a field marks the draft dirty and holds polling", async () => {
  resetDraft();
  resetAiPolicyUi();
  globalThis.fetch = async (path) => {
    if (String(path).startsWith("/api/policy")) return new Response(JSON.stringify(fixture()), { status: 200 });
    return new Response("{}", { status: 404 });
  };
  const node = await renderPolicy({ query: new URLSearchParams() }, { toast: () => {}, refresh: () => {} });
  assert.equal(node.dataset.holdPoll, undefined);

  const shareNumber = byFocusKey(node, "policy-claude-share-number");
  assert.ok(shareNumber, "expected the target share number input to be present");
  shareNumber.value = "75";
  shareNumber.listeners.change();

  assert.equal(node.dataset.holdPoll, "1");
  const updatedNumber = byFocusKey(node, "policy-claude-share-number");
  assert.equal(updatedNumber.value, "75");
});

function aiFixture() {
  return {
    hosts: [
      { host: "windows", mode: "ensemble", status: "configured", revision: "r1", checks: [] },
      { host: "wsl", mode: "ensemble", status: "configured", revision: "r1", checks: [] },
    ],
    applies_to: "new_sessions",
    csrf_token: "tok",
  };
}

test("Codex AI mode controls stay independent of the dispatch policy draft", async () => {
  resetDraft();
  resetAiPolicyUi();
  const puts = [];
  globalThis.fetch = async (path, init = {}) => {
    if (String(path) === "/api/ai-policy" && init.method === "PUT") {
      puts.push(JSON.parse(init.body));
      const applied = aiFixture();
      applied.hosts = applied.hosts.map((row) => ({ ...row, mode: "native", revision: "r2" }));
      applied.results = [{ host: "windows", ok: true }, { host: "wsl", ok: true }];
      return new Response(JSON.stringify(applied), { status: 200 });
    }
    if (String(path).startsWith("/api/ai-policy")) return new Response(JSON.stringify(aiFixture()), { status: 200 });
    if (String(path).startsWith("/api/policy")) return new Response(JSON.stringify(fixture()), { status: 200 });
    return new Response("{}", { status: 404 });
  };
  const node = await renderPolicy({ query: new URLSearchParams() }, { toast: () => {}, refresh: () => { throw new Error("AI apply must not refresh the dispatch draft"); } });
  assert.equal(node.dataset.holdPoll, undefined);

  const native = byFocusKey(node, "ai-policy-mode-native");
  assert.ok(native, "expected the Native radio");
  native.checked = true;
  native.listeners.change();
  assert.equal(node.dataset.holdPoll, undefined);

  const shareNumber = byFocusKey(node, "policy-claude-share-number");
  shareNumber.value = "75";
  shareNumber.listeners.change();
  assert.equal(node.dataset.holdPoll, "1");

  const apply = byFocusKey(node, "ai-policy-apply");
  assert.ok(apply);
  await apply.listeners.click();
  assert.equal(node.dataset.holdPoll, "1");
  assert.equal(byFocusKey(node, "policy-claude-share-number").value, "75");
  assert.deepEqual(puts[0].mode, "native");
  assert.deepEqual(puts[0].hosts, ["windows", "wsl"]);
});

test("AI apply holds polling and ignores a stale GET that finishes after apply", async () => {
  resetDraft();
  resetAiPolicyUi();
  let releasePut;
  let releaseGet;
  const putGate = new Promise((resolve) => { releasePut = resolve; });
  const getGate = new Promise((resolve) => { releaseGet = resolve; });
  let getCount = 0;
  globalThis.fetch = async (path, init = {}) => {
    if (String(path) === "/api/ai-policy" && init.method === "PUT") {
      await putGate;
      return new Response(JSON.stringify({
        ...aiFixture(),
        hosts: [
          { host: "windows", mode: "native", status: "configured", revision: "applied", checks: [{ name: "post-apply", ok: true, detail: "ok" }] },
          { host: "wsl", mode: "native", status: "configured", revision: "applied", checks: [{ name: "post-apply", ok: true, detail: "ok" }] },
        ],
        results: [{ host: "windows", ok: true }, { host: "wsl", ok: true }],
      }), { status: 200 });
    }
    if (String(path).startsWith("/api/ai-policy")) {
      getCount += 1;
      if (getCount > 1) await getGate;
      const revision = getCount === 1 ? "r1" : "stale-poll";
      return new Response(JSON.stringify({
        ...aiFixture(),
        hosts: [
          { host: "windows", mode: "ensemble", status: "configured", revision, checks: [] },
          { host: "wsl", mode: "ensemble", status: "configured", revision, checks: [] },
        ],
      }), { status: 200 });
    }
    if (String(path).startsWith("/api/policy")) return new Response(JSON.stringify(fixture()), { status: 200 });
    return new Response("{}", { status: 404 });
  };
  const first = await renderPolicy({ query: new URLSearchParams() }, { toast: () => {}, refresh: () => {} });
  const stalePoll = renderPolicy({ query: new URLSearchParams() }, { toast: () => {}, refresh: () => {} });
  const applyDone = byFocusKey(first, "ai-policy-apply").listeners.click();
  assert.equal(isAiPolicyApplying(), true);
  assert.equal(first.dataset.holdPoll, "1");
  releaseGet();
  const pollNode = await stalePoll;
  assert.doesNotMatch(textOf(pollNode), /stale-poll/);
  assert.doesNotMatch(textOf(pollNode), /post-apply/);
  releasePut();
  await applyDone;
  assert.match(textOf(pollNode), /post-apply/);
  assert.doesNotMatch(textOf(pollNode), /stale-poll/);
});

function tuningFixture() {
  const base = fixture();
  base.policy.providers.claude.max_concurrent = null;
  base.policy.max_concurrent_total = null;
  base.policy.escalation = { enabled: false, step_up_points: [5, 12], release_points: 3, min_turns: 10, window_hold_percent: 80, window_down_percent: 90, budget_hold_ratio: 0.8, budget_down_ratio: 0.95 };
  base.policy.roles.planner.ladders = {};
  base.policy.roles.planner.fanout = 1;
  base.status.providers.claude.escalation = { level: 1, previous_level: 0, reason: "step +1: 8 points under target", signal: {} };
  base.status.roles = { planner: { fanout: 1, effective_selections: { claude: { model: "sonnet", effort: "medium", level: 0 } } } };
  return {
    ...base,
    limits: { providers: { claude: { limit: 4, source: "config", ceiling: 8 } }, total: { limit: null, source: null, ceiling: null } },
    utilization: { day: { claude: { limit: 4, slot_utilization: 0.4, holds: 9, peak_active: 4, saturated_acquires: 2, queued: 3, queue_wait: { p50_s: 12.5 } } } },
    fanout: {},
    presets: { claude: { conserve: [{ path: ["providers", "claude", "max_concurrent"], value: 1 }], max: [{ path: ["providers", "claude", "max_concurrent"], value: 8 }] } },
    file_managed: { config_file: "/x/config.toml", concurrency: { claude: 4 }, capacity: { per_provider_max: 8, total_max: null } },
  };
}

test("the provider card shows slots in force and slot use, and a preset fills the draft", async () => {
  resetDraft();
  resetAiPolicyUi();
  globalThis.fetch = async (path) => {
    if (String(path).startsWith("/api/policy")) return new Response(JSON.stringify(tuningFixture()), { status: 200 });
    return new Response("{}", { status: 404 });
  };
  const node = await renderPolicy({ query: new URLSearchParams() }, { toast: () => {}, refresh: () => {} });
  const text = textOf(node);
  assert.match(text, /In force: 4 from config · ceiling 8 from \[capacity\]/);
  assert.match(text, /Slots 40% used · 9 turns held a slot · peak 4 of 4 · 2 took the last slot/);
  assert.match(text, /3 queued now · median wait for a slot 12.5s/);
  assert.match(text, /ladder \+1/);
  assert.match(text, /step \+1: 8 points under target/);
  assert.equal(byFocusKey(node, "policy-claude-concurrency").value, "");

  byFocusKey(node, "policy-claude-preset-max").listeners.click();
  assert.equal(node.dataset.holdPoll, "1");
  assert.equal(byFocusKey(node, "policy-claude-concurrency").value, "8");
  assert.equal(byFocusKey(node, "policy-claude-preset-max").attributes["aria-pressed"], "true");

  // A hand edit afterwards is just a draft edit: the dial reads custom again.
  const slots = byFocusKey(node, "policy-claude-concurrency");
  slots.value = "5";
  slots.listeners.change();
  assert.equal(byFocusKey(node, "policy-claude-preset-max").attributes["aria-pressed"], "false");
  assert.match(textOf(node), /custom/);
});

test("a ladder step and a fan-out are edited in the role matrix", async () => {
  resetDraft();
  resetAiPolicyUi();
  globalThis.fetch = async (path) => {
    if (String(path).startsWith("/api/policy")) return new Response(JSON.stringify(tuningFixture()), { status: 200 });
    return new Response("{}", { status: 404 });
  };
  const node = await renderPolicy({ query: new URLSearchParams() }, { toast: () => {}, refresh: () => {} });
  byFocusKey(node, "policy-role-planner-claude-above-add").listeners.click();
  const stepModel = byFocusKey(node, "policy-role-planner-claude-above-0-model");
  assert.equal(stepModel.value, "sonnet", "a new step starts from the role's own model");
  stepModel.value = "haiku";
  stepModel.listeners.change();
  assert.ok("disabled" in byFocusKey(node, "policy-role-planner-claude-above-0-effort").attributes, "haiku takes no effort");

  const fanout = byFocusKey(node, "policy-role-planner-fanout");
  fanout.value = "2";
  fanout.listeners.change();
  assert.equal(byFocusKey(node, "policy-role-planner-fanout").value, "2");
  assert.match(textOf(node), /−0 \/ \+1/);
});
