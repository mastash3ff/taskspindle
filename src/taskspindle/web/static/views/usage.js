import { getJSON } from "../api.js";
import { badge, emptyState, h, metric, s, sectionHeading, table } from "../dom.js";
import { updateRouteQuery } from "../router.js";

const GROUPS = ["provider", "day", "provider_day", "model", "mode", "repository_id"];
const GROUP_LABELS = { provider: "Provider", day: "Day", provider_day: "Provider and day", model: "Model", mode: "Mode", repository_id: "Repository" };
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
  const keys = ["provider", "day", "model", "mode", "repository_id"].filter((key) => rows.some((row) => key in row));
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
  return h("div", { class: "view usage-view", dataset: { stale: String(stale) } },
    h("div", { class: "page-heading" }, h("div", {}, h("span", { class: "eyebrow", text: "Observed consumption" }), h("h1", { text: "Usage" }), h("p", { text: "Explore recorded token volume, outcomes, timing, and provider window telemetry." })), stale ? badge("stale", "Cached data") : null),
    controls(route),
    h("section", { class: "metric-grid" }, metric("Turns", compact(totals.turns), "Recorded"), metric("Input", compact(totals.input), "Recorded aggregate"), metric("Output", compact(totals.output), "Recorded aggregate"), metric("Estimated cost", totals.priced ? `$${totals.cost.toFixed(2)}` : "Unavailable", totals.priced ? `${totals.priced}/${totals.turns} turns priced` : "No priced turns")),
    h("div", { class: "usage-grid chart-grid" },
      h("section", { class: "panel" }, sectionHeading("Provider distribution", "Observed tokens"), usageChart(providerUsage, ["provider"], "Token distribution by provider")),
      h("section", { class: "panel" }, sectionHeading("Outcome distribution", "Recorded task states"), outcomeChart(data.outcomes)),
    ),
    h("section", { class: "panel" }, sectionHeading("Token detail", `Grouped by ${params.get("group_by")}`), usageChart(usage, exact.keys), h("details", { class: "table-alternative", open: true, dataset: { persistKey: "usage-table" } }, h("summary", { text: "Exact values" }), exact.node)),
    h("div", { class: "usage-grid" },
      h("section", { class: "panel" }, sectionHeading("Outcomes", "Task states"), data.outcomes?.length ? table(["Worker", "Mode", "State", "Count"], data.outcomes.map((row) => h("tr", {}, h("td", { text: row.provider }), h("td", { text: row.mode }), h("td", {}, badge(row.state)), h("td", { text: row.count })))) : emptyState("No outcomes", "Nothing was recorded for this period.")),
      h("section", { class: "panel" }, sectionHeading("Timing", "Turn and check duration"), h("div", { class: "timing-display" }, h("strong", { text: `${data.turns?.count ?? 0} turns` }), h("span", { text: `Mean ${data.turns?.mean_ms ?? "—"} ms · p50 ${data.turns?.p50_ms ?? "—"} ms` }), h("strong", { text: `${data.checks?.passed ?? 0}/${data.checks?.count ?? 0} checks passed` }))),
      h("section", { class: "panel" }, sectionHeading("Violations", "Policy observations"), data.violations?.length ? table(["Worker", "Kind", "Count"], data.violations.map((row) => h("tr", {}, h("td", { text: row.provider }), h("td", { text: row.kind }), h("td", { text: row.count })))) : emptyState("No violations", "No violations were recorded.")),
      h("section", { class: "panel" }, sectionHeading("Window telemetry", "Provider reports"), data.windows?.length ? h("ul", { class: "window-list" }, data.windows.map((item) => h("li", {}, h("div", {}, h("strong", { text: item.provider }), badge(item.state)), h("p", { text: item.note })))) : emptyState("No window telemetry", "No provider window observations were recorded.")),
    ),
    h("p", { class: "cost-note", text: `Token totals are recorded aggregates; turns without provider token telemetry contribute zero. ${data.cost_note || "Cost figures are estimates based on recorded usage and available pricing."}` }),
  );
}
