import { putJSON } from "./api.js";
import { badge, h, sectionHeading } from "./dom.js";

const HOSTS = ["windows", "wsl"];
const MODE_LABELS = { ensemble: "Ensemble", native: "Native" };
const MODE_HELP = "Ensemble is TaskSpindle-first with native fallback; Native turns external integration off and retains Codex subagents.";
const HOST_LABELS = { both: "Both", windows: "Windows", wsl: "WSL" };
const STATUS_TONE = {
  configured: "positive",
  needs_repair: "warning",
  unavailable: "danger",
  update_failed: "danger",
  mixed: "warning",
};
const STATUS_LABELS = {
  configured: "configured",
  needs_repair: "needs repair",
  unavailable: "unavailable",
  update_failed: "update failed",
  mixed: "mixed",
};

let currentData = null;
let ui = null; // { mode, hostScope, applying, results } independent of the dispatch-policy draft
let mutationEpoch = 0;

function selectedHosts(hostScope) {
  return hostScope === "both" ? [...HOSTS] : [hostScope];
}

function rowsFor(data, hostScope) {
  const hosts = data?.hosts || [];
  const wanted = selectedHosts(hostScope);
  return wanted.map((id) => hosts.find((row) => row.host === id) || { host: id, mode: null, status: "unavailable", revision: null, checks: [] });
}

function aggregateStatus(rows) {
  const values = [...new Set(rows.map((row) => row.status || "unavailable"))];
  return values.length === 1 ? values[0] : "mixed";
}

function aggregateMode(rows) {
  const values = [...new Set(rows.map((row) => row.mode))];
  return values.length === 1 ? values[0] : "mixed";
}

function inferMode(data) {
  const mode = aggregateMode(data?.hosts || []);
  return mode === "native" || mode === "ensemble" ? mode : "ensemble";
}

function expectedRevisions(rows) {
  const out = {};
  for (const row of rows) out[row.host] = row.revision == null ? "" : String(row.revision);
  return out;
}

function canApply(rows) {
  return rows.length > 0 && rows.every((row) => typeof row.revision === "string" && row.revision) && rows.some((row) => row.status !== "unavailable");
}

function syncState(data) {
  currentData = data;
  if (!ui) ui = { mode: inferMode(data), hostScope: "both", applying: false, results: null };
}

export function resetAiPolicyUi() {
  currentData = null;
  ui = null;
  mutationEpoch = 0;
}

export function isAiPolicyApplying() {
  return Boolean(ui?.applying);
}

export function aiPolicyEpoch() {
  return mutationEpoch;
}

function mergeHosts(existing, incoming) {
  const byHost = new Map();
  for (const row of existing || []) {
    if (row && row.host) byHost.set(row.host, row);
  }
  for (const row of incoming || []) {
    if (row && row.host) byHost.set(row.host, row);
  }
  return HOSTS.map((id) => byHost.get(id)).filter(Boolean);
}

function applyReturnedHosts(payload) {
  const merged = mergeHosts(currentData?.hosts, payload?.hosts);
  currentData = { ...currentData, ...payload, hosts: merged };
  return currentData;
}

function checkItem(check) {
  return h("li", {},
    h("span", { text: check.ok ? "ok" : "fail" }),
    h("div", {}, h("strong", { text: `${HOST_LABELS[check.host] || check.host}: ${check.name}` }), h("small", { text: check.detail || "" })),
  );
}

function radio(name, value, checked, focusKey, label, onChange, disabled = false) {
  const input = h("input", { type: "radio", name, value, dataset: { focusKey }, disabled });
  input.checked = Boolean(checked);
  input.addEventListener("change", () => { if (ui?.applying) return; onChange(value); });
  return h("label", { class: "radio-option" }, input, h("span", { text: label }));
}

