import { getJSON } from "../api.js";
import { badge, emptyState, formatDate, h, metric, relativeTime, sectionHeading } from "../dom.js";
import { routeHref } from "../router.js";

function taskItem(task, kind) {
  const repo = task.repository?.path || task.repository?.display_path || task.repository_path || task.repository_id || "No repository";
  return h("a", { class: "activity-item", href: routeHref("tasks", task.id) },
    h("span", { class: `activity-mark activity-${kind}`, "aria-hidden": "true" }),
    h("span", { class: "activity-main" }, h("strong", { text: task.summary || "Untitled task" }), h("small", { text: `${task.id} · ${repo}` })),
    badge(task.state),
    h("time", { datetime: task.updated_at, title: formatDate(task.updated_at), text: relativeTime(task.updated_at) }),
  );
}

function queuePanel(title, eyebrow, tasks, kind, truncated) {
  return h("section", { class: "panel queue-panel" }, sectionHeading(title, eyebrow, h("a", { class: "text-link", href: routeHref("tasks"), text: "View tasks →" })),
    tasks.length ? h("div", { class: "activity-list" }, tasks.map((task) => taskItem(task, kind))) : emptyState(kind === "active" ? "No active work" : "Nothing needs attention", kind === "active" ? "Queued and running tasks will appear here." : "Results and recovery issues will appear here."),
    truncated ? h("p", { class: "panel-note", text: "More tasks are available in the full task list." }) : null,
  );
}

function workerStrip(data) {
  const workers = data?.providers || [];
  return h("section", { class: "panel overview-workers" }, sectionHeading("Worker access", "Cached evidence", h("a", { class: "text-link", href: routeHref("workers"), text: "Inspect workers →" })),
    workers.length ? h("div", { class: "signal-grid" }, workers.map((worker) => h("a", { class: "signal-card", href: routeHref("workers") }, h("span", { text: worker.id }), badge(worker.availability?.state || "unknown"), h("small", { text: worker.availability?.stale ? "Stale observation" : worker.availability?.reason || "No current evidence" })))) : emptyState("No workers configured", "Configured worker profiles will appear here."),
  );
}

function subscriptionStrip(data) {
  const priority = (row) => row.end_passed_unverified ? 0 : row.upcoming_end_warning === "within_1_day" ? 1 : row.upcoming_end_warning === "within_7_days" ? 2 : 3;
  const rows = [...(data?.subscriptions || [])].sort((a, b) => priority(a) - priority(b));
  const status = (row) => row.end_passed_unverified ? "Verification needed · recorded end passed" : row.upcoming_end_warning === "within_1_day" ? "Confirmed access end within one day" : row.upcoming_end_warning === "within_7_days" ? "Confirmed access end within seven days" : row.error ? "Needs verification" : row.status || "Unknown";
  return h("section", { class: "panel overview-subscriptions" }, sectionHeading("Subscription verification", "Browser billing", h("a", { class: "text-link", href: routeHref("subscriptions"), text: "View subscriptions →" })),
    rows.length ? h("div", { class: "signal-grid" }, rows.map((row) => h("a", { class: "signal-card", href: routeHref("subscriptions", null, { provider: row.provider }) }, h("span", { text: row.label || row.provider }), badge(priority(row) < 3 || row.error ? "warning" : row.status || "unknown", status(row)), h("small", { text: row.last_success_at ? `Browser billing verified ${relativeTime(row.last_success_at)}` : "Browser billing not yet verified" }), row.access_ends_at ? h("small", { text: `Access end ${formatDate(row.access_ends_at, row.date_precision === "date")}` }) : null))) : emptyState("No subscription sources", "Subscription checks will appear here."),
  );
}

export async function renderOverview(_route, { signal } = {}) {
  const [overview, providers, subscriptions, health] = await Promise.all([
    getJSON("/api/overview", { signal, fresh: true }),
    getJSON("/api/providers", { signal, fresh: true }).catch(() => ({ data: null, stale: true })),
    getJSON("/api/subscriptions", { signal, fresh: true }).catch(() => ({ data: null, stale: true })),
    getJSON("/api/health", { signal, fresh: true }).catch(() => ({ data: null, stale: true })),
  ]);
  const data = overview.data;
  const counts = data.counts || {};
  const stale = overview.stale || providers.stale || subscriptions.stale || health.stale;
  return h("div", { class: "view overview-view", dataset: { stale: String(stale) } },
    h("div", { class: "page-heading overview-heading" }, h("div", {}, h("span", { class: "eyebrow", text: "Local service" }), h("h1", { text: "Overview" }), h("p", { text: "The current execution picture, with the work that needs a decision brought forward." })), stale ? badge("stale", "Some cached data") : null),
    h("section", { class: "metric-grid", "aria-label": "Task counts" }, metric("All tasks", counts.total ?? 0, "Recorded"), metric("Active", counts.active ?? 0, "In flight", counts.active ? "positive" : ""), metric("Attention", counts.attention ?? 0, "Needs action", counts.attention ? "warning" : ""), metric("Awaiting review", counts.awaiting_review ?? 0, "Result ready", counts.awaiting_review ? "warning" : "")),
    h("div", { class: "overview-columns" }, queuePanel("Active work", "Now", data.active_tasks || [], "active", data.truncated?.active), queuePanel("Needs attention", "Decision queue", data.attention_tasks || [], "attention", data.truncated?.attention)),
    workerStrip(providers.data), subscriptionStrip(subscriptions.data),
    health.data ? h("footer", { class: "service-health" }, h("span", { text: `TaskSpindle ${health.data.version || ""}` }), h("span", { text: health.data.task_database_read_only ? "Task data: read-only" : "Task data: writable" }), h("span", { text: health.data.subscription_actions_enabled ? "Manual subscription checks: available" : "Manual subscription checks: unavailable" })) : null,
  );
}
