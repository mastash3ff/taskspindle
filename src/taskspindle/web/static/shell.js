import { getJSON } from "./api.js";
import { captureViewState, emptyState, h, restoreViewState } from "./dom.js";
import { parseHash, routeHref, subscribe } from "./router.js";
import { renderOverview } from "./views/overview.js";
import { renderTasks } from "./views/tasks.js";
import { renderWorkers } from "./views/workers.js";
import { renderSubscriptions } from "./views/subscriptions.js";
import { renderUsage } from "./views/usage.js";

const renderers = { overview: renderOverview, tasks: renderTasks, workers: renderWorkers, subscriptions: renderSubscriptions, usage: renderUsage };
const POLL = { overview: 10_000, tasks: 5_000, workers: 15_000, subscriptions: 5_000, usage: 30_000 };
let generation = 0, controller = null, timer = null, currentRoute = null;
const app = document.getElementById("app");
const routeKey = (route) => `${route.name}/${route.id || ""}?${route.query.toString()}`;

function setTheme(value) {
  const theme = ["dark", "light", "system"].includes(value) ? value : "dark";
  document.documentElement.dataset.theme = theme;
  document.getElementById("theme-select").value = theme;
  try { localStorage.setItem("taskspindle-theme", theme); } catch (_) {}
}

function toast(message, tone = "positive") {
  const region = document.getElementById("toast-region");
  const item = h("div", { class: `toast toast-${tone}`, role: "status", text: message });
  region.append(item);
  setTimeout(() => item.remove(), 4200);
}

