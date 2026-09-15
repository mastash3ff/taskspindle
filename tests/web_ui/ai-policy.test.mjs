import test from "node:test";
import assert from "node:assert/strict";

class FakeNode {
  constructor(tag = "", text = "") {
    this.tag = tag;
    this.textContent = text;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.className = "";
    this.checked = false;
    this.disabled = false;
  }
  append(...children) { this.children.push(...children); }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
    if (key === "disabled") this.disabled = true;
    if (key === "value") this.value = String(value);
  }
  addEventListener(kind, handler) { this.listeners[kind] = handler; }
}

globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (text) => new FakeNode("#text", text),
};
globalThis.HTMLInputElement = class FakeInput {};
globalThis.HTMLTextAreaElement = class FakeTextArea {};

const { renderAiPolicyCard, resetAiPolicyUi, __test__ } = await import("../../src/taskspindle/web/static/ai-policy.js");
const { selectedHosts, aggregateStatus, aggregateMode, expectedRevisions, canApply, mergeHosts, STATUS_LABELS, MODE_HELP } = __test__;

const find = (node, predicate) => {
  const result = predicate(node) ? [node] : [];
  return result.concat(...(node.children || []).map((child) => find(child, predicate)));
};
const textOf = (node) => (node.textContent || "") + (node.children || []).map(textOf).join("");
const byFocusKey = (root, key) => find(root, (node) => node.dataset?.focusKey === key)[0];
const byAttr = (root, key, value) => find(root, (node) => node.attributes[key] === value)[0];

function fixture(overrides = {}) {
  return {
    hosts: [
      { host: "windows", mode: "ensemble", status: "configured", revision: "win-1", checks: [{ name: "ready", ok: true, detail: "ok" }] },
      { host: "wsl", mode: "ensemble", status: "configured", revision: "wsl-1", checks: [{ name: "ready", ok: true, detail: "ok" }] },
    ],
    applies_to: "new_sessions",
    csrf_token: "tok",
    ...overrides,
  };
}

test("selectedHosts expands Both and keeps a single host", () => {
  assert.deepEqual(selectedHosts("both"), ["windows", "wsl"]);
  assert.deepEqual(selectedHosts("windows"), ["windows"]);
});

test("aggregateStatus reports mixed when hosts disagree", () => {
  assert.equal(aggregateStatus([{ status: "configured" }, { status: "configured" }]), "configured");
  assert.equal(aggregateStatus([{ status: "configured" }, { status: "needs_repair" }]), "mixed");
  assert.equal(aggregateMode([{ mode: "native" }, { mode: "ensemble" }]), "mixed");
});

test("canApply requires revisions and rejects a fully unavailable selection", () => {
  assert.equal(canApply([{ revision: "r1", status: "configured" }]), true);
  assert.equal(canApply([{ revision: null, status: "unavailable" }]), false);
  assert.equal(canApply([{ revision: "r1", status: "unavailable" }, { revision: "r2", status: "unavailable" }]), false);
});

test("the Codex AI mode card exposes keyboard-accessible labels", () => {
  resetAiPolicyUi();
  const node = renderAiPolicyCard(fixture());
  assert.match(textOf(node), /Applies to new Codex sessions only/);
  assert.equal(node.attributes["aria-label"], "Codex AI mode");
  assert.ok(byAttr(node, "aria-label", "Codex AI mode"));
  assert.ok(byAttr(node, "aria-label", "Codex AI mode host"));
  assert.ok(byFocusKey(node, "ai-policy-mode-ensemble"));
  assert.ok(byFocusKey(node, "ai-policy-mode-native"));
  assert.ok(byFocusKey(node, "ai-policy-host-both"));
  assert.ok(byFocusKey(node, "ai-policy-host-windows"));
  assert.ok(byFocusKey(node, "ai-policy-host-wsl"));
  const apply = byFocusKey(node, "ai-policy-apply");
  assert.equal(apply.attributes["aria-label"], "Apply Codex AI mode");
  assert.match(textOf(node), /Ensemble/);
  assert.match(textOf(node), /Native/);
  assert.match(textOf(node), /Both/);
  assert.match(textOf(node), /Windows/);
  assert.match(textOf(node), /WSL/);
  const ensembleLabel = find(node, (item) => item.tag === "label" && textOf(item).includes("Ensemble"))[0];
  assert.ok(ensembleLabel);
  assert.ok(find(ensembleLabel, (item) => item.attributes.type === "radio").length);
});

