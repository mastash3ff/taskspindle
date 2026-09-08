import { getJSON } from "../api.js";
import { badge, emptyState, formatDate, h, labeledValue, relativeTime, safeJSON, sectionHeading, table } from "../dom.js";
import { routeHref, updateRouteQuery } from "../router.js";

const STATES = ["PREPARING", "QUEUED", "RUNNING", "REPAIRING", "RESUMING", "ACCEPTING", "CANCELLING", "RESULT_READY", "COMPLETED", "ACCEPTED", "FAILED", "REJECTED", "CANCELLED", "INTERRUPTED", "RECOVERY_AMBIGUOUS"];
const MODES = ["consult", "review", "implement"];
const DETAIL_TABS = ["changes", "result", "turns", "events", "metadata", "log"];
const DIFF_CACHE_LIMIT = 12;
const diffCache = new Map();
const val = (value) => value == null || value === "" ? "—" : value;
const shortSha = (value) => value ? String(value).slice(0, 10) : "—";
let lastDeepLink = null;

function modelFor(task) {
  return task.reported_model || task.resolved_model || task.requested_model || "Model not recorded";
}

function repositoryFor(task, repository = null) {
  return repository?.path || repository?.display_path || task.repository?.path || task.repository_path || task.repository_id || "No repository";
}

function filterControl(label, name, value, choices = null, placeholder = "") {
  const input = choices ? h("select", { name, "aria-label": label, dataset: { focusKey: `filter-${name}` } },
    h("option", { value: "", text: `All ${label.toLowerCase()}` }),
    choices.map((choice) => h("option", { value: choice, text: choice })),
  ) : h("input", { name, value, type: name === "q" ? "search" : "text", placeholder, "aria-label": label, dataset: { focusKey: `filter-${name}` } });
  input.value = value || "";
  return h("label", { class: "filter-field" }, h("span", { text: label }), input);
}

function taskRows(tasks) {
  return tasks.map((task) => h("tr", { dataset: { taskId: task.id } },
    h("td", { "data-label": "Task", class: "task-summary-cell" },
      h("a", { class: "task-link", href: routeHref("tasks", task.id), text: task.summary || task.prompt || "Untitled task" }),
      h("small", { class: "mono", text: task.id }),
    ),
    h("td", { "data-label": "State" }, badge(task.state)),
    h("td", { "data-label": "Worker", class: "task-worker-cell" },
      h("strong", { text: val(task.provider) }),
      h("small", { text: modelFor(task) }),
    ),
    h("td", { "data-label": "Repository", class: "mono", text: repositoryFor(task) }),
    h("td", { "data-label": "Mode", text: val(task.mode) }),
    h("td", { "data-label": "Updated" }, h("time", { datetime: task.updated_at, title: formatDate(task.updated_at), text: relativeTime(task.updated_at) })),
  ));
}

export async function renderTasks(route, { signal } = {}) {
  if (route.id) return renderTaskDetail(route.id, { signal, route });
  const params = new URLSearchParams();
  for (const key of ["state", "provider", "mode", "q"]) if (route.query.get(key)) params.set(key, route.query.get(key));
  params.set("limit", "200");
  const { data, stale } = await getJSON(`/api/tasks?${params}`, { signal, fresh: true });
  const tasks = data.tasks || [];
  const form = h("form", { class: "filter-bar", role: "search" },
    filterControl("Search", "q", route.query.get("q"), null, "ID or prompt"),
    filterControl("State", "state", route.query.get("state"), STATES),
    filterControl("Mode", "mode", route.query.get("mode"), MODES),
    filterControl("Worker", "provider", route.query.get("provider"), null, "Provider"),
    h("button", { class: "button button-secondary", type: "submit", text: "Apply" }),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    updateRouteQuery(Object.fromEntries(new FormData(form)));
  });
  const reset = h("button", { class: "button button-quiet", type: "button", text: "Clear filters", onclick: () => updateRouteQuery({ q: null, state: null, mode: null, provider: null }) });
  return h("div", { class: "view tasks-view", dataset: { stale: String(stale) } },
    h("div", { class: "page-heading" }, h("div", {}, h("span", { class: "eyebrow", text: "Execution ledger" }), h("h1", { text: "Tasks" }), h("p", { text: "Scan the work, its owner, model, repository, and current decision state." })), stale ? badge("stale", "Cached data") : null),
    form,
    h("section", { class: "panel" }, sectionHeading(`${tasks.length} task${tasks.length === 1 ? "" : "s"}`, "Current filters", reset),
      tasks.length ? table(["Task", "State", "Worker & model", "Repository", "Mode", "Updated"], taskRows(tasks), "responsive-table task-table") : emptyState("No matching tasks", "Change the filters or search term to widen the list."),
    ),
  );
}

