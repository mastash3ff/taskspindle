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
    this.parent = null;
    this.clicked = 0;
  }
  append(...children) {
    for (const child of children) {
      this.children.push(child);
      child.parent = this;
    }
  }
  setAttribute(key, value) {
    this.attributes[key] = String(value);
    if (key === "id") this.id = String(value);
  }
  addEventListener(kind, handler) { this.listeners[kind] = handler; }
  replaceWith(next) {
    const index = this.parent.children.indexOf(this);
    this.parent.children[index] = next;
    next.parent = this.parent;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const matches = [];
    const wantedId = selector.startsWith("#") ? selector.slice(1) : null;
    const wantedRole = selector === '[role="tab"]' ? "tab" : null;
    const visit = (node) => {
      if ((wantedId && node.id === wantedId) || (wantedRole && node.attributes.role === wantedRole)) matches.push(node);
      node.children.forEach(visit);
    };
    this.children.forEach(visit);
    return matches;
  }
  scrollIntoView() {}
  focus() { this.focused = true; }
  click() { this.clicked += 1; }
  get isConnected() { return true; }
}

globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (text) => new FakeNode("#text", text),
};
globalThis.CSS = { escape: (value) => value };
globalThis.requestAnimationFrame = (callback) => callback();

const { renderTaskDetail } = await import("../../src/taskspindle/web/static/views/tasks.js");

const diff = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1,2 @@\n old\n+new\n";
const detail = {
  task: {
    id: "ts_fixture", state: "RESULT_READY", cleanup_state: "RETAINED", provider: "grok",
    mode: "implement", summary: "Synthetic candidate", repository: { id: "repo", path: "/fixture" },
    candidate_sha: "c".repeat(40), candidate_revision: 1, diff_size: diff.length,
    requested_model: "grok-4.6", resolved_model: "grok-4.6", updated_at: "2026-09-07T12:00:00Z",
    created_at: "2026-09-07T11:00:00Z", warnings: [],
  },
  repository: { id: "repo", display_path: "/fixture" },
  turns: [{ revision: 1, kind: "initial", response: "Candidate ready." }],
  checks: [{ command: "pytest -q", ok: true, exit_code: 0, duration_ms: 20 }],
  events: [], worker_log: "fixture log",
  review: {
    verdict: "CONCERN", provider: "claude", candidate_sha: "c".repeat(40),
    summary: "Inspect one line.",
    findings: [{ id: "finding-1", severity: "medium", path: "src/a.py", line: 2,
      evidence: "Synthetic evidence", remedy: "Synthetic remedy" }],
  },
};

const response = (body, status = 200) => new Response(body, { status });
const route = (query = "") => ({ id: "ts_fixture", query: new URLSearchParams(query) });
const textOf = (node) => node.textContent + node.children.map(textOf).join("");
const nodes = (node, predicate) => {
  const result = predicate(node) ? [node] : [];
  return result.concat(...node.children.map((child) => nodes(child, predicate)));
};

test("task detail retains only an exact-candidate diff and fails closed on mismatch", async () => {
  let diffMode = "ok";
  const diffRequests = [];
  globalThis.fetch = async (url) => {
    const path = String(url);
    if (path.startsWith("/api/tasks/ts_fixture/diff")) {
      diffRequests.push(path);
      if (diffMode === "network") throw new Error("temporary failure");
      if (diffMode === "changed") return response('{"error":"CANDIDATE_CHANGED"}', 409);
      return response(diff);
    }
    return response(JSON.stringify(detail));
  };

  const first = await renderTaskDetail("ts_fixture", { route: route("tab=changes&finding=finding-1") });
  assert.match(diffRequests[0], /revision=1/);
  assert.match(diffRequests[0], /candidate_sha=c{40}/);
  const findingLink = nodes(first, (node) => node.tag === "a" && node.attributes.href?.includes("finding=finding-1"))[0];
  assert.match(findingLink.attributes.href, /candidate=c{40}/);
  assert.match(findingLink.attributes.href, /file=src%2Fa.py/);

  diffMode = "network";
  const cached = await renderTaskDetail("ts_fixture", { route: route("tab=changes") });
  assert.match(textOf(cached), /last verified copy for this exact candidate/);
  assert.match(textOf(cached), /\+new/);

  diffMode = "changed";
  const changed = await renderTaskDetail("ts_fixture", { route: route("tab=changes") });
  assert.match(textOf(changed), /candidate changed/i);
  assert.doesNotMatch(textOf(changed), /Synthetic evidence|\+new/);

  diffMode = "network";
  const afterChanged = await renderTaskDetail("ts_fixture", { route: route("tab=changes") });
  assert.doesNotMatch(textOf(afterChanged), /last verified copy|\+new/);

  const before = diffRequests.length;
  const oldLink = await renderTaskDetail("ts_fixture", { route: route(`tab=changes&candidate=${"d".repeat(40)}`) });
  assert.equal(diffRequests.length, before);
  assert.match(textOf(oldLink), /older candidate/);
  assert.doesNotMatch(textOf(oldLink), /CONCERN|Synthetic evidence/);
});

test("task detail tabs use roving focus and URL-backed activation", async () => {
  globalThis.fetch = async (url) => String(url).includes("/diff") ? response(diff) : response(JSON.stringify(detail));
  const view = await renderTaskDetail("ts_fixture", { route: route("tab=result") });
  const tablist = nodes(view, (node) => node.attributes.role === "tablist")[0];
  const tabs = tablist.querySelectorAll('[role="tab"]');
  const active = tabs.find((node) => node.attributes["aria-selected"] === "true");
  let prevented = false;
  tablist.listeners.keydown({ key: "ArrowRight", target: active, preventDefault: () => { prevented = true; } });
  const next = tabs[(tabs.indexOf(active) + 1) % tabs.length];
  assert.equal(prevented, true);
  assert.equal(next.focused, true);
  assert.equal(next.clicked, 1);
});

test("task events explain controlled provider recovery attempts and outcomes", async () => {
  const recoveryDetail = { ...detail, events: [
    { at: "2026-09-07T11:01:00Z", kind: "WARNING",
      payload: { code: "PROVIDER_RECOVERY_ATTEMPT", permit_id: "recovery-1", model: "grok-4.6" } },
    { at: "2026-09-07T11:02:00Z", kind: "WARNING",
      payload: { code: "PROVIDER_RECOVERY_OUTCOME", permit_id: "recovery-1", outcome: "failed", outcome_code: "PROVIDER_AUTH_EXPIRED" } },
  ] };
  globalThis.fetch = async () => response(JSON.stringify(recoveryDetail));
  const view = await renderTaskDetail("ts_fixture", { route: route("tab=events") });
  assert.match(textOf(view), /Controlled worker recovery attempt/);
  assert.match(textOf(view), /permit recovery-1/);
  assert.match(textOf(view), /model grok-4\.6/);
  assert.match(textOf(view), /Controlled worker recovery outcome/);
  assert.match(textOf(view), /failed/);
  assert.match(textOf(view), /code PROVIDER_AUTH_EXPIRED/);
});
