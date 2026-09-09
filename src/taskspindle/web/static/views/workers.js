import { getJSON } from "../api.js";
import { badge, emptyState, formatDate, h, labeledValue, sectionHeading, table } from "../dom.js";
import { nativeOverageDetails } from "../native-overage.js";
import { routeHref } from "../router.js";

const NEXT_ACTION = {
  start: "Ready for a new task.", retry: "Retry a task to verify access.", wait: "Wait for the limit to reset, then retry.",
  sign_in: "Sign in to this worker CLI, then retry.", review_access: "Review profile access, then retry.", choose_model: "Choose another model, then retry.",
};
const SOURCE = {
  task_success: "Successful task", turn_ok: "Successful task", native_auth_check: "Native CLI check", rate_limit_event: "Quota report",
  acp_prompt_response: "Worker response", acp_error: "Worker refusal", worker_error: "Worker refusal",
};

const RECOVERY_COPY = {
  none: ["Controlled retry available", "A recorded refusal can be retried once with explicit approval."],
  armed: ["Retry armed", "The retry is authorized and waiting for a task to claim it."],
  claimed: ["Recovery attempt pending", "The authorized task will report whether provider access succeeds."],
  succeeded: ["Recovery succeeded", "The provider accepted access for this controlled attempt."],
  failed: ["Recovery failed", "The controlled attempt ended without establishing provider access."],
  revoked: ["Recovery revoked", "This authorization can no longer be used."],
  expired: ["Recovery expired", "The retry deadline passed before the initial prompt was admitted."],
};
const RECOVERY_TONE = { none: "warning", armed: "warning", claimed: "warning", succeeded: "positive", failed: "danger", expired: "danger", revoked: "neutral" };

function quotaRestrictions(restrictions = []) {
  if (!Array.isArray(restrictions) || !restrictions.length) return null;
  return h("section", { class: "quota-restrictions", "aria-label": "Active quota restrictions" },
    h("h4", { text: restrictions.length === 1 ? "Active quota restriction" : "Active quota restrictions" }),
    h("div", { class: "quota-restriction-list" }, restrictions.map((restriction) => h("article", { class: "quota-restriction" },
      h("dl", { class: "mini-details" },
        labeledValue("Scope", restriction.scope || "—"),
        labeledValue("Model family", restriction.model_family || restriction.model || "All models"),
        labeledValue("Window", restriction.window || "—"),
        labeledValue("Reset", formatDate(restriction.reset || restriction.reset_at)),
        labeledValue("Source", SOURCE[restriction.source] || restriction.source || "—"),
        labeledValue("Observed", formatDate(restriction.observed || restriction.observed_at)),
        restriction.fingerprint ? labeledValue("Evidence fingerprint", restriction.fingerprint, { mono: true }) : null,
      ),
    ))),
  );
}

function authContext(context) {
  if (!context?.changed) return null;
  return h("div", { class: "callout callout-warning auth-context" },
    h("strong", { text: "Authentication context changed." }),
    h("span", { text: " This metadata signals that availability evidence came from a changed authentication context; it does not identify an account." }),
    context.fingerprint ? h("code", { text: context.fingerprint }) : null,
  );
}

function quotaRetry(retry) {
  if (!retry || !["pending", "claimed", "prompting"].includes(retry.state)) return null;
  const task = retry.task_id ? h("a", { href: routeHref("tasks", retry.task_id), class: "text-link mono", text: retry.task_id }) : null;
  return h("div", { class: "callout callout-warning quota-retry" },
    h("strong", { text: "Quota retry pending." }),
    task ? h("span", {}, " Task: ", task, ".") : h("span", { text: " A post-reset attempt is already pending." }),
  );
}

export const __test__ = {
  quotaDetails: (availability) => h("div", {}, quotaRestrictions(availability.quota_restrictions), authContext(availability.auth_context), quotaRetry(availability.quota_retry)),
};