function definitionList(items, className = "") {
  return h("dl", { class: `detail-grid ${className}`.trim() }, items.map(([label, value, mono]) => labeledValue(label, val(value), { mono })));
}

function latestResult(turns) {
  return [...(turns || [])].reverse().find((item) => item.response || item.transcript?.text) || null;
}

function resultText(turn) {
  return turn?.response || turn?.transcript?.text || "";
}

function decisionPanel(task, repository, turns, checks) {
  const turn = latestResult(turns);
  const text = resultText(turn);
  const preview = text.length > 420 ? `${text.slice(0, 420).trimEnd()}…` : text;
  const passed = (checks || []).filter((check) => check.ok).length;
  const warnings = task.warnings || [];
  return h("section", { class: "panel task-decision", dataset: { taskSection: "overview" } },
    h("div", { class: "task-status-line" }, badge(task.state), task.cleanup_state ? badge(task.cleanup_state, `Cleanup ${task.cleanup_state}`) : null, badge(task.mode)),
    h("h2", { text: task.summary || task.prompt || "Untitled task" }),
    h("p", { class: "panel-note", text: `${task.provider || "Unassigned"} · ${modelFor(task)} · ${repositoryFor(task, repository)} · updated ${relativeTime(task.updated_at)}` }),
    definitionList([
      ["Result", preview || "No worker result yet"],
      ["Checks", checks?.length ? `${passed}/${checks.length} passed` : "No checks reported"],
      ["Warnings", warnings.length ? `${warnings.length} recorded` : "None"],
      ["Candidate", task.candidate_sha ? `${shortSha(task.candidate_sha)} · revision ${task.candidate_revision}` : "None"],
    ], "compact decision-grid"),
    task.error ? h("div", { class: "callout callout-danger" }, h("strong", { text: task.error.code || "Task error" }), h("p", { text: task.error.message || "No error detail was recorded." }), h("small", { text: task.error.retryable ? "Retryable" : "Manual review required" })) : null,
  );
}

function checksPanel(checks, warnings) {
  return h("section", { class: "panel task-checks", dataset: { taskSection: "checks" } }, sectionHeading("Checks & warnings", "Decision readiness", warnings?.length ? badge("warning", `${warnings.length} warning${warnings.length === 1 ? "" : "s"}`) : badge("ok", "No warnings")),
    checks?.length ? table(
      ["Command", "Result", "Exit", "Duration"],
      checks.map((check) => h("tr", {},
        h("td", { "data-label": "Command", class: "mono", text: check.command }),
        h("td", { "data-label": "Result" }, badge(check.ok ? "ok" : "failed", check.ok ? "Passed" : "Failed")),
        h("td", { "data-label": "Exit", text: val(check.exit_code) }),
        h("td", { "data-label": "Duration", text: check.duration_ms == null ? "—" : `${check.duration_ms} ms` }),
      )),
      "responsive-table",
    ) : h("p", { class: "panel-note", text: "No checks were reported." }),
    warnings?.length ? h("ul", { class: "warning-list" }, warnings.map((warning) => h("li", { text: warning }))) : null,
  );
}

function detailHref(task, route, tab, extras = {}) {
  const query = new URLSearchParams(route.query);
  query.set("tab", tab);
  query.delete("file");
  query.delete("finding");
  query.delete("candidate");
  for (const [key, value] of Object.entries(extras)) if (value != null && value !== "") query.set(key, value);
  return routeHref("tasks", task.id, query);
}

