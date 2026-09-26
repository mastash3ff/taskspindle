import { getJSON } from "../api.js";
import { badge, emptyState, h, metric, s, sectionHeading, table } from "../dom.js";
import { updateRouteQuery } from "../router.js";

const GROUPS = ["provider", "day", "provider_day", "model", "role", "mode", "repository_id"];
const GROUP_LABELS = { provider: "Provider", day: "Day", provider_day: "Provider and day", model: "Model", role: "Role", mode: "Mode", repository_id: "Repository" };
const numeric = (value) => value == null || value === "" || !Number.isFinite(Number(value)) ? null : Number(value);
const compact = (value) => new Intl.NumberFormat(undefined, { notation: Math.abs(value) >= 10000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(value || 0);

function controls(route) {
  const form = h("form", { class: "filter-bar usage-filters" },
    h("label", { class: "filter-field" }, h("span", { text: "Since" }), h("input", { name: "since", value: route.query.get("since") || "7d", placeholder: "7d, 24h, or ISO-8601", dataset: { focusKey: "usage-since" } })),
    h("label", { class: "filter-field" }, h("span", { text: "Group by" }), h("select", { name: "group_by", dataset: { focusKey: "usage-group" } }, GROUPS.map((group) => h("option", { value: group, text: GROUP_LABELS[group] })) )),
    h("label", { class: "filter-field" }, h("span", { text: "Worker" }), h("input", { name: "provider", value: route.query.get("provider") || "", placeholder: "All workers", dataset: { focusKey: "usage-provider" } })),
    h("button", { class: "button button-secondary", type: "submit", text: "Apply" }),
  );
  form.elements.group_by.value = route.query.get("group_by") || "provider";
  form.addEventListener("submit", (event) => { event.preventDefault(); updateRouteQuery(Object.fromEntries(new FormData(form))); });
  return form;
}

export function labelFor(row, keys) {
  const parts = keys.filter((key) => row[key] != null).map((key) => key === "repository_id" ? row.repository_path || row[key] : row[key]);
  return parts.length ? parts.join(" · ") : "Other";
}

function usageChart(rows, keys, title = "Tokens by usage group") {
  if (!rows.length) return emptyState("No usage recorded", "No turns match this period and filter.");
  const values = rows.map((row) => {
    const input = numeric(row.input_tokens), output = numeric(row.output_tokens);
    return input == null && output == null ? null : (input || 0) + (output || 0);
  });
  const max = Math.max(...values.filter((value) => value != null), 1);
  const width = 760, height = Math.max(190, rows.length * 42 + 44), left = 150, right = 108, plot = width - left - right;
  const id = `chart-${Math.random().toString(36).slice(2)}`;
  const svg = s("svg", { class: "usage-chart", viewBox: `0 0 ${width} ${height}`, role: "img", "aria-labelledby": `${id}-title ${id}-desc` }, s("title", { id: `${id}-title`, text: title }), s("desc", { id: `${id}-desc`, text: "Horizontal bars compare combined input and output tokens. The table below contains exact values and identifies missing observations." }));
  rows.forEach((row, index) => {
    const y = 24 + index * 42, value = values[index], bar = value == null ? 0 : Math.max(value ? 2 : 0, (value / max) * plot);
    svg.append(s("text", { x: left - 12, y: y + 15, "text-anchor": "end", class: "chart-label", text: String(labelFor(row, keys)).slice(0, 22) }), s("rect", { x: left, y, width: plot, height: 20, rx: 4, class: "chart-track" }), s("rect", { x: left, y, width: bar, height: 20, rx: 4, class: "chart-bar" }), s("text", { x: left + bar + 8, y: y + 15, class: "chart-value", text: value == null ? "Not observed" : compact(value) }));
  });
  return h("div", { class: "chart-wrap" }, svg);
}

function outcomeChart(rows) {
  if (!rows?.length) return emptyState("No outcomes", "Nothing was recorded for this period.");
  const grouped = new Map();
  for (const row of rows) grouped.set(row.state || "Unknown", (grouped.get(row.state || "Unknown") || 0) + (numeric(row.count) || 0));
  const items = [...grouped].map(([state, count]) => ({ state, count }));
  const max = Math.max(...items.map((item) => item.count), 1), width = 620, height = Math.max(160, items.length * 38 + 36), left = 165, plot = width - left - 96;
  const svg = s("svg", { class: "usage-chart outcome-chart", viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Task outcomes by state" });
  items.forEach((item, index) => {
    const y = 18 + index * 38, bar = Math.max(item.count ? 2 : 0, item.count / max * plot);
    const state = item.state.toLowerCase();
    const tone = /failed|rejected/.test(state) ? "danger" : /interrupt|cancel|ambiguous|repair/.test(state) ? "warning" : /complete|accepted|running|ready/.test(state) ? "positive" : "neutral";
    svg.append(s("text", { x: left - 10, y: y + 14, "text-anchor": "end", class: "chart-label", text: item.state }), s("rect", { x: left, y, width: plot, height: 18, rx: 4, class: "chart-track" }), s("rect", { x: left, y, width: bar, height: 18, rx: 4, class: `chart-bar chart-bar-${tone}` }), s("text", { x: left + bar + 7, y: y + 14, class: "chart-value", text: item.count }));
  });
  return h("div", { class: "chart-wrap" }, svg);
}

function usageTable(rows) {
  const keys = ["provider", "day", "model", "role", "mode", "repository_id"].filter((key) => rows.some((row) => key in row));
  const columns = [...keys, "turns", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_estimate_usd"];
  const node = rows.length ? table(
    columns.map((column) => column === "repository_id" ? "Repository" : column.replaceAll("_", " ")),
    rows.map((row) => h("tr", {}, columns.map((column) => h("td", {
      "data-label": column.replaceAll("_", " "),
      text: column === "repository_id" ? row.repository_path || row.repository_id || "No repository" : column === "cost_estimate_usd" && !row.priced_turns ? "—" : row[column] ?? "—",
    })))),
    "responsive-table usage-table",
  ) : emptyState("No usage recorded", "No turns match this period and filter.");
  return { keys, node };
}

// How busy each provider's slots were, and how long queued work waited for one. A number of
// slot-seconds, not a provider quota: it says where a higher concurrency limit would be used.
export function slotRows(utilization = []) {
  return utilization.map((row) => ({
    provider: row.provider,
    limit: row.limit ?? "—",
    used: row.slot_utilization == null ? "—" : `${Math.round(row.slot_utilization * 100)}%`,
    held: row.holds ?? 0,
    peak: row.peak_active ?? 0,
    saturated: row.saturated_acquires ?? 0,
    queued: row.queued ?? 0,
    wait: row.queue_wait?.p50_s == null ? "—" : `${row.queue_wait.p50_s}s`,
  }));
}

// Failures by provider, mode and error code, most frequent first. Codes are identifiers the
// server extracted from each task's error; the messages never leave the database.
export function failureRows(failures = []) {
  return [...failures]
    .map((row) => ({ provider: row.provider, mode: row.mode, code: row.code || "UNKNOWN", count: numeric(row.count) || 0 }))
    .sort((a, b) => b.count - a.count || a.provider.localeCompare(b.provider) || a.code.localeCompare(b.code));
}

// The last `days` entries of the report's per-day series, with the check pass rate made explicit
// and left empty on a day no check ran (no checks is not a 0% pass rate).
export function dailyTrend(daily = [], days = 14) {
  return daily.slice(-days).map((row) => {
    const run = numeric(row.checks_run) || 0, passed = numeric(row.checks_passed) || 0;
    return {
      day: row.day,
      tasks: numeric(row.tasks_created) || 0,
      failures: numeric(row.failures) || 0,
      checks: run ? `${passed}/${run}` : "—",
      passRate: run ? passed / run : null,
      passLabel: run ? `${Math.round((passed / run) * 100)}%` : "—",
    };
  });
}

// One sentence on how much of the recorded work carries token usage, so a partial total says so.
export function meteringNote(metering = []) {
  const sum = (key) => metering.reduce((total, row) => total + (numeric(row[key]) || 0), 0);
  const total = sum("turns");
  if (!total) return null;
  const metered = sum("turns_with_usage"), unmetered = sum("unmetered_turns"), successful = sum("unmetered_successful_turns"), unpriced = sum("unpriced_turns");
  if (!unmetered && !unpriced) return `All ${total} turns carry token usage.`;
  const parts = [`${metered} of ${total} turns carry token usage`];
  if (unmetered) parts.push(`${unmetered} unmetered (${successful} ended normally)`);
  if (unpriced) parts.push(`${unpriced} metered but unpriced`);
  return `${parts.join("; ")}. Token and cost totals are partial.`;
}

// A window's current observation: its status, and the period's last reported percentage with
// the time it was reported when that was earlier than the newest observation; a window whose
// reset time has passed says so, since nothing has been observed about the period after it.
export function windowMark(window, now = Date.now()) {
  let text = `${window.window} ${window.status || "unknown"}`;
  if (window.used_percent != null) {
    text += ` ${Number(window.used_percent)}%`;
    if (window.used_percent_observed_at && window.used_percent_observed_at !== window.observed_at) text += ` (as of ${String(window.used_percent_observed_at).slice(0, 16).replace("T", " ")} UTC)`;
  }
  const resets = Date.parse(window.resets_at || "");
  if (Number.isFinite(resets) && resets <= now) text += " (reset passed)";
  return text;
}

function trendChart(rows) {
  if (!rows.length) return emptyState("No daily series", "Nothing was recorded for this period.");
  const width = 760, height = 210, left = 36, right = 44, top = 14, bottom = 34, plotW = width - left - right, plotH = height - top - bottom;
  const max = Math.max(...rows.map((row) => row.tasks), 1), step = plotW / rows.length, barW = Math.max(4, Math.min(28, step * 0.62));
  const svg = s("svg", { class: "usage-chart trend-chart", viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Tasks created per day, failed tasks per day, and the share of verification checks that passed" });
  const points = [];
  rows.forEach((row, index) => {
    const x = left + index * step + (step - barW) / 2, base = top + plotH;
    const tall = row.tasks ? Math.max(2, (row.tasks / max) * plotH) : 0, failed = row.failures ? Math.max(2, (row.failures / max) * plotH) : 0;
    svg.append(s("rect", { x, y: base - tall, width: barW, height: tall, rx: 2, class: "chart-bar" }), s("rect", { x, y: base - failed, width: barW, height: failed, rx: 2, class: "chart-bar chart-bar-danger" }));
    if (index % Math.ceil(rows.length / 7) === 0 || index === rows.length - 1) svg.append(s("text", { x: x + barW / 2, y: height - 12, "text-anchor": "middle", class: "chart-label", text: String(row.day).slice(5) }));
    if (row.passRate != null) points.push([x + barW / 2, top + plotH - row.passRate * plotH]);
  });
  if (points.length > 1) svg.append(s("polyline", { points: points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" "), class: "chart-line" }));
  points.forEach(([cx, cy]) => svg.append(s("circle", { cx: cx.toFixed(1), cy: cy.toFixed(1), r: 3, class: "chart-dot" })));
  svg.append(s("text", { x: 4, y: top + 10, class: "chart-label", text: String(max) }), s("text", { x: width - right + 6, y: top + 10, class: "chart-label", text: "100%" }), s("text", { x: width - right + 6, y: top + plotH, class: "chart-label", text: "0%" }));
  return h("div", { class: "chart-wrap" }, svg);
}

function trendPanel(data) {
  const rows = dailyTrend(data.daily);
  const columns = [["day", "Day"], ["tasks", "Tasks"], ["failures", "Failed"], ["checks", "Checks passed"], ["passLabel", "Pass rate"]];
  return h("section", { class: "panel" }, sectionHeading("Daily trend", "Tasks, failures and check pass rate (UTC days)"),
    h("p", { class: "panel-note", text: "Bars: tasks created (teal) and failed (red). Line: share of verification checks that passed, on days any ran." }),
    trendChart(rows),
    rows.length ? h("details", { class: "table-alternative", dataset: { persistKey: "usage-trend-table" } }, h("summary", { text: "Exact values" }), table(columns.map(([, label]) => label), rows.map((row) => h("tr", {}, columns.map(([key, label]) => h("td", { "data-label": label, text: row[key] })))), "responsive-table")) : null,
  );
}

function failurePanel(data) {
  const rows = failureRows(data.failures);
  return h("section", { class: "panel" }, sectionHeading("Failures", "Failed tasks by error code"),
    rows.length ? table(["Worker", "Mode", "Code", "Count"], rows.map((row) => h("tr", {}, h("td", { "data-label": "Worker", text: row.provider }), h("td", { "data-label": "Mode", text: row.mode }), h("td", { "data-label": "Code" }, badge(row.code)), h("td", { "data-label": "Count", text: row.count }))), "responsive-table") : emptyState("No failures", "No task failed in this period."));
}

function slotPanel(data) {
  const rows = slotRows(data.utilization);
  const fanout = (data.fanout || []).filter((row) => row.groups);
  const columns = [["provider", "Worker"], ["limit", "Slots"], ["used", "Slot time used"], ["held", "Turns"], ["peak", "Peak active"], ["saturated", "Took the last slot"], ["queued", "Queued now"], ["wait", "Median wait"]];
  return h("section", { class: "panel" }, sectionHeading("Slot use", "Scheduling on this host, not provider quota"),
    rows.length ? table(columns.map(([, label]) => label), rows.map((row) => h("tr", {}, columns.map(([key, label]) => h("td", { "data-label": label, text: row[key] })))), "responsive-table") : emptyState("No slot history", "No turn has held a slot in this period."),
    fanout.length ? h("p", { class: "panel-note", text: `Fan-out: ${fanout.map((row) => `${row.role || "no role"} ${row.groups} groups, mean width ${row.mean_width}`).join(" · ")}` }) : null,
  );
}

export async function renderUsage(route, { signal } = {}) {
  const params = new URLSearchParams({ since: route.query.get("since") || "7d", group_by: route.query.get("group_by") || "provider" });
  if (route.query.get("provider")) params.set("provider", route.query.get("provider"));
  const providerParams = new URLSearchParams(params); providerParams.set("group_by", "provider");
  const [primary, providerResult] = await Promise.all([
    getJSON(`/api/usage?${params}`, { signal, fresh: true }),
    params.get("group_by") === "provider" ? Promise.resolve(null) : getJSON(`/api/usage?${providerParams}`, { signal, fresh: true }),
  ]);
  const { data } = primary, stale = primary.stale || providerResult?.stale;
  const usage = data.usage || [], exact = usageTable(usage);
  const providerUsage = providerResult?.data?.usage || usage;
  const totals = usage.reduce((sum, row) => { const input = numeric(row.input_tokens), output = numeric(row.output_tokens), turns = numeric(row.turns), cost = numeric(row.cost_estimate_usd), priced = numeric(row.priced_turns); return { turns: sum.turns + (turns || 0), input: sum.input + (input || 0), output: sum.output + (output || 0), cost: sum.cost + (cost || 0), priced: sum.priced + (priced || 0) }; }, { turns: 0, input: 0, output: 0, cost: 0, priced: 0 });
  const metering = meteringNote(data.metering);
  return h("div", { class: "view usage-view", dataset: { stale: String(stale) } },
    h("div", { class: "page-heading" }, h("div", {}, h("span", { class: "eyebrow", text: "Observed consumption" }), h("h1", { text: "Usage" }), h("p", { text: "Explore recorded token volume, outcomes, timing, and provider window telemetry." })), stale ? badge("stale", "Cached data") : null),
    controls(route),
    h("section", { class: "metric-grid" }, metric("Turns", compact(totals.turns), "Recorded"), metric("Input", compact(totals.input), "Recorded aggregate"), metric("Output", compact(totals.output), "Recorded aggregate"), metric("Estimated cost", totals.priced ? `$${totals.cost.toFixed(2)}` : "Unavailable", totals.priced ? `${totals.priced}/${totals.turns} turns priced` : "No priced turns")),
    h("div", { class: "usage-grid chart-grid" },
      h("section", { class: "panel" }, sectionHeading("Provider distribution", "Observed tokens"), usageChart(providerUsage, ["provider"], "Token distribution by provider")),
      h("section", { class: "panel" }, sectionHeading("Outcome distribution", "Recorded task states"), outcomeChart(data.outcomes)),
    ),
    metering ? h("p", { class: "panel-note metering-note", text: `Metering: ${metering}` }) : null,
    trendPanel(data),
    slotPanel(data),
    h("section", { class: "panel" }, sectionHeading("Token detail", `Grouped by ${params.get("group_by")}`), usageChart(usage, exact.keys), h("details", { class: "table-alternative", open: true, dataset: { persistKey: "usage-table" } }, h("summary", { text: "Exact values" }), exact.node)),
    h("div", { class: "usage-grid" },
      h("section", { class: "panel" }, sectionHeading("Outcomes", "Task states"), data.outcomes?.length ? table(["Worker", "Mode", "State", "Count"], data.outcomes.map((row) => h("tr", {}, h("td", { text: row.provider }), h("td", { text: row.mode }), h("td", {}, badge(row.state)), h("td", { text: row.count })))) : emptyState("No outcomes", "Nothing was recorded for this period.")),
      failurePanel(data),
      h("section", { class: "panel" }, sectionHeading("Timing", "Turn and check duration"), h("div", { class: "timing-display" }, h("strong", { text: `${data.turns?.count ?? 0} turns` }), h("span", { text: `Mean ${data.turns?.mean_ms ?? "—"} ms · p50 ${data.turns?.p50_ms ?? "—"} ms · p95 ${data.turns?.p95_ms ?? "—"} ms` }), h("strong", { text: `${data.checks?.passed ?? 0}/${data.checks?.count ?? 0} checks passed` }))),
      h("section", { class: "panel" }, sectionHeading("Violations", "Policy observations"), data.violations?.length ? table(["Worker", "Kind", "Count"], data.violations.map((row) => h("tr", {}, h("td", { text: row.provider }), h("td", { text: row.kind }), h("td", { text: row.count })))) : emptyState("No violations", "No violations were recorded.")),
      h("section", { class: "panel" }, sectionHeading("Window telemetry", "Provider reports"), data.windows?.length ? h("ul", { class: "window-list" }, data.windows.map((item) => h("li", {}, h("div", {}, h("strong", { text: item.provider }), badge(item.state)), item.windows?.length ? h("p", { class: "window-marks", text: item.windows.map((window) => windowMark(window)).join(" · ") }) : null, h("p", { text: item.note })))) : emptyState("No window telemetry", "No provider window observations were recorded.")),
    ),
    h("p", { class: "cost-note", text: `Token totals are recorded aggregates; turns without provider token telemetry are omitted from token totals. ${data.cost_note || "Cost figures are estimates based on recorded usage and available pricing."}` }),
  );
}
