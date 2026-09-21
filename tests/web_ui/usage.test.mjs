import test from "node:test";
import assert from "node:assert/strict";
import { labelFor, slotRows } from "../../src/taskspindle/web/static/views/usage.js";

test("compound usage groups keep every selected dimension in chart labels", () => {
  assert.equal(labelFor({ provider: "claude", day: "2026-09-07" }, ["provider", "day"]), "claude · 2026-09-07");
  assert.equal(labelFor({ provider: "grok", model: "grok-4.6" }, ["provider", "model"]), "grok · grok-4.6");
  assert.equal(labelFor({ repository_id: "repo-1", repository_path: "/work/taskspindle" }, ["repository_id"]), "/work/taskspindle");
});

test("slot rows state occupancy as a share of offered slot time and leave unknowns blank", () => {
  assert.deepEqual(slotRows([
    { provider: "claude", limit: 4, slot_utilization: 0.4167, holds: 9, peak_active: 4, saturated_acquires: 2, queued: 3, queue_wait: { p50_s: 12.5 } },
    { provider: "agy", limit: null, slot_utilization: null, queue_wait: { p50_s: null } },
  ]), [
    { provider: "claude", limit: 4, used: "42%", held: 9, peak: 4, saturated: 2, queued: 3, wait: "12.5s" },
    { provider: "agy", limit: "—", used: "—", held: 0, peak: 0, saturated: 0, queued: 0, wait: "—" },
  ]);
  assert.deepEqual(slotRows(undefined), []);
});
