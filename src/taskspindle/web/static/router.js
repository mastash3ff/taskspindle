const ROUTES = new Set(["overview", "tasks", "workers", "subscriptions", "usage"]);

export function parseHash(hash = location.hash) {
  const raw = (hash || "#/overview").replace(/^#\/?/, "");
  const [pathPart, queryPart = ""] = raw.split("?", 2);
  const parts = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  let name = parts[0] || "overview";
  if (name === "providers") name = "workers";
  if (!ROUTES.has(name)) name = "overview";
  return { name, id: name === "tasks" ? parts[1] || null : null, query: new URLSearchParams(queryPart) };
}

export function routeHref(name, id = null, query = null) {
  const path = `#/${encodeURIComponent(name)}${id ? `/${encodeURIComponent(id)}` : ""}`;
  const params = query instanceof URLSearchParams ? query.toString() : new URLSearchParams(query || {}).toString();
  return `${path}${params ? `?${params}` : ""}`;
}

export function updateRouteQuery(changes, { replace = true } = {}) {
  const route = parseHash();
  for (const [key, value] of Object.entries(changes)) {
    if (value == null || value === "") route.query.delete(key); else route.query.set(key, value);
  }
  const next = routeHref(route.name, route.id, route.query);
  if (replace) history.replaceState(null, "", next); else location.hash = next.slice(1);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}

export function subscribe(callback) {
  const handler = () => callback(parseHash());
  window.addEventListener("hashchange", handler);
  handler();
  return () => window.removeEventListener("hashchange", handler);
}