test("missing adapter renders unavailable and disables Apply", () => {
  resetAiPolicyUi();
  const node = renderAiPolicyCard({
    hosts: [
      { host: "windows", mode: null, status: "unavailable", revision: null, checks: [], error: "ADAPTER_UNAVAILABLE" },
      { host: "wsl", mode: null, status: "unavailable", revision: null, checks: [], error: "ADAPTER_UNAVAILABLE" },
    ],
    applies_to: "new_sessions",
    csrf_token: "tok",
  });
  assert.match(textOf(node), /unavailable/);
  assert.equal(byFocusKey(node, "ai-policy-apply").disabled, true);
});

test("mixed host state is labeled mixed", () => {
  resetAiPolicyUi();
  const node = renderAiPolicyCard(fixture({
    hosts: [
      { host: "windows", mode: "native", status: "configured", revision: "a", checks: [] },
      { host: "wsl", mode: "ensemble", status: "needs_repair", revision: "b", checks: [] },
    ],
  }));
  assert.match(textOf(node), /mixed/);
  assert.match(textOf(node), new RegExp(STATUS_LABELS.needs_repair));
});

test("Apply sends expected revisions and surfaces a stale failure with fresh status", async () => {
  resetAiPolicyUi();
  let captured = null;
  globalThis.fetch = async (path, init) => {
    captured = { path, init };
    return new Response(JSON.stringify({
      error: "AI_POLICY_APPLY_FAILED",
      applies_to: "new_sessions",
      csrf_token: "tok",
      hosts: [
        { host: "windows", mode: "ensemble", status: "configured", revision: "fresh", checks: [] },
        { host: "wsl", mode: "ensemble", status: "configured", revision: "fresh", checks: [] },
      ],
      results: [
        { host: "windows", ok: false, error: "STALE_REVISION" },
        { host: "wsl", ok: false, error: "STALE_REVISION" },
      ],
    }), { status: 409 });
  };
  const painted = [];
  const node = renderAiPolicyCard(fixture(), { paint: () => painted.push(1), toast() {} });
  await byFocusKey(node, "ai-policy-apply").listeners.click();
  assert.equal(captured.path, "/api/ai-policy");
  assert.equal(captured.init.method, "PUT");
  assert.equal(captured.init.headers["X-TaskSpindle-CSRF"], "tok");
  const body = JSON.parse(captured.init.body);
  assert.deepEqual(body.expected_revisions, expectedRevisions(fixture().hosts));
  const after = renderAiPolicyCard(fixture({
    hosts: [
      { host: "windows", mode: "ensemble", status: "configured", revision: "fresh", checks: [] },
      { host: "wsl", mode: "ensemble", status: "configured", revision: "fresh", checks: [] },
    ],
  }));
  assert.match(textOf(after), /STALE_REVISION|Apply did not succeed/);
  assert.ok(painted.length);
});

test("partial Apply failure does not claim both hosts succeeded", async () => {
  resetAiPolicyUi();
  globalThis.fetch = async () => new Response(JSON.stringify({
    error: "AI_POLICY_APPLY_FAILED",
    applies_to: "new_sessions",
    csrf_token: "tok",
    hosts: [
      { host: "windows", mode: "native", status: "configured", revision: "r2", checks: [] },
      { host: "wsl", mode: "ensemble", status: "update_failed", revision: "wsl-1", checks: [], error: "WSL_FAILED" },
    ],
    results: [
      { host: "windows", ok: true },
      { host: "wsl", ok: false, error: "WSL_FAILED" },
    ],
  }), { status: 409 });
  let latest = fixture();
  const root = { node: null };
  const paint = () => { root.node = renderAiPolicyCard(latest, { paint, onAiPolicy(next) { latest = next; }, toast() {} }); };
  paint();
  await byFocusKey(root.node, "ai-policy-apply").listeners.click();
  paint();
  const text = textOf(root.node);
  assert.match(text, /WSL_FAILED/);
  assert.match(text, /did not succeed on every host/);
  assert.doesNotMatch(text, /Codex AI mode applied for new sessions/);
  assert.doesNotMatch(text, /mode configured/);
});

test("mode radios are explained in one sentence next to the choices", () => {
  resetAiPolicyUi();
  const node = renderAiPolicyCard(fixture());
  assert.match(MODE_HELP, /TaskSpindle-first with native fallback/);
  assert.match(MODE_HELP, /external integration off/);
  assert.match(MODE_HELP, /retains Codex subagents/);
  assert.match(textOf(node), /TaskSpindle-first with native fallback/);
  assert.match(textOf(node), /external integration off/);
  const group = byAttr(node, "aria-describedby", "ai-policy-mode-help");
  assert.ok(group);
  const help = find(node, (item) => item.attributes.id === "ai-policy-mode-help")[0];
  assert.ok(help);
  assert.equal(help.tag, "p");
});