function tabBar(task, route, active, hasChangesWorkspace) {
  const tabs = hasChangesWorkspace ? DETAIL_TABS : DETAIL_TABS.filter((tab) => tab !== "changes");
  const labels = { changes: "Changes & Review", result: "Result", turns: "Turns", events: "Events", metadata: "Metadata", log: "Log" };
  const nav = h("nav", { class: "task-tabs", role: "tablist", "aria-label": "Task detail sections" }, tabs.map((tab) => h("a", {
    id: `task-tab-${tab}`,
    class: `task-tab ${active === tab ? "active" : ""}`,
    href: detailHref(task, route, tab),
    role: "tab",
    "aria-selected": String(active === tab),
    "aria-controls": `task-panel-${tab}`,
    tabindex: active === tab ? "0" : "-1",
    dataset: { focusKey: `task-tab-${tab}` },
    text: labels[tab],
  })));
  nav.addEventListener("keydown", (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const links = [...nav.querySelectorAll('[role="tab"]')];
    const current = links.indexOf(event.target);
    if (current < 0) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? links.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + links.length) % links.length;
    links[next].focus();
    links[next].click();
  });
  return nav;
}

function diffAnchor(path, line) {
  return `diff-${encodeURIComponent(path || "").replace(/'/g, "%27")}-${String(line || "file")}`;
}

function findingAnchor(id) {
  return `finding-${encodeURIComponent(id || "unknown").replace(/'/g, "%27")}`;
}

function diffDocument(text) {
  const pre = h("pre", { class: "candidate-diff", tabindex: "0", "aria-label": "Candidate diff", dataset: { scrollKey: "candidate-diff" } });
  const files = [];
  let path = "", pendingPath = "", line = null;
  for (const value of text.split("\n")) {
    const node = h("span", { class: `diff-line ${value.startsWith("+") && !value.startsWith("+++") ? "diff-add" : value.startsWith("-") && !value.startsWith("---") ? "diff-remove" : ""}`, tabindex: "-1", text: `${value}\n` });
    if (value.startsWith("diff --git ")) {
      const match = /^diff --git a\/(.+) b\/(.+)$/.exec(value);
      pendingPath = match?.[2] || "";
      path = pendingPath;
      line = null;
      if (path && !files.includes(path)) files.push(path);
      if (path) node.id = diffAnchor(path, null);
    } else if (value.startsWith("+++ b/")) {
      path = value.slice(6);
      pendingPath = path;
      if (!files.includes(path)) files.push(path);
      if (!pre.querySelector(`#${CSS.escape(diffAnchor(path, null))}`)) node.id = diffAnchor(path, null);
      line = null;
    } else if (value === "+++ /dev/null") {
      path = pendingPath;
      line = null;
    } else {
      const match = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(value);
      if (match) line = Number(match[1]);
      else if (path && line !== null && /^[ +]/.test(value)) {
        node.id = diffAnchor(path, line);
        line += 1;
      }
    }
    pre.append(node);
  }
  return { node: pre, files };
}

function scheduleReveal(node, key) {
  if (!node || lastDeepLink === key) return;
  lastDeepLink = key;
  const reveal = () => {
    if (!node.isConnected) return;
    node.scrollIntoView({ block: "center" });
    node.focus?.({ preventScroll: true });
  };
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => requestAnimationFrame(reveal));
  else setTimeout(reveal, 0);
}

function rememberDiff(key, text) {
  diffCache.delete(key);
  diffCache.set(key, text);
  while (diffCache.size > DIFF_CACHE_LIMIT) diffCache.delete(diffCache.keys().next().value);
}

