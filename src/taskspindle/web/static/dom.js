export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  const append = (child) => {
    if (child == null || child === false) return;
    if (Array.isArray(child)) child.forEach(append);
    else node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  };
  children.forEach(append);
  return node;
}

export function s(tag, attrs = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === "text") node.textContent = String(value);
    else node.setAttribute(key === "className" ? "class" : key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) { node.replaceChildren(); }

export function formatDate(value, dateOnly = false) {
  if (!value) return "—";
  if (dateOnly && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const [year, month, day] = value.split("-").map(Number);
    return new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" })
      .format(new Date(year, month - 1, day));
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? String(value) : new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium", timeStyle: dateOnly ? undefined : "short",
  }).format(parsed);
}

export function relativeTime(value) {
  if (!value) return "Never";
  const delta = new Date(value).valueOf() - Date.now();
  if (!Number.isFinite(delta)) return String(value);
  const abs = Math.abs(delta);
  const units = abs < 60_000 ? [1000, "second"] : abs < 3_600_000 ? [60_000, "minute"] : abs < 86_400_000 ? [3_600_000, "hour"] : [86_400_000, "day"];
  return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(Math.round(delta / units[0]), units[1]);
}

export function toneFor(value = "") {
  const key = String(value).toLowerCase();
  if (["danger", "warning", "positive", "neutral"].includes(key)) return key;
  if (/failed|error|expired|denied|unavailable|ambiguous|blocked|check_failed/.test(key)) return "danger";
  if (/warning|attention|interrupt|cancel|throttled|stale|queued|preparing|unknown|auth_required|unsupported/.test(key)) return "warning";
  if (/active|running|ready|complete|success|connected|renewing|ok|fresh/.test(key)) return "positive";
  return "neutral";
}

export function badge(value, label = null) {
  return h("span", { class: `badge badge-${toneFor(value)}`, text: label ?? value ?? "Unknown" });
}

export function metric(label, value, note = null, tone = "") {
  return h("article", { class: `metric ${tone ? `metric-${tone}` : ""}` },
    h("span", { class: "metric-label", text: label }),
    h("strong", { class: "metric-value", text: value ?? "—" }),
    note ? h("small", { text: note }) : null,
  );
}

export function sectionHeading(title, eyebrow, action = null) {
  return h("div", { class: "section-heading" },
    h("div", {}, eyebrow ? h("span", { class: "eyebrow", text: eyebrow }) : null, h("h2", { text: title })),
    action,
  );
}

export function emptyState(title, message) {
  return h("div", { class: "empty-state" }, h("span", { class: "empty-glyph", "aria-hidden": "true", text: "◇" }), h("h3", { text: title }), h("p", { text: message }));
}

export function table(headers, rows, className = "") {
  return h("div", { class: "table-wrap" }, h("table", { class: className },
    h("thead", {}, h("tr", {}, headers.map((item) => h("th", { scope: "col", text: item })))),
    h("tbody", {}, rows),
  ));
}

export function labeledValue(label, value, options = {}) {
  const shown = value == null || value === "" ? "—" : value;
  return h("div", { class: "labeled-value" }, h("dt", { text: label }), options.node ? h("dd", {}, options.node) : h("dd", { class: options.mono ? "mono" : "", text: shown }));
}

export function captureViewState(root) {
  const active = document.activeElement;
  const selection = window.getSelection();
  const nodePath = (node) => {
    const path = [];
    while (node && node !== root) {
      const parent = node.parentNode;
      if (!parent) return null;
      path.unshift([...parent.childNodes].indexOf(node));
      node = parent;
    }
    return node === root ? path : null;
  };
  const selectedText = selection?.rangeCount && !selection.isCollapsed && !(active instanceof HTMLInputElement) && !(active instanceof HTMLTextAreaElement) && root.contains(selection.anchorNode) ? {
    anchor: nodePath(selection.anchorNode), anchorOffset: selection.anchorOffset,
    focus: nodePath(selection.focusNode), focusOffset: selection.focusOffset,
  } : null;
  return {
    focusKey: root.contains(active) ? active.dataset.focusKey || active.id || null : null,
    selection: active instanceof HTMLInputElement ? [active.selectionStart, active.selectionEnd] : null,
    scrollX: window.scrollX,
    scrollY: window.scrollY,
    scrollRegions: [...root.querySelectorAll("[data-scroll-key]")].map((el) => [el.dataset.scrollKey, el.scrollLeft, el.scrollTop]),
    controls: [...root.querySelectorAll("[data-focus-key]")].map((el) => [el.dataset.focusKey, el.value, el.checked]),
    disclosures: [...root.querySelectorAll("details[data-persist-key]")].map((el) => [el.dataset.persistKey, el.open]),
    selectedText,
  };
}

export function restoreViewState(root, state) {
  if (!state) return;
  for (const [key, open] of state.disclosures || []) {
    const item = [...root.querySelectorAll("details[data-persist-key]")].find((el) => el.dataset.persistKey === key);
    if (item) item.open = open;
  }
  for (const [key, value, checked] of state.controls || []) {
    const item = [...root.querySelectorAll("[data-focus-key]")].find((el) => el.dataset.focusKey === key);
    if (item && "value" in item) item.value = value;
    if (item && typeof checked === "boolean" && "checked" in item) item.checked = checked;
  }
  for (const [key, left, top] of state.scrollRegions || []) {
    const item = [...root.querySelectorAll("[data-scroll-key]")].find((el) => el.dataset.scrollKey === key);
    if (item) { item.scrollLeft = left; item.scrollTop = top; }
  }
  if (state.focusKey) {
    const active = [...root.querySelectorAll("[data-focus-key], [id]")].find((el) => (el.dataset.focusKey || el.id) === state.focusKey);
    if (active) {
      active.focus({ preventScroll: true });
      if (state.selection && active instanceof HTMLInputElement) active.setSelectionRange(...state.selection);
    }
  }
  if (state.selectedText?.anchor && state.selectedText?.focus) {
    const atPath = (path) => path.reduce((node, index) => node?.childNodes[index], root);
    const anchor = atPath(state.selectedText.anchor), focus = atPath(state.selectedText.focus);
    if (anchor && focus) {
      const selection = window.getSelection();
      try {
        selection.removeAllRanges();
        selection.setBaseAndExtent(anchor, Math.min(state.selectedText.anchorOffset, anchor.length ?? anchor.childNodes.length), focus, Math.min(state.selectedText.focusOffset, focus.length ?? focus.childNodes.length));
      } catch (_) { selection.removeAllRanges(); }
    }
  }
  window.scrollTo({ left: state.scrollX, top: state.scrollY, behavior: "instant" });
}

export function safeJSON(value) {
  try { return JSON.stringify(value, null, 2); } catch (_) { return "Unavailable"; }
}
