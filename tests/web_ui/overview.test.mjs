import test from "node:test";
import assert from "node:assert/strict";

class FakeNode {
  constructor(tag = "", text = "") {
    this.tag = tag;
    this.textContent = text;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.className = "";
  }
  append(...children) { this.children.push(...children); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener() {}
}

globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (text) => new FakeNode("#text", text),
};

const requested = [];
const payloads = {
  "/api/overview": { counts: {}, active_tasks: [], attention_tasks: [], truncated: {} },
  "/api/providers": { providers: [] },
  "/api/health": { version: "test", read_only: true, task_database_read_only: true },
};
globalThis.fetch = async (path) => {
  requested.push(path);
  return new Response(JSON.stringify(payloads[path]), { status: path in payloads ? 200 : 404 });
};

const { renderOverview } = await import("../../src/taskspindle/web/static/views/overview.js");

function texts(node) {
  return [node.textContent, ...node.children.flatMap(texts)].filter(Boolean);
}

test("overview uses only read-only task, worker, and health projections", async () => {
  await renderOverview({}, {});

  assert.deepEqual(requested.sort(), ["/api/health", "/api/overview", "/api/providers"]);
});

test("an active task needing attention names its error code only in the attention list", async () => {
  const stuck = {
    id: "ts_stuck", state: "CANCELLING", summary: "Stuck launch", updated_at: "2030-01-01T00:00:00Z",
    repository: { id: null, path: null }, error: { code: "UNIT_START_UNCERTAIN", message: "unsettled" },
  };
  payloads["/api/overview"] = {
    counts: { total: 1, active: 1, attention: 1 }, active_tasks: [stuck], attention_tasks: [stuck], truncated: {},
  };

  const rendered = texts(await renderOverview({}, {}));

  assert.equal(rendered.filter((text) => text === "ts_stuck · No repository · UNIT_START_UNCERTAIN").length, 1);
  assert.equal(rendered.filter((text) => text === "ts_stuck · No repository").length, 1);
});