async function candidatePanel(task, review, signal, route) {
  const verdictBadge = badge("unknown", "Review unchecked");
  const panel = h("section", { id: "task-panel-changes", class: "panel candidate-panel task-tab-panel", role: "tabpanel", "aria-labelledby": "task-tab-changes", dataset: { taskSection: "candidate", candidate: [task.id, task.candidate_sha, task.candidate_revision].join(":") } }, sectionHeading("Changes & Review", "Primary workspace", verdictBadge));
  if ((!task.candidate_sha || !Number.isInteger(task.candidate_revision) || task.candidate_revision < 1) && review) {
    verdictBadge.replaceWith(badge(review.verdict || "pending", review.verdict || "Not reviewed"));
    panel.append(
      h("div", { class: "review-summary" }, definitionList([["Verdict", review.verdict], ["Reviewer", review.provider], ["Candidate", review.candidate_sha, true]], "compact"), review.summary ? h("p", { text: review.summary }) : null),
      h("p", { class: "panel-note" }, "This review belongs to ", h("a", { href: routeHref("tasks", review.subject_task_id, { tab: "changes", candidate: review.candidate_sha }), class: "mono", text: review.subject_task_id }), ". Finding locations open that candidate with its recorded SHA guard."),
    );
    if (review.findings?.length) panel.append(table(["Severity", "Location", "Evidence", "Remedy"], review.findings.map((finding) => {
      const href = routeHref("tasks", review.subject_task_id, { tab: "changes", candidate: review.candidate_sha, file: finding.path, finding: finding.id });
      return h("tr", { id: findingAnchor(finding.id), dataset: { findingId: finding.id || "" } }, h("td", { "data-label": "Severity" }, badge(finding.severity)), h("td", { "data-label": "Location" }, h("a", { href, text: `${finding.path}:${finding.line || "file"}` })), h("td", { "data-label": "Evidence", text: val(finding.evidence) }), h("td", { "data-label": "Remedy", text: val(finding.remedy) }));
    }), "responsive-table review-findings"));
    return panel;
  }
  if (!task.candidate_sha || !Number.isInteger(task.candidate_revision) || task.candidate_revision < 1) {
    panel.append(h("div", { class: "callout callout-warning", text: "This candidate has no stable SHA and revision pair, so its diff cannot be compared safely." }));
    return panel;
  }
  const expectedCandidate = route.query.get("candidate");
  if (expectedCandidate && expectedCandidate !== task.candidate_sha) {
    panel.append(h("div", { class: "callout callout-warning", text: "This finding belongs to an older candidate. Reload the current task before comparing changes and review." }));
    return panel;
  }
  const query = new URLSearchParams({ revision: task.candidate_revision, candidate_sha: task.candidate_sha });
  const cacheKey = `${task.id}:${task.candidate_revision}:${task.candidate_sha}`;
  let diffNode = null;
  let files = [];
  let candidateChanged = false;
  const appendDiff = (text, cached = false) => {
    const parsed = diffDocument(text);
    diffNode = parsed.node;
    files = parsed.files;
    const selectedFile = route.query.get("file");
    if (cached) panel.append(h("div", { class: "callout callout-warning", text: "The diff could not be refreshed. Showing the last verified copy for this exact candidate." }));
    panel.append(
      h("p", { class: "panel-note", text: `${text ? text.split("\n").length : 0} lines · ${files.length} file${files.length === 1 ? "" : "s"} · revision ${task.candidate_revision} · ${shortSha(task.candidate_sha)}${cached ? " · cached" : ""}` }),
      files.length ? h("nav", { class: "candidate-files", "aria-label": "Changed files" }, files.map((filePath) => h("a", {
        class: `candidate-file-link ${selectedFile === filePath ? "active" : ""}`,
        href: detailHref(task, route, "changes", { candidate: task.candidate_sha, file: filePath }),
        text: filePath,
        onclick: () => diffNode.querySelector(`#${CSS.escape(diffAnchor(filePath, null))}`)?.scrollIntoView({ block: "start" }),
      }))) : null,
      diffNode,
    );
  };
  try {
    const response = await fetch(`/api/tasks/${encodeURIComponent(task.id)}/diff?${query}`, { signal, headers: { Accept: "text/plain" }, cache: "no-store" });
    if (response.status === 409) {
      diffCache.delete(cacheKey);
      candidateChanged = true;
      panel.append(h("div", { class: "callout callout-warning", text: "The candidate changed. Reload this task before comparing its diff and review." }));
    } else if (response.status === 404) {
      diffCache.delete(cacheKey);
      verdictBadge.replaceWith(badge(review?.verdict || "pending", review?.verdict || "Not reviewed"));
      panel.append(emptyState("No candidate diff", "This task has no diff to inspect."));
    }
    else if (!response.ok) throw new Error(`Diff request failed (${response.status})`);
    else {
      const text = await response.text();
      rememberDiff(cacheKey, text);
      verdictBadge.replaceWith(badge(review?.verdict || "pending", review?.verdict || "Not reviewed"));
      appendDiff(text);
    }
  } catch (error) {
    if (error.name !== "AbortError") {
      const retained = diffCache.get(cacheKey);
      if (retained !== undefined) {
        verdictBadge.replaceWith(badge(review?.verdict || "pending", review?.verdict || "Not reviewed"));
        appendDiff(retained, true);
      } else panel.append(h("div", { class: "callout callout-danger", text: error.message }));
    }
  }
  if (candidateChanged) return panel;
  let selectedFindingTarget = null;
  if (review) {
    panel.append(h("div", { class: "review-summary" }, definitionList([["Verdict", review.verdict], ["Reviewer", review.provider], ["Candidate", review.candidate_sha, true]], "compact"), review.summary ? h("p", { text: review.summary }) : null));
    if (review.findings?.length) panel.append(table(["Severity", "Location", "Evidence", "Remedy"], review.findings.map((finding) => {
      const anchor = diffNode?.querySelector(`#${CSS.escape(diffAnchor(finding.path, finding.line))}`);
      const href = detailHref(task, route, "changes", { candidate: task.candidate_sha, file: finding.path, finding: finding.id });
      const location = anchor ? h("a", { href, text: `${finding.path}:${finding.line || "file"}`, onclick: () => anchor.scrollIntoView({ block: "center" }) }) : `${finding.path || "—"}:${finding.line || "file"}`;
      const selected = route.query.get("finding") === finding.id;
      const row = h("tr", { id: findingAnchor(finding.id), class: selected ? "selected-finding" : "", dataset: { findingId: finding.id || "" } }, h("td", { "data-label": "Severity" }, badge(finding.severity)), h("td", { "data-label": "Location" }, location), h("td", { "data-label": "Evidence", text: val(finding.evidence) }), h("td", { "data-label": "Remedy", text: val(finding.remedy) }));
      if (selected) selectedFindingTarget = anchor || row;
      return row;
    }), "responsive-table review-findings"));
  } else panel.append(h("p", { class: "panel-note", text: "No review is recorded for this candidate." }));

  const selectedFile = route.query.get("file");
  if (selectedFindingTarget) scheduleReveal(selectedFindingTarget, `${task.id}:${task.candidate_sha}:finding:${route.query.get("finding")}`);
  else if (selectedFile && files.includes(selectedFile)) scheduleReveal(diffNode?.querySelector(`#${CSS.escape(diffAnchor(selectedFile, null))}`), `${task.id}:${task.candidate_sha}:file:${selectedFile}`);
  else lastDeepLink = null;
  return panel;
}

