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

export async function renderOverview(_route, { signal } = {}) {
  const [overview, providers, health] = await Promise.all([
    getJSON("/api/overview", { signal, fresh: true }),
    getJSON("/api/providers", { signal, fresh: true }).catch(() => ({ data: null, stale: true })),
    getJSON("/api/health", { signal, fresh: true }).catch(() => ({ data: null, stale: true })),
  ]);
  const data = overview.data;
  const counts = data.counts || {};
  const stale = overview.stale || providers.stale || health.stale;
  return h("div", { class: "view overview-view", dataset: { stale: String(stale) } },
    h("div", { class: "page-heading overview-heading" }, h("div", {}, h("span", { class: "eyebrow", text: "Local service" }), h("h1", { text: "Overview" }), h("p", { text: "The current execution picture, with the work that needs a decision brought forward." })), stale ? badge("stale", "Some cached data") : null),
    h("section", { class: "metric-grid", "aria-label": "Task counts" }, metric("All tasks", counts.total ?? 0, "Recorded"), metric("Active", counts.active ?? 0, "In flight", counts.active ? "positive" : ""), metric("Attention", counts.attention ?? 0, "Needs action", counts.attention ? "warning" : ""), metric("Awaiting review", counts.awaiting_review ?? 0, "Result ready", counts.awaiting_review ? "warning" : "")),
    h("div", { class: "overview-columns" }, queuePanel("Active work", "Now", data.active_tasks || [], "active", data.truncated?.active), queuePanel("Needs attention", "Decision queue", data.attention_tasks || [], "attention", data.truncated?.attention)),
    workerStrip(providers.data),
    health.data ? h("footer", { class: "service-health" }, h("span", { text: `TaskSpindle ${health.data.version || ""}` }), h("span", { text: health.data.task_database_read_only ? "Task data: read-only" : "Task data: writable" })) : null,
  );
}
