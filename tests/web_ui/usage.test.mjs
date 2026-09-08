import test from "node:test";
import assert from "node:assert/strict";
import { labelFor } from "../../src/taskspindle/web/static/views/usage.js";

test("compound usage groups keep every selected dimension in chart labels", () => {
  assert.equal(labelFor({ provider: "claude", day: "2026-09-07" }, ["provider", "day"]), "claude · 2026-09-07");
  assert.equal(labelFor({ provider: "grok", model: "grok-4.6" }, ["provider", "model"]), "grok · grok-4.6");
  assert.equal(labelFor({ repository_id: "repo-1", repository_path: "/work/taskspindle" }, ["repository_id"]), "/work/taskspindle");
});
