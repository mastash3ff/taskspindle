import { getJSON, postJSON } from "../api.js";
import { badge, formatDate, h, labeledValue, sectionHeading } from "../dom.js";

const pending = new Map();
const WORKERS = { claude: "claude", grok: "grok", google_ai: "agy" };
const BILLING = { provider_web: "Provider website", apple: "Apple App Store", google_play: "Google Play", x_premium: "X Premium", unknown: "Unknown" };
const NEXT_ACTION = {
  start: "Ready for a new task.", retry: "Retry a worker task to verify access.", wait: "Wait for the provider limit to reset, then retry.",
  sign_in: "Sign in to the worker CLI, then retry a task.", review_access: "Review this worker profile's access, then retry a task.", choose_model: "Choose another model, then retry.",
};
const ERROR_COPY = {
  SETUP_REQUIRED: "Automatic verification needs the Chrome extension connection. Existing verified details are retained.",
  AUTH_REQUIRED: "Sign in to this provider in Chrome, then reconnect.", ACCOUNT_MISMATCH: "Chrome is signed in to a different account. Reconnect with the intended account.",
  UNSUPPORTED_BILLING_CHANNEL: "This billing channel cannot be verified automatically.", PARSE_CHANGED: "The billing page changed and could not be verified.",
  UNAVAILABLE: "Subscription verification is temporarily unavailable.",
};

function billingState(row) {
  const code = row.error?.code;
  if (code === "SETUP_REQUIRED") return ["Chrome setup required", "warning"];
  if (["AUTH_REQUIRED", "ACCOUNT_MISMATCH"].includes(code)) return ["Reconnect required", "warning"];
  if (code === "UNSUPPORTED_BILLING_CHANNEL") return ["Unsupported billing channel", "warning"];
  if (row.error && !row.last_success_at) return ["Verification failed", "danger"];
  if (row.status === "renewing") return [row.renews_at ? `Renews on ${formatDate(row.renews_at, row.date_precision === "date")}` : "Renewing", "positive"];
  if (row.status === "cancelled") {
    const end = row.access_ends_at ? ` — access ends ${formatDate(row.access_ends_at, row.date_precision === "date")}` : "";
    const remaining = row.days_remaining == null || row.days_remaining < 0 ? "" : row.days_remaining === 0 ? " — today" : ` — ${row.days_remaining} day${row.days_remaining === 1 ? "" : "s"} remaining`;
    return [`Cancelled${end}${remaining}`, "warning"];
  }
  if (row.status === "expired") return ["Expired", "danger"];
  if (row.status === "free") return ["Free", "neutral"];
  if (row.status === "none") return ["No subscription", "neutral"];
  return ["Unavailable", "neutral"];
}

function currentOperation(row) { return pending.get(row.provider) || row.operation || null; }
function operationCopy(operation) {
  if (!operation) return "";
  if (operation.status === "requesting") return "Submitting the request…";
  if (operation.origin === "scheduled") return `A scheduled subscription check is ${operation.status === "running" ? "running" : "queued"}.`;
  if (operation.action === "connect") return operation.status === "running" ? "Chrome verification is running. Connection state updates after verification succeeds." : "Connect queued. Your normal Chrome profile will open for verification.";
  return operation.status === "running" ? "Subscription refresh is running." : "Refresh queued.";
}

function warning(row) {
  if (row.end_passed_unverified) return "Verification needed — the recorded access end has passed. Refresh before treating this subscription as expired.";
  if (row.upcoming_end_warning === "within_1_day") return "Access is scheduled to end within one day. Refresh now to confirm the deadline.";
  if (row.upcoming_end_warning === "within_7_days") return "Access is scheduled to end within seven days. Refresh now to confirm the deadline.";
  if (row.freshness === "stale") return "Verification is stale. Refresh to confirm the current subscription.";
  return null;
}

function workerSource(source) {
  return ({ task_success: "Successful worker task", turn_ok: "Successful worker task", native_auth_check: "Native CLI check", rate_limit_event: "Quota report", acp_prompt_response: "Worker response", acp_error: "Worker refusal", worker_error: "Worker refusal" })[source] || (source ? "Worker status record" : "No worker evidence");
}

