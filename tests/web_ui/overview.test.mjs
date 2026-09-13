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
globalThis.fetch = async (path) => {
  requested.push(path);
  const payloads = {
    "/api/overview": { counts: {}, active_tasks: [], attention_tasks: [], truncated: {} },
    "/api/providers": { providers: [] },
    "/api/health": { version: "test", read_only: true, task_database_read_only: true },
  };
  return new Response(JSON.stringify(payloads[path]), { status: path in payloads ? 200 : 404 });
};

const { renderOverview } = await import("../../src/taskspindle/web/static/views/overview.js");

test("overview uses only read-only task, worker, and health projections", async () => {
  await renderOverview({}, {});

  assert.deepEqual(requested.sort(), ["/api/health", "/api/overview", "/api/providers"]);
});
