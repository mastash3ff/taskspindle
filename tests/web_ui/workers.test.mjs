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

const { recoveryFeedback } = await import("../../src/taskspindle/web/static/views/workers.js");
const textOf = (node) => node.textContent + node.children.map(textOf).join("");
const nodes = (node, predicate) => {
  const result = predicate(node) ? [node] : [];
  return result.concat(...node.children.map((child) => nodes(child, predicate)));
};

test("recovery feedback shows arm, pending task, and settled outcome without action requests", async () => {
  const command = "taskspindle providers --retry-next --provider grok --model 'grok safe'";
  const copied = [];
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { clipboard: { writeText: async (value) => copied.push(value) } },
  });
  const armed = recoveryFeedback({
    state: "armed", permit_id: "recovery-1", provider: "grok", model: "grok safe",
    expires_at: "2030-01-03T00:00:00Z", evidence_revision: "evidence-1", cli_command: command,
  });
  assert.match(textOf(armed), /Retry armed/);
  assert.match(textOf(armed), /Providergrok/);
  assert.match(textOf(armed), /Model scopegrok safe/);
  assert.match(textOf(armed), /Permitrecovery-1/);
  assert.match(textOf(armed), /Deadline/);
  assert.match(textOf(armed), /evidence-1/);
  const copy = nodes(armed, (node) => node.tag === "button")[0];
  await copy.listeners.click();
  assert.deepEqual(copied, [command]);
  assert.match(textOf(armed), /Command copied/);

  const claimed = recoveryFeedback({
    state: "claimed", permit_id: "recovery-1", provider: "grok", model: null,
    task_id: "ts_attempt", expires_at: "2030-01-03T00:00:00Z",
  });
  assert.match(textOf(claimed), /Recovery attempt pending/);
  assert.match(textOf(claimed), /Model scopeProvider default/);
  const taskLink = nodes(claimed, (node) => node.tag === "a")[0];
  assert.equal(taskLink.attributes.href, "#/tasks/ts_attempt");

  const settled = recoveryFeedback({
    state: "failed", task_id: "ts_attempt", outcome: "no_access", outcome_code: "PROVIDER_AUTH_EXPIRED",
  });
  assert.match(textOf(settled), /Recovery failed/);
  assert.match(textOf(settled), /no_access/);
  assert.match(textOf(settled), /PROVIDER_AUTH_EXPIRED/);
});

test("recovery feedback exposes the generated CLI only and has no mutation control", () => {
  const command = "taskspindle providers --retry-next --provider claude";
  const available = recoveryFeedback({ state: "none", provider: "claude", can_arm: true, cli_command: command });
  assert.match(textOf(available), /Controlled retry available/);
  assert.match(textOf(available), /A recorded refusal/);
  assert.match(textOf(available), new RegExp(command));
  assert.deepEqual(nodes(available, (node) => node.tag === "button").map((node) => node.textContent), ["Copy command"]);
  assert.equal(recoveryFeedback({ state: "none", can_arm: false, cli_command: null }), null);
});

test("shared-scope permits and newer refusals retain explicit warnings", () => {
  const mismatched = recoveryFeedback({
    state: "armed", provider: "claude", model: "sonnet", permit_id: "recovery-shared",
    evidence_revision: "evidence-original", next_action: "inspect_permit",
  });
  assert.match(textOf(mismatched), /Providerclaude/);
  assert.match(textOf(mismatched), /Model scopesonnet/);
  assert.match(textOf(mismatched), /Permitrecovery-shared/);
  assert.match(textOf(mismatched), /cannot authorize this projection/);

  const succeeded = recoveryFeedback({
    state: "succeeded", provider: "claude", model: null, permit_id: "recovery-old",
    task_id: "ts_old", outcome: "succeeded", next_action: "arm",
  });
  assert.match(textOf(succeeded), /Recovery succeeded/);
  assert.match(textOf(succeeded), /Current access is separate/);
  assert.match(textOf(succeeded), /newer availability evidence/);
});
