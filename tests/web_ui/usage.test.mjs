import test from "node:test";
import assert from "node:assert/strict";
import { dailyTrend, failureRows, labelFor, meteringNote, slotRows, windowMark } from "../../src/taskspindle/web/static/views/usage.js";

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

test("failure rows put the most frequent code first and name a missing code", () => {
  assert.deepEqual(failureRows([
    { provider: "grok", mode: "review", code: "REVIEW_MALFORMED", count: 2 },
    { provider: "claude", mode: "consult", code: "TURN_TIMEOUT", count: 11 },
    { provider: "agy", mode: "consult", code: null, count: 2 },
  ]), [
    { provider: "claude", mode: "consult", code: "TURN_TIMEOUT", count: 11 },
    { provider: "agy", mode: "consult", code: "UNKNOWN", count: 2 },
    { provider: "grok", mode: "review", code: "REVIEW_MALFORMED", count: 2 },
  ]);
  assert.deepEqual(failureRows(undefined), []);
});

test("the daily trend keeps the last days and leaves the pass rate empty when no check ran", () => {
  const daily = Array.from({ length: 20 }, (_, index) => ({
    day: `2026-09-${String(index + 1).padStart(2, "0")}`, tasks_created: index, failures: index % 3,
    checks_run: index === 19 ? 13 : index === 18 ? 0 : 4, checks_passed: index === 19 ? 1 : 4,
  }));
  const rows = dailyTrend(daily);
  assert.equal(rows.length, 14);
  assert.equal(rows[0].day, "2026-09-07");
  assert.deepEqual(rows.at(-1), { day: "2026-09-20", tasks: 19, failures: 1, checks: "1/13", passRate: 1 / 13, passLabel: "8%" });
  assert.deepEqual(rows.at(-2), { day: "2026-09-19", tasks: 18, failures: 0, checks: "—", passRate: null, passLabel: "—" });
  assert.equal(dailyTrend(daily, 3).length, 3);
  assert.deepEqual(dailyTrend(undefined), []);
});

test("the metering note says when token and cost totals are partial", () => {
  assert.equal(meteringNote([]), null);
  assert.equal(meteringNote([{ turns: 4, turns_with_usage: 4, unmetered_turns: 0, unmetered_successful_turns: 0, unpriced_turns: 0 }]), "All 4 turns carry token usage.");
  assert.equal(meteringNote([
    { turns: 100, turns_with_usage: 80, unmetered_turns: 20, unmetered_successful_turns: 9, unpriced_turns: 2 },
    { turns: 196, turns_with_usage: 152, unmetered_turns: 44, unmetered_successful_turns: 14, unpriced_turns: 4 },
  ]), "232 of 296 turns carry token usage; 64 unmetered (23 ended normally); 6 metered but unpriced. Token and cost totals are partial.");
});

test("a window mark shows the carried percentage with the time it was reported", () => {
  assert.equal(windowMark({ window: "five_hour", status: "allowed", used_percent: null, observed_at: "t" }), "five_hour allowed");
  assert.equal(windowMark({ window: "seven_day", status: "allowed_warning", used_percent: 90, observed_at: "2030-01-10T07:00:00Z", used_percent_observed_at: "2030-01-10T07:00:00Z" }), "seven_day allowed_warning 90%");
  const current = { window: "five_hour", status: "allowed", used_percent: 81, resets_at: "2030-01-10T10:00:00Z", observed_at: "2030-01-10T08:00:00Z", used_percent_observed_at: "2030-01-10T07:00:00Z" };
  assert.equal(windowMark(current, Date.parse("2030-01-10T09:00:00Z")), "five_hour allowed 81% (as of 2030-01-10 07:00 UTC)");
  assert.equal(windowMark(current, Date.parse("2030-01-10T11:00:00Z")), "five_hour allowed 81% (as of 2030-01-10 07:00 UTC) (reset passed)");
});