export function renderAiPolicyCard(data, context = {}) {
  syncState(data && typeof data === "object" ? data : { hosts: [], applies_to: "new_sessions" });
  const rows = rowsFor(currentData, ui.hostScope);
  const status = aggregateStatus(rows);
  const appliedMode = aggregateMode(rows);
  const applyEnabled = canApply(rows) && !ui.applying && Boolean(currentData?.csrf_token);
  const applyBtn = h("button", {
    class: "button button-primary",
    type: "button",
    text: ui.applying ? "Applying…" : "Apply",
    disabled: !applyEnabled,
    "aria-label": "Apply Codex AI mode",
    dataset: { focusKey: "ai-policy-apply" },
  });
  applyBtn.addEventListener("click", () => doApply(context));
  const resultErrors = (ui.results || []).filter((row) => row && row.ok === false);
  const checks = rows.flatMap((row) => (row.checks || []).map((check) => ({ host: row.host, ...check })));
  const failedChecks = checks.filter((check) => check.ok === false);
  const routineChecks = checks.filter((check) => check.ok !== false);
  return h("section", { class: "panel ai-policy-card", "aria-label": "Codex AI mode" },
    sectionHeading("AI mode", "Codex"),
    h("p", { id: "ai-policy-scope", class: "panel-note", text: "Applies to new Codex sessions only. Existing sessions keep the mode they started with." }),
    h("fieldset", { class: "ai-policy-fieldset" },
      h("legend", { text: "Mode" }),
      h("div", { class: "radio-row", role: "radiogroup", "aria-label": "Codex AI mode", "aria-describedby": "ai-policy-mode-help" },
        radio("codex-ai-mode", "ensemble", ui.mode === "ensemble", "ai-policy-mode-ensemble", MODE_LABELS.ensemble, (value) => { ui.mode = value; context.paint?.(); }, ui.applying),
        radio("codex-ai-mode", "native", ui.mode === "native", "ai-policy-mode-native", MODE_LABELS.native, (value) => { ui.mode = value; context.paint?.(); }, ui.applying),
      ),
      h("p", { id: "ai-policy-mode-help", class: "panel-note", text: MODE_HELP }),
    ),
    h("fieldset", { class: "ai-policy-fieldset" },
      h("legend", { text: "Host" }),
      h("div", { class: "radio-row", role: "radiogroup", "aria-label": "Codex AI mode host" },
        radio("codex-ai-host", "both", ui.hostScope === "both", "ai-policy-host-both", HOST_LABELS.both, (value) => { ui.hostScope = value; context.paint?.(); }, ui.applying),
        radio("codex-ai-host", "windows", ui.hostScope === "windows", "ai-policy-host-windows", HOST_LABELS.windows, (value) => { ui.hostScope = value; context.paint?.(); }, ui.applying),
        radio("codex-ai-host", "wsl", ui.hostScope === "wsl", "ai-policy-host-wsl", HOST_LABELS.wsl, (value) => { ui.hostScope = value; context.paint?.(); }, ui.applying),
      ),
    ),
    h("div", { class: "ai-policy-status", "aria-live": "polite" },
      h("div", { class: "task-status-line" },
        badge(STATUS_TONE[status] || "neutral", STATUS_LABELS[status] || status),
        appliedMode === "mixed" ? badge("warning", "mixed") : (appliedMode ? badge("neutral", MODE_LABELS[appliedMode] || appliedMode) : null),
      ),
      h("ul", { class: "ai-policy-hosts" }, rows.map((row) => h("li", { class: "ai-policy-host" },
        h("strong", { text: HOST_LABELS[row.host] || row.host }),
        badge(STATUS_TONE[row.status] || "neutral", STATUS_LABELS[row.status] || row.status || "unavailable"),
        h("span", { class: "panel-note", text: row.mode ? MODE_LABELS[row.mode] || row.mode : "No mode" }),
        row.error ? h("small", { class: "field-error", text: row.error }) : null,
      ))),
    ),
    failedChecks.length ? h("ul", { class: "check-list ai-policy-check-failures", "aria-label": "Failed Codex AI mode checks" }, failedChecks.map(checkItem)) : null,
    routineChecks.length ? h("details", { class: "disclosure", dataset: { persistKey: "ai-policy-checks" } },
      h("summary", {}, h("span", { text: `${routineChecks.length} routine integrity check${routineChecks.length === 1 ? "" : "s"}` })),
      h("div", { class: "disclosure-body" }, h("ul", { class: "check-list", "aria-label": "Routine Codex AI mode checks" }, routineChecks.map(checkItem))),
    ) : null,
    resultErrors.length ? h("div", { class: "callout callout-danger" }, h("strong", { text: "Apply did not succeed on every host." }), resultErrors.map((row) => h("p", { text: `${HOST_LABELS[row.host] || row.host}: ${row.error || "update failed"}` }))) : null,
    h("div", { class: "save-bar-actions" }, applyBtn),
  );
}

async function doApply(context) {
  if (!ui || ui.applying || !currentData?.csrf_token) return;
  const rows = rowsFor(currentData, ui.hostScope);
  if (!canApply(rows)) return;
  const mode = ui.mode;
  const hosts = selectedHosts(ui.hostScope);
  const revisions = expectedRevisions(rows);
  const csrfToken = currentData.csrf_token;
  mutationEpoch += 1;
  ui.applying = true;
  ui.results = null;
  context.paint?.();
  try {
    const result = await putJSON("/api/ai-policy", {
      mode,
      hosts,
      expected_revisions: revisions,
    }, csrfToken);
    applyReturnedHosts(result);
    ui.results = result.results || [];
    mutationEpoch += 1;
    ui.applying = false;
    context.onAiPolicy?.(currentData);
    context.toast?.(`${MODE_LABELS[mode] || mode} mode configured. Start a new Codex conversation to use it.`);
    context.paint?.();
  } catch (error) {
    if (error.payload) applyReturnedHosts(error.payload);
    ui.results = error.payload?.results || [];
    mutationEpoch += 1;
    ui.applying = false;
    context.onAiPolicy?.(currentData);
    context.toast?.(error.message || "Could not apply Codex AI mode.", "danger");
    context.paint?.();
  }
}

export const __test__ = {
  selectedHosts, aggregateStatus, aggregateMode, expectedRevisions, canApply, inferMode, mergeHosts, STATUS_LABELS, HOST_LABELS, MODE_LABELS, MODE_HELP,
};
