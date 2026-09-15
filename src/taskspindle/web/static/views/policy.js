import { aiPolicyEpoch, isAiPolicyApplying, renderAiPolicyCard } from "../ai-policy.js";
import { getJSON, postJSON, putJSON } from "../api.js";
import { badge, captureViewState, formatDate, h, labeledValue, relativeTime, restoreViewState, safeJSON, sectionHeading } from "../dom.js";

const FAMILY_ORDER = ["claude", "grok", "agy"];
const STATE_TONE = { active: "positive", paused: "warning", budget_exhausted: "danger" };
const WINDOWS = ["day", "week"];
const ROLE_KEY_RE = /^[a-z][a-z0-9_-]{0,31}$/;
const IDENTIFIER_RE = /^[A-Za-z0-9][A-Za-z0-9._[\]-]{0,63}$/;
const AGY_MODEL_RE = /^gemini-[0-9]+(?:\.[0-9]+)*-[a-z0-9]+(?:-(?:low|medium|high))?$/i;

// -- module state: one draft, keyed by the revision it branched from ----------------
let draftState = null; // { revision, base, draft, window, serverDrift }
let saveErrors = [];
let saveConflict = null;
let saving = false;
let mountedRoot = null;
let currentData = null;
let currentAiData = null;
let currentContext = null;
let currentErrorsByLoc = new Map();

// -- pure helpers (exported for tests) -----------------------------------------------

function applyPath(draft, path, value) {
  if (!Array.isArray(path) || path.length === 0) return value;
  const [key, ...rest] = path;
  const source = draft && typeof draft === "object" && !Array.isArray(draft) ? draft : {};
  return { ...source, [key]: applyPath(source[key], rest, value) };
}

function removeAtPath(draft, path) {
  if (!Array.isArray(path) || path.length === 0) return draft;
  const source = draft && typeof draft === "object" && !Array.isArray(draft) ? draft : {};
  const [key, ...rest] = path;
  if (rest.length === 0) {
    const clone = { ...source };
    delete clone[key];
    return clone;
  }
  return { ...source, [key]: removeAtPath(source[key], rest) };
}

