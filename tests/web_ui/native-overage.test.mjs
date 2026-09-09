import test from "node:test";
import assert from "node:assert/strict";

class FakeNode {
  constructor(tag = "", text = "") { this.tag = tag; this.textContent = text; this.children = []; this.dataset = {}; this.attributes = {}; }
  append(...children) { this.children.push(...children); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener() {}
}
globalThis.Node = FakeNode;
globalThis.document = { createElement: (tag) => new FakeNode(tag), createTextNode: (text) => new FakeNode("#text", text) };
const { nativeOverageDetails, nativeOverageSummary } = await import("../../src/taskspindle/web/static/native-overage.js");
const textOf = (node) => node.textContent + node.children.map(textOf).join("");
const hasButton = (node) => node.tag === "button" || node.children.some(hasButton);

test("missing billing stays unknown and account controls are accurately scoped", () => {
  const missing = nativeOverageDetails(null);
  assert.match(textOf(missing), /Unknown/);
  const account = nativeOverageDetails({ policy: "observe_only", control_scope: "account", eligibility: "unknown" });
  assert.match(textOf(account), /not a per-task spending block/);
  assert.equal(hasButton(account), false);
});

test("only documented cents render as currency and zero is preserved", () => {
  const info = { policy: "provider_managed", control_scope: "account", eligibility: "overage", billing_classification: "mixed" };
  const known = nativeOverageDetails(info, { freshness: "stale", billing: {
    unit: "usd_cents", currency: "USD", prepaid_balance: 1234, on_demand_cap: 0,
    auto_topup: { enabled: false, topup_amount: 500 },
  } });
  assert.match(textOf(known), /\$12\.34/);
  assert.match(textOf(known), /\$0\.00/);
  assert.match(textOf(known), /stale/);
  assert.match(textOf(known), /not task charges/);
  const unknown = nativeOverageDetails(info, { billing: { unit: "ticks", prepaid_balance: 1234 } });
  assert.doesNotMatch(textOf(unknown), /\$12\.34/);
});

test("overage summary retains historical unknowns and carries no spending controls", () => {
  const node = nativeOverageSummary({ total_turns: 2, observed_turns: 1, unknown_turns: 1,
    groups: [{ provider: "claude", policy: "observe_only", billing_classification: "unknown", turns: 1 },
      { provider: "grok", policy: "provider_managed", billing_classification: "native_overage", turns: 1 }] });
  assert.match(textOf(node), /Unknown/);
  assert.match(textOf(node), /Native overage/);
  assert.equal(hasButton(node), false);
});
