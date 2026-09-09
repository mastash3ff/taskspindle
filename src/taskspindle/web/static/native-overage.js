import { h, labeledValue, formatDate, sectionHeading, table } from "./dom.js";

const LABELS = { observe_only: "Observe only", provider_managed: "Provider managed", included: "Included", native_overage: "Native overage", mixed: "Mixed", unknown: "Unknown", overage: "Native extra usage eligible", blocked: "Blocked", provider_enforced_native_attempt: "Provider-managed attempt allowed", included_exhausted_observe_only: "Included allowance exhausted", native_attempt_pending: "Native attempt pending", native_attempt_refused: "Native attempt refused", native_paid_allowance_unavailable: "Native paid allowance unavailable", hard_provider_block: "Provider access blocked", no_included_exhaustion_evidence: "No included exhaustion observed", rate_limit_event: "Provider quota report" };
const label = (value) => LABELS[value] || (value ? String(value).replaceAll("_", " ") : "Unknown");
const yesNo = (value) => value === true ? "Enabled" : value === false ? "Disabled" : "Unknown";
const money = (value, billing) => billing?.unit === "usd_cents" && billing?.currency === "USD" && Number.isSafeInteger(value) && value >= 0 ? `$${(value / 100).toFixed(2)}` : "Unknown";

export function nativeOverageDetails(info, check = null) {
  const billing = check?.billing;
  return h("section", { class: "native-overage", "aria-label": "Native extra usage" },
    h("h3", { text: "Native extra usage" }),
    h("dl", { class: "mini-details" },
      labeledValue("Policy", label(info?.policy)),
      labeledValue("Eligibility", label(info?.eligibility)),
      labeledValue("Admission reason", label(info?.admission_reason)),
      labeledValue("Billing observation", label(info?.billing_classification)),
      labeledValue("Source", label(info?.source)),
      labeledValue("Observed", info?.observed_at ? formatDate(info.observed_at) : "Unknown"),
      labeledValue("Native allowance status", label(info?.observed?.status)),
      labeledValue("Disabled reason", label(info?.observed?.disabled_reason)),
      labeledValue("Native allowance reset", info?.observed?.resets_at ? formatDate(info.observed.resets_at) : "Unknown"),
      labeledValue("Last observed extra usage", info?.observed?.in_use === true ? "In use" : info?.observed?.in_use === false ? "Not in use" : "Unknown"),
    ),
    h("p", { class: "muted", text: info?.control_scope === "account" ? "Provider account settings control spending. Observe only is not a per-task spending block." : "Provider-managed extra usage requires an enabled policy. Missing billing observations remain unknown." }),
    h("p", { class: "muted", text: "Live native extra-usage qualification has not been established by these observations." }),
    billing ? h("div", {},
      h("h4", { text: "Account billing diagnostics" }),
      h("p", { text: `Shared account observations, not task charges. Freshness: ${check.freshness || "unknown"}.` }),
      h("dl", { class: "mini-details" },
        labeledValue("Prepaid balance", money(billing.prepaid_balance, billing)),
        labeledValue("On-demand cap", money(billing.on_demand_cap, billing)),
        labeledValue("On-demand used", money(billing.on_demand_used, billing)),
        labeledValue("On-demand feature available", yesNo(billing.on_demand_enabled)),
        labeledValue("Automatic top-up", yesNo(billing.auto_topup?.enabled)),
        labeledValue("Top-up amount", money(billing.auto_topup?.topup_amount, billing)),
        labeledValue("Monthly top-up maximum", money(billing.auto_topup?.max_amount_per_month, billing)),
        labeledValue("Billing observed", billing.observed_at ? formatDate(billing.observed_at) : "Unknown"),
      ),
    ) : null,
  );
}

export function nativeOverageSummary(summary) {
  const rows = summary?.groups || [];
  const dimensions = ["provider", "day", "model", "mode", "repository_id"].filter((key) => rows.some((row) => key in row));
  return h("section", { class: "panel" },
    sectionHeading("Billing observations", "Native extra usage"),
    h("p", { text: summary ? `${summary.total_turns} turns; ${summary.observed_turns} classified; ${summary.unknown_turns} unknown.` : "Unknown: no billing observations available." }),
    rows.length ? table([...dimensions.map((key) => key === "repository_id" ? "Repository" : key), "Policy", "Classification", "Turns"], rows.map((row) => h("tr", {},
      dimensions.map((key) => h("td", { text: key === "repository_id" ? row.repository_path || row[key] || "No repository" : row[key] || "Unknown" })),
      h("td", { text: label(row.policy) }), h("td", { text: label(row.billing_classification) }), h("td", { text: row.turns }),
    ))) : null,
    h("p", { class: "cost-note", text: summary?.note || "Billing observations are separate from token estimates. Historical or missing telemetry remains unknown." }),
  );
}