export function recoveryFeedback(recovery, context = {}) {
  if (!recovery || (recovery.state === "none" && !recovery.can_arm && !recovery.cli_command)) return null;
  const state = recovery.state || "none";
  const [title, description] = RECOVERY_COPY[state] || ["Controlled recovery", "Review the recorded recovery state before retrying."];
  const inspectPermit = recovery.next_action === "inspect_permit";
  const newerBlock = state === "succeeded" && recovery.next_action && recovery.next_action !== "start";
  const live = h("span", { class: "copy-feedback", "aria-live": "polite" });
  const copy = recovery.cli_command ? h("button", {
    class: "button button-secondary recovery-copy", type: "button", text: "Copy command",
    dataset: { focusKey: `copy-recovery-${recovery.permit_id || recovery.status_key || recovery.provider || "available"}` },
  }) : null;
  if (copy) copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(recovery.cli_command);
      live.textContent = "Command copied.";
      context.toast?.("Recovery command copied.");
    } catch (_) {
      live.textContent = "Copy failed. Select the command text instead.";
      context.toast?.("Could not copy the command.", "danger");
    }
  });
  const task = recovery.task_id ? h("a", { href: routeHref("tasks", recovery.task_id), class: "text-link mono", text: recovery.task_id }) : null;
  const details = [
    labeledValue("Provider", recovery.provider || "—", { mono: true }),
    labeledValue("Model scope", recovery.model || "Provider default", { mono: Boolean(recovery.model) }),
    recovery.permit_id ? labeledValue("Permit", recovery.permit_id, { mono: true }) : null,
    recovery.expires_at ? labeledValue(state === "armed" || state === "claimed" ? "Deadline" : "Deadline was", formatDate(recovery.expires_at)) : null,
    recovery.task_id ? labeledValue("Task", null, { node: task }) : null,
    recovery.outcome ? labeledValue("Outcome", recovery.outcome) : null,
    recovery.outcome_code ? labeledValue("Outcome code", recovery.outcome_code, { mono: true }) : null,
    recovery.evidence_revision ? labeledValue("Evidence revision", recovery.evidence_revision, { mono: true }) : null,
  ];
  return h("section", { class: `recovery-feedback recovery-${state}`, "aria-label": "Controlled worker recovery" },
    h("div", { class: "recovery-heading" }, h("strong", { text: title }), badge(RECOVERY_TONE[state] || state, state === "none" ? "Available" : state)),
    h("p", { text: description }),
    inspectPermit ? h("div", { class: "callout callout-warning", text: "Inspect the named permit. It belongs to a different provider, model, or evidence revision and cannot authorize this projection." }) : null,
    newerBlock ? h("div", { class: "callout callout-warning", text: "Current access is separate from this successful attempt and remains blocked by newer availability evidence." }) : null,
    details.some(Boolean) ? h("dl", { class: "mini-details recovery-details" }, details) : null,
    recovery.cli_command ? h("div", { class: "recovery-command" }, h("code", { text: recovery.cli_command }), h("div", { class: "recovery-command-actions" }, copy, live)) : null,
  );
}

function availabilityCard(worker, item, model = null, context = {}) {
  const state = item || {};
  return h("article", { class: `availability-card ${model ? "availability-model" : ""}` },
    h("div", { class: "card-title-row" }, h("div", {}, h("span", { class: "eyebrow", text: model ? "Model status" : "Worker access" }), h("h3", { text: model || worker.id })), badge(state.state || "unknown", (state.state || "unknown").replaceAll("_", " "))),
    h("p", { class: "status-reason", text: state.reason || (state.state === "unknown" ? "No current access evidence." : "No status detail.") }),
    h("dl", { class: "mini-details" },
      labeledValue("Scope", state.scope || "—"),
      labeledValue("Source", SOURCE[state.source] || (state.source ? "Worker status record" : "No evidence")),
      labeledValue("Observed", formatDate(state.observed_at)),
      labeledValue("Last success", formatDate(state.last_success_at)),
      state.reset_at ? labeledValue("Reset", formatDate(state.reset_at)) : null,
    ),
    state.stale ? h("div", { class: "callout callout-warning", text: "This observation is stale." }) : null,
    quotaRestrictions(state.quota_restrictions),
    authContext(state.auth_context),
    quotaRetry(state.quota_retry),
    h("p", { class: "next-action", text: state.next_action === "wait" && state.reset_at ? `Wait until ${formatDate(state.reset_at)}, then retry.` : NEXT_ACTION[state.next_action] || "Run a worker task to establish current access." }),
    recoveryFeedback(state.recovery, context),
  );
}

function nativeCheck(check) {
  if (!check) return null;
  const retained = check.last_success && check.state !== "quota";
  const snapshot = retained ? check.last_success : check;
  const details = [
    labeledValue("Freshness", check.freshness || "unknown"),
    labeledValue("Last attempt", formatDate(check.last_attempt_at)),
    labeledValue("Last success", formatDate(check.last_success_at || check.last_success?.checked_at)),
    snapshot?.plan ? labeledValue("Plan", snapshot.plan) : null,
    snapshot?.model_count != null ? labeledValue("Models", snapshot.model_count) : null,
    snapshot?.used_percent != null ? labeledValue("Used", `${snapshot.used_percent}%`) : null,
    snapshot?.window ? labeledValue("Window", snapshot.window) : null,
    snapshot?.reset_at ? labeledValue("Reset", formatDate(snapshot.reset_at)) : null,
  ];
  return h("section", { class: "native-check" },
    h("div", { class: "card-title-row" }, h("h3", { text: "Native check" }), badge(check.state || "not_checked", String(check.state || "not checked").replaceAll("_", " "))),
    h("p", { class: "panel-note", text: "Cached CLI evidence only. Account binding is unverified." }),
    retained ? h("p", { class: "panel-note", text: "Showing quota details from the last successful same-account check." }) : null,
    h("dl", { class: "mini-details" }, details),
    check.detail ? h("p", { class: "status-reason", text: check.detail }) : null,
  );
}