function workerAccess(row, providerData) {
  if (row.provider === "chatgpt") return h("section", { class: "subscription-worker" }, h("div", { class: "card-title-row" }, h("h3", { text: "Worker access" }), badge("neutral", "Coordinator only")), h("p", { text: "ChatGPT is used by the Codex coordinator." }));
  const id = WORKERS[row.provider];
  const worker = providerData?.providers?.find((item) => item.id === id);
  if (!worker) return h("section", { class: "subscription-worker" }, h("div", { class: "card-title-row" }, h("h3", { text: "Worker access" }), badge("unknown", providerData ? "Not configured" : "Unavailable")), h("p", { class: "panel-note", text: providerData ? "The associated worker profile is not configured." : "Worker status could not be loaded." }));
  const access = worker.availability || {};
  const selected = access.scope === "model" ? access.affected_model : null;
  const models = (worker.model_availability || []).filter((item) => item?.affected_model && item.affected_model !== selected);
  return h("section", { class: "subscription-worker" },
    h("div", { class: "card-title-row" }, h("div", {}, h("span", { class: "eyebrow", text: "Worker access" }), h("h3", { text: worker.id })), badge(access.state || "unknown", String(access.state || "unknown").replaceAll("_", " "))),
    h("dl", { class: "mini-details" }, labeledValue("Profile model", worker.model || "Provider default"), labeledValue("Refusal scope", access.scope === "account" ? "Worker profile/account" : access.scope === "model" ? `Model · ${access.affected_model || worker.model || "selected"}` : "No active refusal"), labeledValue("Last success", formatDate(access.last_success_at)), labeledValue("Source", workerSource(access.source))),
    h("p", { class: "next-action", text: NEXT_ACTION[access.next_action] || "Run a worker task to establish current access." }),
    models.length ? h(
      "details",
      { class: "model-details", dataset: { persistKey: `subscription-models-${row.provider}` } },
      h("summary", { text: `${models.length} model-specific observation${models.length === 1 ? "" : "s"}` }),
      h("ul", { class: "compact-list" }, models.map((item) => h("li", {},
        h("span", { text: item.affected_model }),
        badge(item.state || "unknown"),
        h("small", { text: NEXT_ACTION[item.next_action] || item.reason || "No current evidence." }),
      ))),
    ) : null,
    row.provider === "google_ai" ? h("p", { class: "panel-note", text: "Product association only: the agy worker account is not verified as the browser subscription account." }) : null,
    h("p", { class: "panel-note", text: "Browser subscription and worker CLI accounts are not assumed to match." }),
  );
}

function actionButton(row, action, operation, csrf, context) {
  const reconnect = action === "connect" && row.connected;
  let label = action === "connect" ? (reconnect ? "Reconnect" : "Connect") : "Refresh now";
  const active = operation?.action === action;
  if (active) label = operation.status === "requesting" ? "Requesting…" : operation.status === "running" ? (action === "connect" ? (reconnect ? "Reconnecting…" : "Connecting…") : "Refreshing…") : `${label} queued`;
  const button = h("button", { class: action === "connect" ? "button button-primary" : "button button-secondary", type: "button", text: label, "aria-label": `${label} ${row.label || row.provider}`, dataset: { subscriptionProvider: row.provider, subscriptionAction: action } });
  button.disabled = Boolean(operation) || (action === "refresh" && !row.connected);
  button.addEventListener("click", async () => {
    if (!csrf || pending.has(row.provider)) return;
    pending.set(row.provider, { provider: row.provider, action, status: "requesting", origin: "manual" }); context.refresh();
    try {
      const job = await postJSON(`/api/subscriptions/${encodeURIComponent(row.provider)}/${action}`, {}, csrf);
      pending.set(row.provider, job.job || { provider: row.provider, action, status: "queued", origin: "manual" });
      context.toast(`${row.label || row.provider} ${action} queued.`); context.refresh();
    } catch (error) {
      pending.delete(row.provider);
      const copy = { 400: "The request was not available.", 403: "Reload this local dashboard and try again.", 409: "Connect this account before refreshing it.", 503: "Subscription tracking is temporarily unavailable." }[error.status] || "Unable to request a subscription check.";
      context.toast(copy, "danger"); context.refresh();
    }
  });
  return button;
}

