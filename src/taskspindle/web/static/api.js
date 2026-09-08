const cache = new Map();

export class APIError extends Error {
  constructor(message, status, payload = null) { super(message); this.name = "APIError"; this.status = status; this.payload = payload; }
}

async function parseResponse(response) {
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch (_) { payload = null; }
  if (!response.ok) throw new APIError(payload?.error || payload?.message || `Request failed (${response.status})`, response.status, payload);
  return payload;
}

export async function getJSON(path, { signal, fresh = false, fallback = true } = {}) {
  if (!fresh && cache.has(path)) return { data: cache.get(path), cached: true, stale: false };
  try {
    const response = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store", signal });
    const data = await parseResponse(response);
    cache.set(path, data);
    return { data, cached: false, stale: false };
  } catch (error) {
    if (fallback && cache.has(path) && error.name !== "AbortError") return { data: cache.get(path), cached: true, stale: true, error };
    throw error;
  }
}

export async function postJSON(path, body, csrfToken, { signal } = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json", "X-TaskSpindle-CSRF": csrfToken },
    body: JSON.stringify(body || {}),
    cache: "no-store",
    signal,
  });
  return parseResponse(response);
}

export function peek(path) { return cache.get(path); }
export function clearCache(path = null) { if (path) cache.delete(path); else cache.clear(); }