function workerCard(worker, context) {
  const selected = worker.availability?.scope === "model" ? worker.availability.affected_model : null;
  const models = (worker.model_availability || []).filter((item) => item?.affected_model && item.affected_model !== selected);
  return h("section", { class: "panel worker-card", dataset: { provider: worker.id } },
    h("header", { class: "worker-card-header" }, h("div", {}, h("span", { class: "eyebrow", text: worker.family || "Worker" }), h("h2", { text: worker.id })), badge(worker.availability?.state || "unknown")),
    h("dl", { class: "detail-grid compact" }, labeledValue("Profile", worker.id), labeledValue("Model", worker.model || "Provider default"), labeledValue("Auth", worker.auth), labeledValue("Modes", (worker.modes || []).join(", ")), labeledValue("Command", worker.command_name, { mono: true }), labeledValue("Gateway", worker.gateway_host, { mono: true })),
    availabilityCard(worker, worker.availability, selected, context),
    models.length ? h("details", { class: "model-details", dataset: { persistKey: `models-${worker.id}` } }, h("summary", { text: `${models.length} model observation${models.length === 1 ? "" : "s"}` }), h("div", { class: "model-grid" }, models.map((item) => availabilityCard(worker, item, item.affected_model, context)))) : null,
    nativeCheck(worker.native_check),
    nativeOverageDetails(worker.native_overage || worker.availability?.native_overage, worker.native_check),
  );
}

function windowTable(providers) {
  const rows = providers.flatMap((worker) => (worker.windows || []).map((item) => ({ worker: worker.id, ...item })));
  return h("section", { class: "panel" }, sectionHeading("Usage windows", "Cached telemetry"), rows.length ? table(
    ["Worker", "Window", "Status", "Used", "Resets", "Observed"],
    rows.map((row) => h("tr", {},
      h("td", { "data-label": "Worker", text: row.worker }),
      h("td", { "data-label": "Window", text: row.window }),
      h("td", { "data-label": "Status" }, badge(row.status || "unknown")),
      h("td", { "data-label": "Used", text: row.used_percent == null ? "—" : `${row.used_percent}%` }),
      h("td", { "data-label": "Resets", text: formatDate(row.resets_at) }),
      h("td", { "data-label": "Observed", text: formatDate(row.observed_at) }),
    )),
    "responsive-table",
  ) : emptyState("No window telemetry", "A worker has not reported a usage window yet."));
}

function doctorPanel(doctor, context) {
  const body = h("div", { class: "doctor-results" });
  const fill = (result) => {
    const content = result?.checks?.length ? h("ul", { class: "check-list" }, result.checks.map((check) => h("li", {},
      badge(check.ok ? "ok" : check.advisory ? "warning" : "failed", check.ok ? "Pass" : check.advisory ? "Advisory" : "Fail"),
      h("span", {}, h("strong", { text: check.name }), h("small", { text: check.detail })),
    ))) : emptyState(result?.status === "not_run" ? "Run diagnostics" : "No doctor checks", result?.status === "not_run" ? "Live diagnostics have not been run." : "No cached diagnostics are available.");
    body.replaceChildren(content);
  };
  fill(doctor);
  const button = h("button", { class: "button button-secondary", type: "button", text: "Run live probes" });
  button.addEventListener("click", async () => {
    button.disabled = true; button.textContent = "Running probes…";
    try { const { data } = await getJSON("/api/doctor?live=1", { fresh: true, fallback: false }); fill(data); context.toast("Live probes finished."); }
    catch (error) { context.toast(error.message, "danger"); }
    finally { button.disabled = false; button.textContent = "Run live probes"; }
  });
  const status = doctor?.status === "not_run" ? "Not run" : doctor?.cached ? (doctor.fresh ? "Cached · current" : "Cached · stale") : "Cached status unavailable";
  return h("details", { class: "panel disclosure doctor-disclosure", dataset: { persistKey: "system-doctor" } },
    h("summary", {}, h("span", { text: "System diagnostics" }), h("span", { text: status })),
    h("div", { class: "disclosure-body" },
      h("div", { class: "doctor-actions" }, h("p", { class: "panel-note", text: `Live probes run only when you choose this action.${doctor?.checked_at ? ` Last checked ${formatDate(doctor.checked_at)}.` : ""}` }), button),
      doctor?.cached && !doctor.fresh ? h("div", { class: "callout callout-warning", text: "Cached doctor results are stale. Run live probes for current diagnostics." }) : null,
      body,
    ),
  );
}

export async function renderWorkers(_route, { signal, toast } = {}) {
  const { data, stale } = await getJSON("/api/providers", { signal, fresh: true });
  const providers = data.providers || [];
  return h("div", { class: "view workers-view", dataset: { stale: String(stale) } },
    h("div", { class: "page-heading" }, h("div", {}, h("span", { class: "eyebrow", text: "Execution access" }), h("h1", { text: "Workers" }), h("p", { text: "See task evidence, model-specific refusals, and cached native checks for each configured worker." })), stale ? badge("stale", "Cached data") : null),
    h("div", { class: "worker-grid" }, providers.map((worker) => workerCard(worker, { toast }))),
    windowTable(providers), doctorPanel(data.doctor, { toast }),
  );
}