function updateChrome(route, stale = false) {
  const title = route.id ? `Task ${route.id}` : route.name[0].toUpperCase() + route.name.slice(1);
  const breadcrumbs = document.getElementById("breadcrumbs");
  breadcrumbs.replaceChildren(h("span", { text: "TaskSpindle" }), h("span", { "aria-hidden": "true", text: "/" }), h("strong", { text: title }));
  document.title = `${title} · TaskSpindle`;
  document.querySelectorAll(".primary-nav a").forEach((link) => { const active = link.dataset.route === route.name; link.classList.toggle("active", active); if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current"); });
  document.getElementById("refreshed").textContent = `${stale ? "Cached" : "Updated"} ${new Intl.DateTimeFormat(undefined, { timeStyle: "short" }).format(new Date())}`;
}

function closeDrawer() {
  document.body.classList.remove("drawer-open");
  document.getElementById("menu-button").setAttribute("aria-expanded", "false");
  document.getElementById("sidebar").inert = window.matchMedia("(max-width: 860px)").matches;
}

async function render(route = currentRoute, { polling = false } = {}) {
  if (!route) return;
  if (polling && document.hidden) return;
  const ownGeneration = ++generation;
  if (controller) controller.abort();
  controller = new AbortController();
  const isSame = currentRoute && routeKey(currentRoute) === routeKey(route);
  currentRoute = route;
  clearTimeout(timer);
  if (!polling && !isSame) app.replaceChildren(h("div", { class: "loading-state", text: "Loading…" }));
  try {
    const node = await renderers[route.name](route, { signal: controller.signal, toast, refresh: () => render(parseHash(), { polling: true }) });
    if (ownGeneration !== generation || controller.signal.aborted) return;
    const state = polling && isSame ? captureViewState(app) : null;
    app.replaceChildren(node);
    if (isSame) restoreViewState(app, state); else window.scrollTo(0, 0);
    updateChrome(route, node.dataset.stale === "true");
  } catch (error) {
    if (error.name === "AbortError" || ownGeneration !== generation) return;
    console.error(error);
    if (app.querySelector(".view")) toast("Could not refresh. Showing the last loaded data.", "danger");
    else app.replaceChildren(h("div", { class: "panel fatal-state" }, emptyState("Unable to load this view", "The local API did not return data."), h("button", { class: "button button-primary", type: "button", text: "Try again", onclick: () => render(route) })));
  } finally {
    if (ownGeneration === generation && !document.hidden) timer = setTimeout(() => render(parseHash(), { polling: true }), POLL[route.name]);
  }
}

function setupDrawer() {
  const button = document.getElementById("menu-button");
  const sidebar = document.getElementById("sidebar");
  const mobile = window.matchMedia("(max-width: 860px)");
  const sync = () => { sidebar.inert = mobile.matches && !document.body.classList.contains("drawer-open"); };
  button.addEventListener("click", () => { const open = !document.body.classList.contains("drawer-open"); document.body.classList.toggle("drawer-open", open); button.setAttribute("aria-expanded", String(open)); sync(); });
  document.getElementById("drawer-backdrop").addEventListener("click", closeDrawer);
  document.querySelector(".primary-nav").addEventListener("click", (event) => { if (event.target.closest("a")) closeDrawer(); });
  mobile.addEventListener("change", sync); sync();
  window.addEventListener("keydown", (event) => { if (event.key === "Escape" && document.body.classList.contains("drawer-open")) { closeDrawer(); sync(); button.focus(); } });
}

function setupCommand() {
  const dialog = document.getElementById("command-dialog"), input = document.getElementById("command-input"), results = document.getElementById("command-results");
  let tasks = [], selected = 0, debounce = null;
  const destinations = [
    ["Overview", "Current activity and attention", routeHref("overview")], ["Tasks", "Execution ledger", routeHref("tasks")],
    ["Workers", "Access and model status", routeHref("workers")], ["Subscriptions", "Browser billing verification", routeHref("subscriptions")], ["Usage", "Tokens, outcomes, and limits", routeHref("usage")],
  ];
  const paint = () => {
    const q = input.value.trim().toLowerCase();
    const navigation = destinations.filter((item) => !q || item.some((value) => value.toLowerCase().includes(q))).map(([label, note, href]) => ({ label, note, href, meta: "Navigate" }));
    const taskMatches = tasks.filter((task) => !q || [task.id, task.state, task.provider, task.mode, task.repository?.path, task.repository_path, task.summary].some((value) => String(value || "").toLowerCase().includes(q))).slice(0, Math.max(0, 12 - navigation.length)).map((task) => ({ label: task.id, note: task.summary || task.repository?.path || task.repository_path || task.repository_id || "No summary", href: routeHref("tasks", task.id), meta: `${task.state || "Unknown"} · ${task.provider || "Unassigned"}` }));
    const matches = [...navigation, ...taskMatches];
    selected = Math.min(selected, Math.max(matches.length - 1, 0));
    const nodes = matches.length ? matches.map((item, index) => h("a", { class: `command-result ${index === selected ? "selected" : ""}`, href: item.href, role: "option", "aria-selected": String(index === selected), dataset: { resultIndex: index } }, h("span", {}, h("strong", { class: item.meta === "Navigate" ? "" : "mono", text: item.label }), h("small", { text: item.note })), h("span", { text: item.meta }))) : [h("p", { class: "command-empty", text: "No matching tasks or destinations" })];
    results.replaceChildren(...nodes);
    results.querySelectorAll("a").forEach((link) => link.addEventListener("click", () => dialog.close()));
  };
  const open = async () => {
    if (!dialog.open) dialog.showModal();
    input.value = ""; selected = 0; input.focus(); paint();
    try { const { data } = await getJSON("/api/tasks?limit=200", { fresh: true }); tasks = data.tasks || []; paint(); } catch (_) { paint(); }
  };
  document.getElementById("open-command").addEventListener("click", open);
  window.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); open(); }
  });
  input.addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(paint, 90); });
  input.addEventListener("keydown", (event) => {
    const links = [...results.querySelectorAll("a")];
    if (event.key === "ArrowDown") { event.preventDefault(); selected = Math.min(selected + 1, links.length - 1); paint(); }
    if (event.key === "ArrowUp") { event.preventDefault(); selected = Math.max(selected - 1, 0); paint(); }
    if (event.key === "Enter" && links[selected]) { event.preventDefault(); links[selected].click(); }
  });
}

export function start() {
  const stored = (() => { try { return localStorage.getItem("taskspindle-theme"); } catch (_) { return null; } })();
  setTheme(stored || "dark");
  document.querySelector(".skip-link").addEventListener("click", (event) => { event.preventDefault(); app.focus({ preventScroll: true }); app.scrollIntoView({ block: "start" }); });
  document.getElementById("theme-select").addEventListener("change", (event) => setTheme(event.target.value));
  setupDrawer(); setupCommand();
  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (!document.hidden && currentRoute) render(parseHash(), { polling: true });
  });
  subscribe((route) => { closeDrawer(); render(route); });
}
