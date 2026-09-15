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
  }
  append(...children) { this.children.push(...children); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(kind, handler) { this.listeners[kind] = handler; }
}

globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (text) => new FakeNode("#text", text),
};

const { __test__ } = await import("../../src/taskspindle/web/static/views/workers.js");
const textOf = (node) => node.textContent + node.children.map(textOf).join("");

test("availability card shows the ok state with no reset or eligible time", () => {
  const card = __test__.availabilityCard({ id: "claude" }, { state: "ok", reset_at: null, eligible_at: null, reason: null });
  assert.match(textOf(card), /Worker access/);
  assert.match(textOf(card), /No current refusal recorded/);
  assert.doesNotMatch(textOf(card), /Reset/);
  assert.doesNotMatch(textOf(card), /Eligible again/);
});

test("availability card shows a refusal's state, reason, reset time, and eligible time", () => {
  const card = __test__.availabilityCard({ id: "grok" }, {
    state: "throttled", reset_at: "2030-01-03T00:00:00Z", eligible_at: "2030-01-03T00:00:00Z",
    reason: "The provider reported a usage limit.",
  });
  assert.match(textOf(card), /throttled/);
  assert.match(textOf(card), /The provider reported a usage limit/);
  assert.match(textOf(card), /Reset/);
  assert.match(textOf(card), /Eligible again/);
});

test("availability card falls back to an eligible-at fifteen minutes after observation", () => {
  const card = __test__.availabilityCard({ id: "claude" }, {
    state: "auth_expired", reset_at: null, eligible_at: "2030-01-01T00:15:00Z",
    reason: "Provider authentication is required.",
  });
  assert.match(textOf(card), /auth expired/);
  assert.match(textOf(card), /Provider authentication is required/);
  assert.match(textOf(card), /Eligible again/);
  assert.doesNotMatch(textOf(card), /^Reset/);
});