function resultPanel(turns) {
  const turn = latestResult(turns);
  return h("section", { id: "task-panel-result", class: "panel result-panel task-tab-panel", role: "tabpanel", "aria-labelledby": "task-tab-result", dataset: { taskSection: "result" } }, sectionHeading("Worker result", "Decision input"),
    turn ? h("div", { class: "prose-output" }, h("pre", { text: resultText(turn) })) : emptyState("No worker result yet", "The latest result will appear here when the worker reports back."),
  );
}

function turnsPanel(turns) {
  return h("section", { id: "task-panel-turns", class: "panel task-tab-panel", role: "tabpanel", "aria-labelledby": "task-tab-turns", dataset: { taskSection: "turns" } }, sectionHeading(`Turns (${turns?.length || 0})`, "Responses, usage, and transcripts"),
    turns?.length ? h("div", { class: "turn-list" }, turns.map((turn) => h("article", { class: "turn-card" },
      h("h3", { text: `Revision ${turn.revision} · ${turn.kind}` }),
      definitionList([["Started", formatDate(turn.started_at)], ["Ended", formatDate(turn.ended_at)], ["Stop reason", turn.stop_reason], ["Session", turn.session_id, true]], "compact"),
      turn.usage ? definitionList([["Model", turn.usage.model], ["Input tokens", turn.usage.input_tokens], ["Output tokens", turn.usage.output_tokens], ["Cache read", turn.usage.cache_read_tokens], ["Cache write", turn.usage.cache_write_tokens], ["Duration", turn.usage.duration_ms == null ? null : `${turn.usage.duration_ms} ms`]], "compact") : null,
      turn.response ? h("pre", { text: turn.response }) : null,
      turn.transcript ? h("pre", { text: safeJSON(turn.transcript) }) : null,
    ))) : emptyState("No turns", "No worker turns were recorded."),
  );
}

