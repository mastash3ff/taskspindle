import { getJSON } from "../api.js";
import { badge, emptyState, formatDate, h, labeledValue, sectionHeading, table } from "../dom.js";

export const __test__ = { availabilityCard };

function availabilityCard(worker, item) {
  const state = item || {};
  return h("article", { class: "availability-card" },
    h("div", { class: "card-title-row" }, h("div", {}, h("span", { class: "eyebrow", text: "Worker access" }), h("h3", { text: worker.id })), badge(state.state || "ok", (state.state || "ok").replaceAll("_", " "))),
    h("p", { class: "status-reason", text: state.reason || "No current refusal recorded." }),
    h("dl", { class: "mini-details" },
      state.reset_at ? labeledValue("Reset", formatDate(state.reset_at)) : null,
      state.eligible_at ? labeledValue("Eligible again", formatDate(state.eligible_at)) : null,
    ),
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

function workerCard(worker) {
  return h("section", { class: "panel worker-card", dataset: { provider: worker.id } },
    h("header", { class: "worker-card-header" }, h("div", {}, h("span", { class: "eyebrow", text: worker.family || "Worker" }), h("h2", { text: worker.id })), badge(worker.availability?.state || "ok")),
    h("dl", { class: "detail-grid compact" }, labeledValue("Profile", worker.id), labeledValue("Model", worker.model || "Provider default"), labeledValue("Auth", worker.auth), labeledValue("Modes", (worker.modes || []).join(", ")), labeledValue("Command", worker.command_name, { mono: true }), labeledValue("Gateway", worker.gateway_host, { mono: true })),
    availabilityCard(worker, worker.availability),
    nativeCheck(worker.native_check),
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
    h("div", { class: "page-heading" }, h("div", {}, h("span", { class: "eyebrow", text: "Execution access" }), h("h1", { text: "Workers" }), h("p", { text: "Provider status, reset/eligible time, and cached native checks for each configured worker." })), stale ? badge("stale", "Cached data") : null),
    h("div", { class: "worker-grid" }, providers.map((worker) => workerCard(worker))),
    windowTable(providers), doctorPanel(data.doctor, { toast }),
  );
}