function card(row, providers, csrf, context, selected = false) {
  const [label, tone] = billingState(row);
  const operation = currentOperation(row);
  const warn = warning(row);
  const setup = row.error?.code === "SETUP_REQUIRED";
  return h("article", { class: `panel subscription-card ${selected ? "subscription-card-highlight" : ""}`, id: `subscription-${row.provider}`, dataset: { provider: row.provider } },
    h("header", { class: "subscription-header" }, h("div", {}, h("span", { class: "eyebrow", text: "Browser billing" }), h("h2", { text: row.label || row.provider })), badge(tone, label)),
    h("div", { class: "subscription-primary" }, h("strong", { text: row.plan || (row.status === "none" ? "No paid plan" : "Plan not verified") }), h("span", { text: row.account_label || "Account not verified" }), h("small", { text: row.last_success_at ? `Last verified ${formatDate(row.last_success_at)}` : "Not yet verified" })),
    h("div", { class: "subscription-actions" }, actionButton(row, "connect", operation, csrf, context), actionButton(row, "refresh", operation, csrf, context)),
    operation ? h("p", { class: "operation-message", "aria-live": "polite", text: operationCopy(operation) }) : null,
    warn ? h("div", { class: "callout callout-warning", text: warn }) : null,
    row.error ? h("div", { class: "callout callout-warning" }, h("strong", { text: label }), h("p", { text: ERROR_COPY[row.error.code] || "The last verification did not complete." }), setup ? h("p", {}, "Install the ", h("a", { href: "https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm", target: "_blank", rel: "noopener noreferrer", text: "Playwright Chrome extension" }), ", then run ", h("code", { text: "taskspindle subscriptions setup-extension" }), ". Enter the private token only in that command's hidden prompt.") : null) : null,
    h("details", { class: "billing-disclosure", dataset: { persistKey: `billing-${row.provider}` } }, h("summary", { text: "Billing details" }), h("dl", { class: "detail-grid compact billing-details" }, labeledValue("Billing channel", BILLING[row.billing_channel] || "Unknown"), labeledValue("Renews", formatDate(row.renews_at, row.date_precision === "date")), labeledValue("Access ends", formatDate(row.access_ends_at, row.date_precision === "date")), labeledValue("Last attempt", formatDate(row.last_attempt_at)), labeledValue("Verification", row.freshness === "fresh" ? "Current" : row.freshness === "stale" ? "Stale" : "Not yet verified"), labeledValue("Source", "Browser subscription check"))),
    workerAccess(row, providers),
  );
}

export async function renderSubscriptions(route, context = {}) {
  const [subscriptions, providers] = await Promise.all([
    getJSON("/api/subscriptions", { signal: context.signal, fresh: true }),
    getJSON("/api/providers", { signal: context.signal, fresh: true }).catch(() => ({ data: null, stale: true })),
  ]);
  const data = subscriptions.data;
  for (const row of data.subscriptions || []) {
    const local = pending.get(row.provider);
    if (local && (row.operation || local.status !== "requesting")) pending.delete(row.provider);
  }
  const collector = data.collector_running ? `Collector running${data.collector_last_seen_at ? ` · last seen ${formatDate(data.collector_last_seen_at)}` : ""}` : "Collector unavailable";
  const selected = route.query.get("provider");
  const rows = [...(data.subscriptions || [])].sort((a, b) => Number(b.provider === selected) - Number(a.provider === selected));
  return h("div", { class: "view subscriptions-view", dataset: { stale: String(subscriptions.stale || providers.stale) } },
    h("div", { class: "page-heading" }, h("div", {}, h("span", { class: "eyebrow", text: "Account verification" }), h("h1", { text: "Subscriptions" }), h("p", { text: "Check browser billing separately from worker access. Connect uses your normal Chrome profile and marks an account connected only after verification." })), subscriptions.stale ? badge("stale", "Cached data") : null),
    h("section", { class: "subscription-toolbar" }, h("div", {}, h("strong", { text: data.scheduled_refresh_enabled ? "Scheduled browser checks: On" : "Scheduled browser checks: Off (default)" }), h("span", { text: "Use Connect or Refresh now to check billing." })), h("div", { class: data.collector_running ? "collector-ok" : "collector-down", text: collector })),
    h("div", { class: "subscription-grid" }, rows.map((row) => card(row, providers.data, data.csrf_token, context, row.provider === selected))),
  );
}