test("routine integrity checks start collapsed and failures stay visible", () => {
  resetAiPolicyUi();
  const routine = Array.from({ length: 12 }, (_, index) => ({ name: `check-${index + 1}`, ok: true, detail: "ok" }));
  const node = renderAiPolicyCard(fixture({
    hosts: [
      { host: "windows", mode: "ensemble", status: "needs_repair", revision: "win-1", checks: [...routine, { name: "mcp-bind", ok: false, detail: "missing" }] },
      { host: "wsl", mode: "ensemble", status: "configured", revision: "wsl-1", checks: [] },
    ],
  }));
  const details = find(node, (item) => item.tag === "details")[0];
  assert.ok(details);
  assert.equal(Boolean(details.open), false);
  assert.match(textOf(details), /12 routine integrity checks/);
  assert.doesNotMatch(textOf(details), /mcp-bind/);
  const failures = byAttr(node, "aria-label", "Failed Codex AI mode checks");
  assert.ok(failures);
  assert.match(textOf(failures), /mcp-bind/);
  assert.match(textOf(failures), /fail/);
});

test("mergeHosts updates returned hosts without dropping the others", () => {
  const merged = mergeHosts(
    [{ host: "windows", revision: "win-1" }, { host: "wsl", revision: "wsl-1" }],
    [{ host: "windows", revision: "win-2" }],
  );
  assert.deepEqual(merged.map((row) => [row.host, row.revision]), [["windows", "win-2"], ["wsl", "wsl-1"]]);
});

test("single-host Apply keeps the other host revision", async () => {
  resetAiPolicyUi();
  globalThis.fetch = async (_path, init = {}) => {
    if (init.method === "PUT") {
      return new Response(JSON.stringify({
        applies_to: "new_sessions",
        csrf_token: "tok",
        hosts: [{ host: "windows", mode: "native", status: "configured", revision: "win-2", checks: [] }],
        results: [{ host: "windows", ok: true }],
      }), { status: 200 });
    }
    return new Response("{}", { status: 404 });
  };
  let latest = fixture();
  const root = { node: null };
  const ctx = {
    toast() {},
    onAiPolicy(next) { latest = next; },
    paint() { root.node = renderAiPolicyCard(latest, ctx); },
  };
  ctx.paint();
  byFocusKey(root.node, "ai-policy-host-windows").listeners.change();
  await byFocusKey(root.node, "ai-policy-apply").listeners.click();
  byFocusKey(root.node, "ai-policy-host-both").listeners.change();
  assert.equal(byFocusKey(root.node, "ai-policy-apply").disabled, false);
  const wsl = (latest.hosts || []).find((row) => row.host === "wsl");
  assert.equal(wsl?.revision, "wsl-1");
});

test("Apply freezes radios and toasts the captured mode", async () => {
  resetAiPolicyUi();
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  globalThis.fetch = async (_path, init = {}) => {
    if (init.method === "PUT") {
      await gate;
      return new Response(JSON.stringify({
        applies_to: "new_sessions",
        csrf_token: "tok",
        hosts: fixture().hosts.map((row) => ({ ...row, mode: "native", revision: "r2" })),
        results: [{ host: "windows", ok: true }, { host: "wsl", ok: true }],
      }), { status: 200 });
    }
    return new Response("{}", { status: 404 });
  };
  const toasts = [];
  const node = renderAiPolicyCard(fixture(), { toast: (message) => toasts.push(message), paint() {} });
  byFocusKey(node, "ai-policy-mode-native").listeners.change();
  const done = byFocusKey(node, "ai-policy-apply").listeners.click();
  byFocusKey(node, "ai-policy-mode-ensemble").listeners.change();
  release();
  await done;
  assert.deepEqual(toasts, ["Native mode configured. Start a new Codex conversation to use it."]);
});

test("successful Apply toasts the configured mode and a new conversation", async () => {
  resetAiPolicyUi();
  globalThis.fetch = async () => new Response(JSON.stringify({
    applies_to: "new_sessions",
    csrf_token: "tok",
    hosts: [
      { host: "windows", mode: "native", status: "configured", revision: "r2", checks: [] },
      { host: "wsl", mode: "native", status: "configured", revision: "r2", checks: [] },
    ],
    results: [{ host: "windows", ok: true }, { host: "wsl", ok: true }],
  }), { status: 200 });
  const toasts = [];
  const node = renderAiPolicyCard(fixture(), { toast: (message) => toasts.push(message), paint() {} });
  byFocusKey(node, "ai-policy-mode-native").listeners.change();
  await byFocusKey(node, "ai-policy-apply").listeners.click();
  assert.deepEqual(toasts, ["Native mode configured. Start a new Codex conversation to use it."]);
});