function eventsPanel(events) {
  return h("section", { id: "task-panel-events", class: "panel task-tab-panel", role: "tabpanel", "aria-labelledby": "task-tab-events", dataset: { taskSection: "events" } }, sectionHeading(`Events (${events?.length || 0})`, "State transition history"),
    events?.length ? table(["At", "Kind", "Payload"], events.map((event) => h("tr", {}, h("td", { "data-label": "At", text: formatDate(event.at) }), h("td", { "data-label": "Kind", text: event.kind }), h("td", { "data-label": "Payload", class: "mono", text: event.payload ? safeJSON(event.payload) : "—" }))), "responsive-table") : emptyState("No events", "No events were recorded."),
  );
}

function metadataPanel(task, repository) {
  return h("section", { id: "task-panel-metadata", class: "panel task-summary task-tab-panel", role: "tabpanel", "aria-labelledby": "task-tab-metadata", dataset: { taskSection: "metadata" } }, sectionHeading("Metadata", "Task provenance"),
    definitionList([
      ["Worker", task.provider], ["Provider family", task.provider_family], ["Mode", task.mode], ["Auth", task.auth_mode], ["Repository", repositoryFor(task, repository)],
      ["Branch", task.branch, true], ["Worktree", task.worktree_path, true], ["Requested model", task.requested_model], ["Resolved model", task.resolved_model], ["Reported model", task.reported_model],
      ["Requested effort", task.requested_effort], ["Resolved effort", task.resolved_effort], ["Created", formatDate(task.created_at)], ["Updated", formatDate(task.updated_at)], ["Started", formatDate(task.started_at)], ["Finished", formatDate(task.finished_at)],
      ["State version", task.state_version], ["Candidate revision", task.candidate_revision], ["Candidate SHA", task.candidate_sha, true], ["Session", task.session_id, true], ["Unit", task.unit_name, true], ["Heartbeat", formatDate(task.heartbeat_at)],
    ]),
  );
}

function logPanel(workerLog) {
  return h("section", { id: "task-panel-log", class: "panel task-tab-panel", role: "tabpanel", "aria-labelledby": "task-tab-log", dataset: { taskSection: "log" } }, sectionHeading("Worker log", "Tail output"),
    workerLog ? h("pre", { class: "worker-log", tabindex: "0", dataset: { scrollKey: "worker-log" }, text: workerLog }) : emptyState("No worker log", "No log tail is available."),
  );
}

export async function renderTaskDetail(id, { signal, route } = {}) {
  const activeRoute = route || { id, query: new URLSearchParams() };
  const { data, stale } = await getJSON(`/api/tasks/${encodeURIComponent(id)}`, { signal, fresh: true });
  const task = data.task;
  const hasCandidate = Boolean(task.candidate_sha || task.candidate_revision || task.diff_size);
  const hasChangesWorkspace = hasCandidate || Boolean(data.review);
  const requestedTab = activeRoute.query.get("tab");
  const activeTab = DETAIL_TABS.includes(requestedTab) && (requestedTab !== "changes" || hasChangesWorkspace) ? requestedTab : hasChangesWorkspace ? "changes" : "result";
  let content;
  if (activeTab === "changes") content = await candidatePanel(task, data.review, signal, activeRoute);
  else if (activeTab === "turns") content = turnsPanel(data.turns || []);
  else if (activeTab === "events") content = eventsPanel(data.events || []);
  else if (activeTab === "metadata") content = metadataPanel(task, data.repository);
  else if (activeTab === "log") content = logPanel(data.worker_log);
  else content = resultPanel(data.turns || []);
  return h("div", { class: "view task-detail-view", dataset: { stale: String(stale) } },
    h("a", { class: "back-link", href: routeHref("tasks"), text: "← All tasks" }),
    h("div", { class: "page-heading task-detail-heading" }, h("div", {}, h("span", { class: "eyebrow", text: "Task record" }), h("h1", {}, "Task ", h("span", { class: "mono", text: task.id })), h("p", { text: `${task.provider || "Unassigned"} · ${task.mode || "Unknown mode"}` })), stale ? badge("stale", "Cached data") : null),
    decisionPanel(task, data.repository, data.turns || [], data.checks || []),
    checksPanel(data.checks || [], task.warnings || []),
    tabBar(task, activeRoute, activeTab, hasChangesWorkspace),
    content,
  );
}