function canonicalJSON(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJSON).join(",")}]`;
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJSON(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function isDirty(base, draft) {
  return canonicalJSON(base) !== canonicalJSON(draft);
}

function localValidate(draft, profiles = []) {
  const errors = [];
  const providerIds = profiles.map((profile) => (typeof profile === "string" ? profile : profile.id));
  const providerById = new Map(profiles.map((profile) => [typeof profile === "string" ? profile : profile.id, profile]));
  const providers = draft?.providers || {};
  let shareSum = 0;
  for (const id of providerIds) {
    const entry = providers[id];
    if (!entry) continue;
    if (entry.enabled !== false) shareSum += Number(entry.target_share) || 0;
    for (const field of ["advertised_models", "advertised_efforts", "models_without_effort"]) {
      for (const value of entry[field] || []) {
        if (!IDENTIFIER_RE.test(value)) errors.push({ loc: ["providers", id, field], msg: `"${value}" is not a valid identifier.`, code: "IDENTIFIER_INVALID" });
      }
    }
    if (entry.note && entry.note.length > 512) errors.push({ loc: ["providers", id, "note"], msg: "Note must be at most 512 characters.", code: "NOTE_TOO_LONG" });
  }
  if (shareSum > 100) errors.push({ loc: ["providers"], msg: `Enabled target shares sum to ${shareSum}, which is over 100.`, code: "SHARE_OVER_100" });
  const roles = draft?.roles || {};
  const roleKeys = Object.keys(roles);
  if (roleKeys.length > 32) errors.push({ loc: ["roles"], msg: "At most 32 roles are allowed.", code: "TOO_MANY_ROLES" });
  for (const key of roleKeys) {
    const role = roles[key] || {};
    if (!ROLE_KEY_RE.test(key)) errors.push({ loc: ["roles", key], msg: `"${key}" is not a valid role key.`, code: "ROLE_KEY_INVALID" });
    if (role.brief && role.brief.length > 512) errors.push({ loc: ["roles", key, "brief"], msg: "Brief must be at most 512 characters.", code: "BRIEF_TOO_LONG" });
    if (role.timeout_s != null && (role.timeout_s < 60 || role.timeout_s > 14400)) errors.push({ loc: ["roles", key, "timeout_s"], msg: "Timeout must be between 60 and 14400 seconds.", code: "TIMEOUT_OUT_OF_RANGE" });
    const selections = role.selections || {};
    for (const [providerId, selection] of Object.entries(selections)) {
      if (!selection) continue;
      const profile = providerById.get(providerId);
      const providerEntry = providers[providerId] || {};
      const noEffortModels = providerEntry.models_without_effort || [];
      if (profile?.family === "agy") {
        if (selection.model && !AGY_MODEL_RE.test(selection.model)) errors.push({ loc: ["roles", key, "selections", providerId, "model"], msg: "AGY model must be a Gemini id like gemini-<version>-<variant>[-low|medium|high].", code: "AGY_MODEL_INVALID" });
      } else {
        if (selection.model && !IDENTIFIER_RE.test(selection.model)) errors.push({ loc: ["roles", key, "selections", providerId, "model"], msg: `"${selection.model}" is not a valid identifier.`, code: "IDENTIFIER_INVALID" });
        if (selection.effort && !IDENTIFIER_RE.test(selection.effort)) errors.push({ loc: ["roles", key, "selections", providerId, "effort"], msg: `"${selection.effort}" is not a valid identifier.`, code: "IDENTIFIER_INVALID" });
      }
      if (selection.model && selection.effort && noEffortModels.includes(selection.model)) errors.push({ loc: ["roles", key, "selections", providerId, "effort"], msg: `${selection.model} does not take an effort.`, code: "EFFORT_NOT_ALLOWED" });
    }
  }
  return errors;
}

function orderByUnderTarget(status) {
  const providers = status?.providers || {};
  const known = status?.under_target_order || [];
  const seen = new Set(known);
  const rank = (id) => {
    const state = providers[id]?.share_state;
    return state === "under_target" ? 0 : state === "on_target" ? 1 : state === "over_target" ? 2 : state === "untracked" ? 3 : 4;
  };
  const rest = Object.keys(providers).filter((id) => !seen.has(id)).sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  return [...known, ...rest];
}

function errorsByLoc(errors = []) {
  const map = new Map();
  for (const err of errors) {
    const key = (err.loc || []).join(".");
    const list = map.get(key) || [];
    list.push(err.msg);
    map.set(key, list);
  }
  return map;
}

function normalizeTargets(providers = {}) {
  const result = {};
  for (const [id, entry] of Object.entries(providers)) {
    const share = Number(entry?.target_share);
    result[id] = Number.isFinite(share) ? Math.max(0, Math.min(100, share)) / 100 : 0;
  }
  return result;
}

function resetDraft() {
  draftState = null;
  saveErrors = [];
  saveConflict = null;
}

export const __test__ = { applyPath, isDirty, localValidate, orderByUnderTarget, errorsByLoc, normalizeTargets, resetDraft };

// -- draft lifecycle ------------------------------------------------------------------

function syncDraftState(data) {
  if (!draftState) {
    draftState = { revision: data.revision, base: data.policy, draft: data.policy, window: data.policy?.share_window === "week" ? "week" : "day", serverDrift: null };
    return;
  }
  const dirty = isDirty(draftState.base, draftState.draft);
  if (data.revision === draftState.revision) {
    if (!dirty) { draftState.base = data.policy; draftState.draft = data.policy; }
    draftState.serverDrift = null;
    return;
  }
  if (dirty) { draftState.serverDrift = data.revision; return; }
  draftState = { revision: data.revision, base: data.policy, draft: data.policy, window: draftState.window, serverDrift: null };
}

function discardAndRefresh() {
  draftState = null;
  saveErrors = []; saveConflict = null;
  if (mountedRoot) delete mountedRoot.dataset.holdPoll;
  currentContext?.refresh?.();
}

function setDraft(path, value) {
  draftState.draft = applyPath(draftState.draft, path, value);
  saveErrors = []; saveConflict = null;
  paint();
}

function removeDraft(path) {
  draftState.draft = removeAtPath(draftState.draft, path);
  saveErrors = []; saveConflict = null;
  paint();
}

function paint() {
  if (!mountedRoot || !currentData || !draftState) return;
  const captured = captureViewState(mountedRoot);
  const fresh = buildView(currentData, currentContext);
  if (fresh.dataset.holdPoll === "1") mountedRoot.dataset.holdPoll = "1"; else delete mountedRoot.dataset.holdPoll;
  mountedRoot.dataset.stale = fresh.dataset.stale;
  mountedRoot.replaceChildren(...Array.from(fresh.childNodes));
  restoreViewState(mountedRoot, captured);
}

async function doSave(data) {
  if (saving) return;
  saving = true; paint();
  try {
    const result = await putJSON("/api/policy", { if_revision: draftState.revision, policy: draftState.draft }, data.csrf_token);
    saving = false;
    draftState = null; saveErrors = []; saveConflict = null;
    if (mountedRoot) delete mountedRoot.dataset.holdPoll;
    currentContext?.toast?.(`Policy saved (revision ${result.revision})`);
    currentContext?.refresh?.();
  } catch (error) {
    saving = false;
    if (error.status === 409) { saveConflict = error.payload?.current_revision ?? "unknown"; saveErrors = []; }
    else if (error.status === 400) { saveErrors = error.payload?.details?.errors || []; saveConflict = null; }
    else currentContext?.toast?.(error.message || "Save failed.", "danger");
    paint();
  }
}

async function doReset(data) {
  try {
    const result = await postJSON("/api/policy/reset", { if_revision: draftState.revision }, data.csrf_token);
    draftState = null; saveErrors = []; saveConflict = null;
    if (mountedRoot) delete mountedRoot.dataset.holdPoll;
    currentContext?.toast?.(`Policy reset to defaults (revision ${result.revision})`);
    currentContext?.refresh?.();
  } catch (error) {
    currentContext?.toast?.(error.message || "Reset failed.", "danger");
  }
}

// -- small building blocks ------------------------------------------------------------

function orderedProfiles(profiles = []) {
  return [...profiles].sort((a, b) => {
    const ia = FAMILY_ORDER.indexOf(a.family), ib = FAMILY_ORDER.indexOf(b.family);
    const ra = ia === -1 ? FAMILY_ORDER.length : ia, rb = ib === -1 ? FAMILY_ORDER.length : ib;
    return ra - rb || a.id.localeCompare(b.id);
  });
}

function fieldError(loc) {
  const messages = currentErrorsByLoc.get(loc);
  return messages?.length ? h("p", { class: "field-error", text: messages.join(" ") }) : null;
}

function switchControl(label, checked, focusKey, onChange, disabled = false) {
  const input = h("input", { type: "checkbox", role: "switch", dataset: { focusKey }, disabled });
  input.checked = Boolean(checked);
  input.addEventListener("change", () => onChange(input.checked));
  return h("label", { class: "switch" }, input, h("span", { class: "switch-track" }, h("span", { class: "switch-thumb" })), h("span", { class: "switch-label", text: label }));
}

function pageHeading(stale, writable) {
  return h("div", { class: "page-heading" },
    h("div", {}, h("span", { class: "eyebrow", text: "Dispatch policy" }), h("h1", { text: "Policy" }), h("p", { text: "Codex AI mode for new sessions, then provider routing, target shares, budgets, and the model and effort each role uses on each provider." })),
    stale ? badge("stale", "Cached data") : null,
  );
}

function unwritableCallout() {
  return h("div", { class: "callout callout-warning" },
    h("strong", { text: "Policy editing is unavailable." }),
    h("p", { text: "The state database is not at schema 11 yet. Start the MCP server or run `taskspindle policy show` once, then reload this page." }),
  );
}

function driftCallout(serverRevision) {
  const reloadBtn = h("button", { class: "button button-secondary", type: "button", text: "Reload" });
  reloadBtn.addEventListener("click", discardAndRefresh);
  return h("div", { class: "callout callout-warning" },
    h("strong", { text: `Policy changed on the server (revision ${serverRevision}).` }),
    h("p", {}, "Reload to discard your draft, or keep editing; Save will be refused until you reload. ", reloadBtn),
  );
}

function windowSelector() {
  const select = h("select", { dataset: { focusKey: "policy-window" } }, WINDOWS.map((window) => h("option", { value: window, text: window === "day" ? "Day" : "Week" })));
  select.value = draftState.window;
  select.addEventListener("change", () => { draftState.window = select.value; paint(); });
  return h("label", { class: "filter-field" }, h("span", { text: "Observed window" }), select);
}

function targetShareRow(profile, providerDraft) {
  const value = Math.max(0, Math.min(100, Number(providerDraft.target_share) || 0));
  const range = h("input", { type: "range", class: "slider", min: "0", max: "100", step: "1", dataset: { focusKey: `policy-${profile.id}-share-range` } });
  const number = h("input", { type: "number", min: "0", max: "100", step: "1", dataset: { focusKey: `policy-${profile.id}-share-number` } });
  range.value = String(value); number.value = String(value);
  const commit = (raw) => setDraft(["providers", profile.id, "target_share"], Math.max(0, Math.min(100, Number(raw) || 0)));
  range.addEventListener("input", () => commit(range.value));
  number.addEventListener("change", () => commit(number.value));
  return h("div", { class: "target-share-row" }, range, number);
}

function shareBars(providerStatus, window) {
  const observed = providerStatus.observed?.[window] || {};
  const target = providerStatus.target_share_normalized ?? 0;
  const targetPct = Math.round(target * 100);
  const turnsShare = observed.share_turns ?? 0, tokensShare = observed.share_tokens ?? 0;
  const bar = (share) => h("div", { class: "share-bar" }, h("span", { class: "share-fill", style: `width:${Math.max(0, Math.min(100, share * 100))}%` }), h("span", { class: "share-target", style: `left:${Math.max(0, Math.min(100, targetPct))}%` }));
  return h("div", {},
    h("div", { class: "share-row" }, bar(turnsShare), h("small", { text: `${observed.turns ?? 0} turns · ${Math.round(turnsShare * 100)}% (target ${targetPct}%)` }), h("small", { text: `${observed.telemetry_turns ?? 0} of ${observed.turns ?? 0} turns had telemetry` })),
    h("div", { class: "share-row" }, bar(tokensShare), h("small", { text: `${observed.tokens ?? 0} tokens · ${Math.round(tokensShare * 100)}% (target ${targetPct}%)` })),
  );
}

function budgetRow(profile, providerDraft, providerStatus, window) {
  const budget = providerDraft.budgets?.[window] || {};
  const statusBudget = providerStatus.budgets?.[window] || {};
  const turns = h("input", { type: "number", min: "0", step: "1", dataset: { focusKey: `policy-${profile.id}-${window}-turns` } });
  turns.value = budget.turns == null ? "" : String(budget.turns);
  turns.addEventListener("change", () => setDraft(["providers", profile.id, "budgets", window, "turns"], turns.value === "" ? null : Number(turns.value)));
  const tokens = h("input", { type: "number", min: "0", step: "1", dataset: { focusKey: `policy-${profile.id}-${window}-tokens` } });
  tokens.value = budget.tokens == null ? "" : String(budget.tokens);
  tokens.addEventListener("change", () => setDraft(["providers", profile.id, "budgets", window, "tokens"], tokens.value === "" ? null : Number(tokens.value)));
  const enforceSwitch = switchControl("Enforce", Boolean(budget.enforce), `policy-${profile.id}-${window}-enforce`, (checked) => setDraft(["providers", profile.id, "budgets", window, "enforce"], checked));
  const turnsUsage = statusBudget.turns ? `${statusBudget.turns.used ?? 0} used${statusBudget.turns.remaining != null ? ` · ${statusBudget.turns.remaining} remaining` : ""}` : "No turns limit set";
  const tokensUsage = statusBudget.tokens ? `${statusBudget.tokens.used ?? 0} used${statusBudget.tokens.remaining != null ? ` · ${statusBudget.tokens.remaining} remaining` : ""}` : "No tokens limit set";
  return h("div", { class: "budget-row" },
    h("strong", { text: window === "day" ? "Day" : "Week" }),
    h("label", { class: "filter-field" }, h("span", { text: "Turns" }), turns), h("small", { text: turnsUsage }),
    h("label", { class: "filter-field" }, h("span", { text: "Tokens" }), tokens), h("small", { text: tokensUsage }),
    enforceSwitch,
  );
}

function chipEditor(label, profile, field, focusKey) {
  const items = draftState.draft.providers?.[profile.id]?.[field] || [];
  const list = h("ul", { class: "chip-list" });
  items.forEach((item, index) => {
    const remove = h("button", { class: "chip-remove", type: "button", text: "×", "aria-label": `Remove ${item}` });
    remove.addEventListener("click", () => setDraft(["providers", profile.id, field], items.filter((_, i) => i !== index)));
    list.append(h("li", { class: "chip" }, h("span", { text: item }), remove));
  });
  const input = h("input", { type: "text", placeholder: `Add ${label.toLowerCase()}`, dataset: { focusKey } });
  const add = () => {
    const value = input.value.trim();
    if (value && !items.includes(value) && items.length < 64) setDraft(["providers", profile.id, field], [...items, value]);
    input.value = "";
  };
  input.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); add(); } });
  const addBtn = h("button", { class: "button button-quiet", type: "button", text: "Add" });
  addBtn.addEventListener("click", add);
  return h("div", {}, h("p", { class: "panel-note", text: label }), list, h("div", { class: "chip-add" }, input, addBtn));
}

function allowedModesField(profile, providerDraft) {
  const modes = profile.modes || [];
  const value = providerDraft.allowed_modes;
  const boxes = modes.map((mode) => {
    const checked = value == null ? true : value.includes(mode);
    const input = h("input", { type: "checkbox", dataset: { focusKey: `policy-${profile.id}-mode-${mode}` } });
    input.checked = checked;
    input.addEventListener("change", () => {
      const current = Array.isArray(providerDraft.allowed_modes) ? providerDraft.allowed_modes : [...modes];
      const next = input.checked ? [...new Set([...current, mode])] : current.filter((m) => m !== mode);
      setDraft(["providers", profile.id, "allowed_modes"], next.length === modes.length ? null : next);
    });
    return h("label", { class: "chip" }, input, h("span", { text: mode }));
  });
  return h("div", {}, h("div", { class: "chip-list" }, boxes), h("small", { class: "panel-note", text: value == null ? "Inherit (all served modes)" : "" }));
}

function noteField(profile, providerDraft) {
  const textarea = h("textarea", { class: "policy-note", maxlength: "512", dataset: { focusKey: `policy-${profile.id}-note` } });
  textarea.value = providerDraft.note || "";
  textarea.addEventListener("change", () => setDraft(["providers", profile.id, "note"], textarea.value));
  return textarea;
}

function mappingBlock(obj) {
  const entries = Object.entries(obj || {});
  if (!entries.length) return h("span", { text: "—" });
  return h("div", { class: "mono" }, entries.map(([id, value]) => h("div", { text: `${id} = ${value && typeof value === "object" ? JSON.stringify(value) : value}` })));
}

function providerCard(profile, draft, status, window) {
  const providerDraft = draft.providers?.[profile.id] || {};
  const providerStatus = status?.providers?.[profile.id] || {};
  const state = providerStatus.state || (providerDraft.enabled === false ? "paused" : "active");
  return h("section", { class: "panel policy-provider-card", dataset: { provider: profile.id } },
    h("header", { class: "worker-card-header" },
      h("div", {}, h("span", { class: "eyebrow", text: profile.family || "provider" }), h("h2", { text: profile.id })),
      h("div", { class: "task-status-line" }, badge(STATE_TONE[state] || "neutral", state.replaceAll("_", " ")), switchControl("Enabled", providerDraft.enabled !== false, `policy-${profile.id}-enabled`, (checked) => setDraft(["providers", profile.id, "enabled"], checked))),
    ),
    h("div", { class: "filter-field", dataset: { loc: `providers.${profile.id}.target_share` } }, h("span", { text: "Target share" }), targetShareRow(profile, providerDraft)),
    fieldError(`providers.${profile.id}.target_share`),
    h("h4", { text: "Observed" }), shareBars(providerStatus, window),
    h("h4", { text: "Budgets" }), budgetRow(profile, providerDraft, providerStatus, "day"), budgetRow(profile, providerDraft, providerStatus, "week"),
    h("h4", { text: "Models" }),
    chipEditor("Advertised models", profile, "advertised_models", `policy-${profile.id}-models-add`),
    chipEditor("Advertised efforts", profile, "advertised_efforts", `policy-${profile.id}-efforts-add`),
    chipEditor("Models without effort", profile, "models_without_effort", `policy-${profile.id}-noeffort-add`),
    h("h4", { text: "Allowed modes" }), allowedModesField(profile, providerDraft),
    h("h4", { text: "Note" }), noteField(profile, providerDraft),
  );
}

// -- role matrix ------------------------------------------------------------------------

function preferenceCell(key, role, providers) {
  const list = Array.isArray(role.provider_preference) ? role.provider_preference : [];
  const chips = h("ul", { class: "pref-order chip-list" });
  list.forEach((id, index) => {
    const up = h("button", { type: "button", text: "▲", "aria-label": `Move ${id} earlier`, disabled: index === 0 });
    const down = h("button", { type: "button", text: "▼", "aria-label": `Move ${id} later`, disabled: index === list.length - 1 });
    const remove = h("button", { class: "chip-remove", type: "button", text: "×", "aria-label": `Remove ${id} from preference` });
    up.addEventListener("click", () => { const next = [...list]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; setDraft(["roles", key, "provider_preference"], next); });
    down.addEventListener("click", () => { const next = [...list]; [next[index + 1], next[index]] = [next[index], next[index + 1]]; setDraft(["roles", key, "provider_preference"], next); });
    remove.addEventListener("click", () => setDraft(["roles", key, "provider_preference"], list.filter((_, i) => i !== index)));
    chips.append(h("li", { dataset: { focusKey: `policy-role-${key}-pref-${id}` } }, h("span", { text: id }), up, down, remove));
  });
  const available = providers.map((p) => p.id).filter((id) => !list.includes(id));
  let addControl = null;
  if (available.length) {
    const select = h("select", { dataset: { focusKey: `policy-role-${key}-pref-add` } }, available.map((id) => h("option", { value: id, text: id })));
    const addBtn = h("button", { class: "button button-quiet", type: "button", text: "Add" });
    addBtn.addEventListener("click", () => setDraft(["roles", key, "provider_preference"], [...list, select.value]));
    addControl = h("div", { class: "chip-add" }, select, addBtn);
  }
  return h("div", {}, chips, addControl);
}

function selectionCells(key, role, provider) {
  const selection = role.selections?.[provider.id] || {};
  const providerDraft = draftState.draft.providers?.[provider.id] || {};
  const models = providerDraft.advertised_models || [];
  const efforts = providerDraft.advertised_efforts || [];
  const noEffort = (providerDraft.models_without_effort || []).includes(selection.model);
  let modelControl;
  if (provider.family === "agy") {
    modelControl = h("input", { type: "text", placeholder: "gemini-<version>-<variant>[-low|medium|high]", dataset: { focusKey: `policy-role-${key}-${provider.id}-model` } });
    modelControl.value = selection.model || "";
    modelControl.addEventListener("change", () => setDraft(["roles", key, "selections", provider.id, "model"], modelControl.value || null));
  } else {
    modelControl = h("select", { dataset: { focusKey: `policy-role-${key}-${provider.id}-model` } }, h("option", { value: "", text: "(unset)" }), models.map((m) => h("option", { value: m, text: m })));
    modelControl.value = selection.model || "";
    modelControl.addEventListener("change", () => {
      const value = modelControl.value || null;
      let nextDraft = applyPath(draftState.draft, ["roles", key, "selections", provider.id, "model"], value);
      if (value && (providerDraft.models_without_effort || []).includes(value)) nextDraft = applyPath(nextDraft, ["roles", key, "selections", provider.id, "effort"], null);
      draftState.draft = nextDraft; saveErrors = []; saveConflict = null; paint();
    });
  }
  const effortSelect = h("select", { dataset: { focusKey: `policy-role-${key}-${provider.id}-effort` }, disabled: noEffort });
  effortSelect.append(h("option", { value: "", text: "(unset)" }), ...efforts.map((e) => h("option", { value: e, text: e })));
  effortSelect.value = noEffort ? "" : (selection.effort || "");
  effortSelect.addEventListener("change", () => setDraft(["roles", key, "selections", provider.id, "effort"], effortSelect.value || null));
  return [
    h("td", { "data-label": `${provider.id} model`, dataset: { loc: `roles.${key}.selections.${provider.id}.model` } }, modelControl, fieldError(`roles.${key}.selections.${provider.id}.model`)),
    h("td", { "data-label": `${provider.id} effort`, dataset: { loc: `roles.${key}.selections.${provider.id}.effort` } }, effortSelect, fieldError(`roles.${key}.selections.${provider.id}.effort`)),
  ];
}

function roleRow(key, role, providers) {
  role = role || {};
  const removeBtn = h("button", { class: "button button-quiet", type: "button", text: "Remove", "aria-label": `Remove role ${key}` });
  removeBtn.addEventListener("click", () => removeDraft(["roles", key]));
  const brief = h("textarea", { maxlength: "512", dataset: { focusKey: `policy-role-${key}-brief` } });
  brief.value = role.brief || "";
  brief.addEventListener("change", () => setDraft(["roles", key, "brief"], brief.value));
  const timeout = h("input", { type: "number", min: "60", max: "14400", step: "1", dataset: { focusKey: `policy-role-${key}-timeout` } });
  timeout.value = role.timeout_s == null ? "" : String(role.timeout_s);
  timeout.addEventListener("change", () => setDraft(["roles", key, "timeout_s"], timeout.value === "" ? null : Number(timeout.value)));
  const cells = [
    h("td", { "data-label": "Role" }, h("strong", { class: "mono", text: key }), removeBtn),
    h("td", { "data-label": "Brief" }, brief),
    h("td", { "data-label": "Preference order" }, preferenceCell(key, role, providers)),
    ...providers.flatMap((provider) => selectionCells(key, role, provider)),
    h("td", { "data-label": "Timeout (s)", dataset: { loc: `roles.${key}.timeout_s` } }, timeout, fieldError(`roles.${key}.timeout_s`)),
  ];
  return h("tr", { dataset: { role: key } }, cells);
}

function roleMatrix(draftRoles, providers) {
  const headers = ["Role", "Brief", "Preference order", ...providers.flatMap((p) => [`${p.id} model`, `${p.id} effort`]), "Timeout (s)"];
  const rows = Object.entries(draftRoles).sort(([a], [b]) => a.localeCompare(b)).map(([key, role]) => roleRow(key, role, providers));
  return h("div", { class: "table-wrap" }, h("table", { class: "role-matrix responsive-table" },
    h("thead", {}, h("tr", {}, headers.map((label) => h("th", { scope: "col", text: label })))),
    h("tbody", {}, rows),
  ));
}

function addRoleControl(draftRoles) {
  const input = h("input", { type: "text", placeholder: "role_key", dataset: { focusKey: "policy-add-role-key" } });
  const btn = h("button", { class: "button button-secondary", type: "button", text: "Add role" });
  const error = h("p", { class: "field-error" });
  btn.addEventListener("click", () => {
    const key = input.value.trim();
    if (!ROLE_KEY_RE.test(key)) { error.textContent = "Role key must match ^[a-z][a-z0-9_-]{0,31}$."; return; }
    if (draftRoles[key]) { error.textContent = "That role already exists."; return; }
    if (Object.keys(draftRoles).length >= 32) { error.textContent = "At most 32 roles are allowed."; return; }
    setDraft(["roles", key], { brief: "", provider_preference: [], selections: {}, timeout_s: null });
    input.value = "";
  });
  return h("div", { class: "chip-add" }, input, btn, error);
}

// -- file-managed panel, save bar, history ----------------------------------------------

function fileManagedPanel(fileManaged) {
  const managed = fileManaged || {};
  return h("section", { class: "panel" }, sectionHeading("File-managed", "config.toml"),
    h("dl", { class: "detail-grid compact" },
      labeledValue("Concurrency", null, { node: mappingBlock(managed.concurrency) }),
      labeledValue("Native overage", null, { node: mappingBlock(managed.native_overage) }),
      labeledValue("Provider recovery", null, { node: mappingBlock(managed.provider_recovery) }),
      labeledValue("Config file", managed.config_file, { mono: true }),
    ),
    h("p", { class: "panel-note", text: "Edited in config.toml and read on every refresh; the dashboard never writes it." }),
    managed.error ? h("div", { class: "callout callout-danger", text: managed.error }) : null,
  );
}

function historyDisclosure() {
  const list = h("ul", { class: "history-list" });
  const viewer = h("pre", { class: "mono history-viewer" });
  const body = h("div", { class: "disclosure-body" }, list, viewer);
  const details = h("details", { class: "disclosure", dataset: { persistKey: "policy-history" } }, h("summary", {}, h("span", { text: "History" }), h("span", { text: "" })), body);
  details.addEventListener("toggle", async () => {
    if (!details.open || list.dataset.loaded === "1") return;
    list.dataset.loaded = "1";
    try {
      const { data } = await getJSON("/api/policy/history?limit=50", { fresh: true });
      list.replaceChildren(...(data.history || []).map((row) => {
        const item = h("li", {});
        const viewBtn = h("button", { class: "button button-quiet", type: "button", text: "View" });
        viewBtn.addEventListener("click", async () => {
          try { const revision = await getJSON(`/api/policy/history/${row.revision}`, { fresh: true }); viewer.textContent = safeJSON(revision.data); }
          catch (error) { viewer.textContent = error.message; }
        });
        item.append(h("span", { text: `Revision ${row.revision} · ${formatDate(row.updated_at)} · ${row.updated_by || "unknown"}` }), viewBtn);
        return item;
      }));
    } catch (_) { list.replaceChildren(h("li", { text: "Could not load history." })); }
  });
  return details;
}

function saveBar(data) {
  const dirty = isDirty(draftState.base, draftState.draft);
  const errors = localValidate(draftState.draft, data.profiles || []);
  const canSave = dirty && errors.length === 0 && !draftState.serverDrift && !saving;
  const saveBtn = h("button", { class: "button button-primary", type: "button", text: saving ? "Saving…" : "Save", disabled: !canSave });
  saveBtn.addEventListener("click", () => doSave(data));
  const discardBtn = h("button", { class: "button button-secondary", type: "button", text: "Discard", disabled: !dirty });
  discardBtn.addEventListener("click", () => { draftState.draft = draftState.base; saveErrors = []; saveConflict = null; paint(); });
  const resetBtn = h("button", { class: "button button-quiet", type: "button", text: "Reset to defaults" });
  const cancelBtn = h("button", { class: "button button-secondary", type: "button", text: "Cancel" });
  const confirmBtn = h("button", { class: "button button-primary", type: "button", text: "Reset" });
  const resetDialog = h("dialog", { class: "reset-dialog" },
    h("p", { text: "Reset the dispatch policy to defaults? This creates a new revision and cannot be undone from here." }),
    h("div", { class: "save-bar-actions" }, cancelBtn, confirmBtn),
  );
  resetBtn.addEventListener("click", () => resetDialog.showModal?.());
  cancelBtn.addEventListener("click", () => resetDialog.close?.());
  confirmBtn.addEventListener("click", () => { resetDialog.close?.(); doReset(data); });
  const statusText = data.source === "defaults" ? `Revision ${data.revision} · defaults` : `Revision ${data.revision} · updated ${relativeTime(data.updated_at)} by ${data.updated_by || "unknown"}`;
  const errorMessages = saveErrors.map((err) => `${(err.loc || []).join(".") || "document"}: ${err.msg}`);
  return h("div", { class: "save-bar" },
    h("div", { class: "save-bar-status" }, h("strong", { text: dirty ? "Unsaved changes" : "No changes" }), h("span", { text: statusText })),
    saveConflict != null ? h("div", { class: "callout callout-danger" }, h("strong", { text: `Save refused: the server is at revision ${saveConflict}.` }), h("p", { text: "Reload to discard your draft, or keep editing and reload before saving again." })) : null,
    errorMessages.length ? h("div", { class: "callout callout-danger" }, h("strong", { text: "Save failed." }), errorMessages.map((msg) => h("p", { text: msg }))) : null,
    dirty && errors.length ? h("div", { class: "callout callout-warning local-errors" }, h("strong", { text: "Fix before saving:" }), errors.map((err) => h("p", { text: `${(err.loc || []).join(".") || "document"}: ${err.msg}` }))) : null,
    h("div", { class: "save-bar-actions" }, saveBtn, discardBtn, resetBtn, historyDisclosure()),
    resetDialog,
  );
}

// -- top-level view ---------------------------------------------------------------------

function unavailableAiPolicy(csrfToken) {
  return {
    hosts: [
      { host: "windows", mode: null, status: "unavailable", revision: null, checks: [], error: "ADAPTER_UNAVAILABLE" },
      { host: "wsl", mode: null, status: "unavailable", revision: null, checks: [], error: "ADAPTER_UNAVAILABLE" },
    ],
    applies_to: "new_sessions",
    csrf_token: csrfToken || "",
  };
}

function buildView(data, _context) {
  currentErrorsByLoc = errorsByLoc(saveErrors);
  const writable = data.writable !== false;
  const heading = pageHeading(data.stale, writable);
  const aiCard = renderAiPolicyCard(currentAiData, {
    toast: currentContext?.toast,
    paint,
    onAiPolicy(next) { currentAiData = next; },
  });
  const holdPoll = isDirty(draftState?.base, draftState?.draft) || isAiPolicyApplying();
  if (!writable) {
    const blocked = h("div", { class: "view policy-view", dataset: { stale: String(Boolean(data.stale)) } }, heading, aiCard, unwritableCallout());
    if (holdPoll) blocked.dataset.holdPoll = "1";
    return blocked;
  }
  const profiles = orderedProfiles(data.profiles || []);
  const driftNode = draftState.serverDrift ? driftCallout(draftState.serverDrift) : null;
  const cards = h("div", { class: "policy-grid" }, profiles.map((profile) => providerCard(profile, draftState.draft, data.status, draftState.window)));
  const matrix = h("section", { class: "panel" }, sectionHeading("Roles", "Preference, selections, and timeout"), roleMatrix(draftState.draft.roles || {}, profiles), addRoleControl(draftState.draft.roles || {}));
  const view = h("div", { class: "view policy-view", dataset: { stale: String(Boolean(data.stale)) } },
    heading, aiCard, h("div", { class: "filter-bar" }, windowSelector()), driftNode, cards, matrix, fileManagedPanel(data.file_managed), saveBar(data),
  );
  if (holdPoll) view.dataset.holdPoll = "1";
  return view;
}

export async function renderPolicy(route, context = {}) {
  const { signal } = context;
  const fetchEpoch = aiPolicyEpoch();
  const policyRequest = getJSON("/api/policy", { signal, fresh: true });
  const aiRequest = getJSON("/api/ai-policy", { signal, fresh: true }).catch(() => null);
  const { data, stale } = await policyRequest;
  const aiResult = await aiRequest;
  syncDraftState(data);
  currentContext = context;
  currentData = { ...data, stale };
  const aiStale = isAiPolicyApplying() || fetchEpoch !== aiPolicyEpoch();
  if (!aiStale) {
    currentAiData = aiResult ? { ...aiResult.data, stale: aiResult.stale } : unavailableAiPolicy(data.csrf_token);
  }
  const node = buildView(currentData, currentContext);
  mountedRoot = node;
  return node;
}
