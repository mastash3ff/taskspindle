"use strict";

(function () {
  var APP = document.getElementById("app");
  var REFRESHED = document.getElementById("refreshed");
  var pollTimer = null;

  var TASK_STATES = [
    "PREPARING", "QUEUED", "RUNNING", "COMPLETED", "RESULT_READY", "ACCEPTING",
    "ACCEPTED", "REJECTED", "REPAIRING", "RESUMING", "INTERRUPTED",
    "RECOVERY_AMBIGUOUS", "CANCELLING", "CANCELLED", "FAILED",
  ];
  var TASK_MODES = ["consult", "review", "implement"];
  var GROUP_BY_OPTIONS = ["provider", "day", "provider_day", "model", "mode", "repository_id"];

  var taskFilters = { state: "", provider: "", mode: "", q: "" };
  var searchTimer = null;
  var taskRequest = 0;
  var subscriptionRequest = 0;
  var subscriptionCsrf = null;
  var pendingSubscriptionActions = {};
  var usageFilters = { since: "7d", group_by: "provider", provider: "" };
  var SUBSCRIPTION_WORKERS = { claude: "claude", grok: "grok", google_ai: "agy" };

  // -- small DOM helpers ------------------------------------------------------------

  function h(tag, props, children) {
    var node = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach(function (key) {
        var value = props[key];
        if (value == null) return;
        if (key === "class") node.className = value;
        else node.setAttribute(key, value);
      });
    }
    var list = arguments.length > 2 ? Array.prototype.slice.call(arguments, 2) : [];
    list.forEach(appendChild);
    function appendChild(child) {
      if (child == null || child === false) return;
      if (Array.isArray(child)) {
        child.forEach(appendChild);
        return;
      }
      node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function clearNode(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function badge(text, cls) {
    return h("span", { class: "badge " + (cls || text) }, text);
  }

  function shortSha(sha) {
    return sha ? sha.slice(0, 10) : "-";
  }

  function textOrDash(value) {
    return value == null || value === "" ? "-" : String(value);
  }

  function kv(pairs) {
    var dl = h("dl", { class: "kv" });
    pairs.forEach(function (pair) {
      dl.appendChild(h("dt", null, pair[0]));
      dl.appendChild(h("dd", null, textOrDash(pair[1])));
    });
    return dl;
  }

  function tableOf(columns, rows, rowToCells) {
    var scroll = h("div", { class: "table-scroll" });
    var table = h("table");
    var headRow = h("tr");
    columns.forEach(function (c) { headRow.appendChild(h("th", null, c)); });
    table.appendChild(h("thead", null, headRow));
    var tbody = h("tbody");
    rows.forEach(function (row) {
      var tr = h("tr");
      rowToCells(row).forEach(function (cell) { tr.appendChild(h("td", null, cell)); });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scroll.appendChild(table);
    return scroll;
  }

  function selectWithAll(label, value, options, onChange) {
    var select = document.createElement("select");
    var all = document.createElement("option");
    all.value = "";
    all.textContent = "all " + label;
    select.appendChild(all);
    options.forEach(function (opt) {
      var o = document.createElement("option");
      o.value = opt;
      o.textContent = opt;
      select.appendChild(o);
    });
    select.value = value || "";
    select.addEventListener("change", function () { onChange(select.value); });
    return select;
  }

  function plainSelect(value, options, onChange) {
    var select = document.createElement("select");
    options.forEach(function (opt) {
      var o = document.createElement("option");
      o.value = opt;
      o.textContent = opt;
      select.appendChild(o);
    });
    select.value = value;
    select.addEventListener("change", function () { onChange(select.value); });
    return select;
  }

  function textInput(placeholder, value, onChange) {
    var input = h("input", { type: "text", placeholder: placeholder, value: value || "" });
    input.addEventListener("change", function () { onChange(input.value.trim()); });
    return input;
  }

  function showError(err) {
    var box = h("div", { class: "panel error" }, "Error: " + (err && err.message ? err.message : err));
    APP.insertBefore(box, APP.firstChild);
    setTimeout(function () { box.remove(); }, 6000);
  }

  function setRefreshed() {
    REFRESHED.textContent = "updated " + new Date().toLocaleTimeString();
  }

  function getJSON(url) {
    return fetch(url, { cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json" } }).then(function (resp) {
      return resp.json().catch(function () { return null; }).then(function (body) {
        if (!resp.ok) {
          var message = (body && body.error) || (resp.status + " " + resp.statusText);
          throw new Error(message);
        }
        return body;
      });
    });
  }

  // -- routing ------------------------------------------------------------------------

  function parseRoute() {
    var hash = location.hash.replace(/^#/, "") || "/tasks";
    var parts = hash.split("/").filter(Boolean);
    if (parts[0] === "tasks" && parts[1]) return { name: "task", id: decodeURIComponent(parts[1]) };
    if (parts[0] === "providers") return { name: "providers" };
    if (parts[0] === "subscriptions") return { name: "subscriptions" };
    if (parts[0] === "usage") return { name: "usage" };
    return { name: "tasks" };
  }

  function highlightNav(route) {
    var active = route.name === "task" ? "tasks" : route.name;
    document.querySelectorAll(".tabs a").forEach(function (a) {
      a.classList.toggle("active", a.dataset.route === active);
    });
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function startPolling(intervalMs, fn) {
    stopPolling();
    pollTimer = setInterval(function () {
      if (document.hidden) return;
      fn().catch(showError);
    }, intervalMs);
  }

  function navigate() {
    taskRequest += 1;
    clearTimeout(searchTimer);
    var route = parseRoute();
    highlightNav(route);
    stopPolling();
    var run;
    if (route.name === "task") {
      run = renderTaskDetail(route.id).then(function () {
        startPolling(5000, function () { return renderTaskDetail(route.id); });
      });
    } else if (route.name === "providers") {
      run = renderProviders().then(function () { startPolling(30000, renderProviders); });
    } else if (route.name === "subscriptions") {
      run = renderSubscriptions().then(function () { startPolling(15000, renderSubscriptions); });
    } else if (route.name === "usage") {
      run = renderUsage().then(function () { startPolling(30000, renderUsage); });
    } else {
      run = renderTasks().then(function () { startPolling(5000, renderTasks); });
    }
    run.catch(showError);
  }

  window.addEventListener("hashchange", navigate);
  document.addEventListener("DOMContentLoaded", navigate);

  // -- tasks list -----------------------------------------------------------------

  function renderTasks() {
    var request = ++taskRequest;
    var params = new URLSearchParams();
    if (taskFilters.state) params.set("state", taskFilters.state);
    if (taskFilters.provider) params.set("provider", taskFilters.provider);
    if (taskFilters.mode) params.set("mode", taskFilters.mode);
    if (taskFilters.q) params.set("q", taskFilters.q);
    params.set("limit", "200");
    return getJSON("/api/tasks?" + params.toString()).then(function (data) {
      if (request !== taskRequest || parseRoute().name !== "tasks") return;
      var active = document.activeElement;
      var cursor = active && active.id === "task-search" ? active.selectionStart : null;
      clearNode(APP);
      var panel = h("div", { class: "panel" });
      panel.appendChild(h("h1", null, "Tasks"));
      panel.appendChild(renderTaskFilters());
      panel.appendChild(renderTaskTable(data.tasks));
      APP.appendChild(panel);
      if (cursor !== null) {
        var search = document.getElementById("task-search");
        search.focus();
        search.setSelectionRange(cursor, cursor);
      }
      setRefreshed();
    });
  }

  function renderTaskFilters() {
    var wrap = h("div", { class: "filters" });
    var search = h("input", { id: "task-search", type: "search", placeholder: "Search ID or prompt",
      "aria-label": "Search task ID or prompt" });
    search.value = taskFilters.q;
    search.addEventListener("input", function () {
      taskFilters.q = search.value;
      taskRequest += 1;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(function () { renderTasks().catch(showError); }, 250);
    });
    wrap.appendChild(search);
    wrap.appendChild(selectWithAll("states", taskFilters.state, TASK_STATES, function (v) {
      taskFilters.state = v;
      renderTasks().catch(showError);
    }));
    wrap.appendChild(selectWithAll("modes", taskFilters.mode, TASK_MODES, function (v) {
      taskFilters.mode = v;
      renderTasks().catch(showError);
    }));
    wrap.appendChild(textInput("provider", taskFilters.provider, function (v) {
      taskFilters.provider = v;
      renderTasks().catch(showError);
    }));
    return wrap;
  }

  function stateClass(state) {
    if (state === "COMPLETED" || state === "ACCEPTED") return "ok";
    if (state === "FAILED" || state === "REJECTED" || state === "CANCELLED") return "fail";
    if (state === "RECOVERY_AMBIGUOUS" || state === "INTERRUPTED") return "warn";
    return "unknown";
  }

  function renderTaskTable(tasks) {
    var wrap = h("div", { class: "panel" });
    var columns = ["id", "state", "provider", "mode", "created", "updated", "warnings", "sha"];
    wrap.appendChild(tableOf(columns, tasks, function (t) {
      return [
        h("a", { href: "#/tasks/" + encodeURIComponent(t.id) }, t.id),
        badge(t.state, stateClass(t.state)),
        t.provider,
        t.mode,
        t.created_at,
        t.updated_at,
        String((t.warnings || []).length),
        shortSha(t.candidate_sha),
      ];
    }));
    if (!tasks.length) wrap.appendChild(h("p", { class: "muted" }, "no tasks"));
    return wrap;
  }

  // -- task detail ------------------------------------------------------------------

  function renderTaskDetail(id) {
    var request = ++taskRequest;
    return getJSON("/api/tasks/" + encodeURIComponent(id)).then(function (data) {
      if (request !== taskRequest || parseRoute().id !== id) return;
      var task = data.task;
      var key = [id, task.candidate_sha, task.candidate_revision].join(":");
      var prior = APP.querySelector("[data-candidate]");
      var priorDiff = prior && prior.dataset.candidate === key && prior.querySelector(".candidate-diff");
      var scroll = priorDiff ? { top: priorDiff.scrollTop, left: priorDiff.scrollLeft } : null;
      var focused = priorDiff && priorDiff.contains(document.activeElement) ? document.activeElement.id : null;
      clearNode(APP);
      var top = h("div", { class: "panel" });
      top.appendChild(h("h1", null, "Task " + task.id));
      top.appendChild(h("p", null, h("a", { href: "#/tasks" }, "← back to tasks")));
      top.appendChild(renderTaskHeader(task, data.repository));
      APP.appendChild(top);
      APP.appendChild(renderEvents(data.events));
      APP.appendChild(renderTurns(data.turns));
      APP.appendChild(renderChecksPanel(data.checks));
      var comparison = h("div", { "data-candidate": key });
      APP.appendChild(comparison);
      APP.appendChild(renderWarnings(task.warnings));
      APP.appendChild(renderWorkerLog(data.worker_log));
      setRefreshed();
      return renderDiffSection(task).then(function (diffPanel) {
        if (request !== taskRequest || !comparison.isConnected) return;
        comparison.appendChild(diffPanel);
        var diff = diffPanel.querySelector(".candidate-diff");
        if (diff) comparison.className = "candidate-review";
        comparison.appendChild(renderReview(data.review, diffPanel));
        if (diff && scroll) {
          diff.scrollTop = scroll.top;
          diff.scrollLeft = scroll.left;
          var target = focused && document.getElementById(focused);
          if (target) target.focus({ preventScroll: true });
        }
      });
    });
  }

  function renderTaskHeader(task, repository) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Overview"));
    wrap.appendChild(kv([
      ["state", task.state],
      ["cleanup_state", task.cleanup_state],
      ["provider", task.provider],
      ["auth_mode", task.auth_mode],
      ["mode", task.mode],
      ["state_version", task.state_version],
      ["candidate_revision", task.candidate_revision],
      ["candidate_sha", task.candidate_sha],
      ["repository", repository ? (repository.display_path || repository.common_dir) : task.repository_id],
      ["worktree_path", task.worktree_path],
      ["branch", task.branch],
      ["session_id", task.session_id],
      ["requested_model", task.requested_model],
      ["reported_model", task.reported_model],
      ["created_at", task.created_at],
      ["updated_at", task.updated_at],
      ["started_at", task.started_at],
      ["finished_at", task.finished_at],
    ]));
    if (task.error) {
      wrap.appendChild(h("h3", null, "Error"));
      wrap.appendChild(kv([
        ["code", task.error.code],
        ["message", task.error.message],
        ["retryable", task.error.retryable],
      ]));
    }
    return wrap;
  }

  function renderEvents(events) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Events"));
    wrap.appendChild(tableOf(["at", "kind", "payload"], events, function (e) {
      return [e.at, e.kind, e.payload ? JSON.stringify(e.payload) : "-"];
    }));
    if (!events.length) wrap.appendChild(h("p", { class: "muted" }, "no events"));
    return wrap;
  }

  function renderTurns(turns) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Turns"));
    if (!turns.length) wrap.appendChild(h("p", { class: "muted" }, "no turns"));
    turns.forEach(function (t) {
      var box = h("div", { class: "panel" });
      box.appendChild(h("h3", null, "revision " + t.revision + " (" + t.kind + ")"));
      box.appendChild(kv([
        ["started_at", t.started_at],
        ["ended_at", t.ended_at],
        ["stop_reason", t.stop_reason],
        ["session_id", t.session_id],
      ]));
      if (t.usage) {
        box.appendChild(h("h4", null, "usage"));
        box.appendChild(kv([
          ["model", t.usage.model],
          ["input_tokens", t.usage.input_tokens],
          ["output_tokens", t.usage.output_tokens],
          ["cache_read_tokens", t.usage.cache_read_tokens],
          ["cache_write_tokens", t.usage.cache_write_tokens],
          ["cost_estimate_usd", t.usage.cost_estimate_usd],
          ["duration_ms", t.usage.duration_ms],
        ]));
      }
      if (t.response) {
        box.appendChild(h("h4", null, "response"));
        box.appendChild(h("pre", null, t.response));
      }
      var transcript = t.transcript;
      if (transcript) {
        if (transcript.text) {
          box.appendChild(h("h4", null, "text"));
          box.appendChild(h("pre", null, transcript.text));
        }
        if (transcript.tool_calls && transcript.tool_calls.length) {
          box.appendChild(h("h4", null, "tool calls"));
          box.appendChild(tableOf(["title", "kind", "update", "status"], transcript.tool_calls, function (c) {
            return [c.title || c.name || "-", c.kind || "-", c.update || "-", c.status || "-"];
          }));
        }
        if (transcript.violations && transcript.violations.length) {
          box.appendChild(h("h4", null, "violations"));
          box.appendChild(h("pre", null, JSON.stringify(transcript.violations, null, 2)));
        }
        if (transcript.permission_events && transcript.permission_events.length) {
          box.appendChild(h("h4", null, "permission events"));
          box.appendChild(h("pre", null, JSON.stringify(transcript.permission_events, null, 2)));
        }
      }
      wrap.appendChild(box);
    });
    return wrap;
  }

  function renderChecksPanel(checks) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Checks"));
    wrap.appendChild(tableOf(["command", "ok", "exit_code", "duration_ms"], checks, function (c) {
      return [c.command, badge(c.ok ? "ok" : "fail", c.ok ? "ok" : "fail"), c.exit_code, c.duration_ms];
    }));
    if (!checks.length) wrap.appendChild(h("p", { class: "muted" }, "no checks"));
    return wrap;
  }

  function renderReview(review, diffPanel) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Review"));
    if (!review) {
      wrap.appendChild(h("p", { class: "muted" }, "no review for this candidate"));
      return wrap;
    }
    wrap.appendChild(kv([
      ["verdict", review.verdict],
      ["provider", review.provider],
      ["summary", review.summary],
      ["candidate_sha", review.candidate_sha],
    ]));
    if (review.findings && review.findings.length) {
      wrap.appendChild(tableOf(
        ["id", "severity", "path", "line", "evidence", "remedy"],
        review.findings,
        function (f) {
          var location = f.path;
          var target = diffPanel && diffPanel.querySelector('[id="' + diffAnchor(f.path, f.line) + '"]');
          if (target) {
            location = h("a", { href: "#" + target.id }, f.path);
            location.addEventListener("click", function (event) {
              event.preventDefault();
              target.scrollIntoView({ block: "center" });
              target.focus({ preventScroll: true });
            });
          }
          return [f.id, f.severity, location, f.line, f.evidence, f.remedy];
        }
      ));
    }
    return wrap;
  }

  function renderWarnings(warnings) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Warnings"));
    if (!warnings || !warnings.length) {
      wrap.appendChild(h("p", { class: "muted" }, "none"));
      return wrap;
    }
    var ul = h("ul");
    warnings.forEach(function (w) { ul.appendChild(h("li", null, w)); });
    wrap.appendChild(ul);
    return wrap;
  }

  function renderWorkerLog(text) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Worker log (tail)"));
    if (!text) {
      wrap.appendChild(h("p", { class: "muted" }, "no log"));
      return wrap;
    }
    wrap.appendChild(h("pre", null, text));
    return wrap;
  }

  function diffAnchor(path, line) {
    return "diff-" + encodeURIComponent(path || "").replace(/'/g, "%27") + "-" + String(line || "file");
  }

  function renderDiffLines(text) {
    var pre = h("pre", { class: "candidate-diff" });
    var path = "", line = null;
    text.split("\n").forEach(function (value) {
      var node = h("span", { class: "diff-line", tabindex: "-1" }, value + "\n");
      if (value.indexOf("+++ b/") === 0) {
        path = value.slice(6);
        node.id = diffAnchor(path, null);
        line = null;
      } else if (value.indexOf("diff --git ") === 0) {
        path = ""; line = null;
      } else {
        var hunk = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(value);
        if (hunk) line = Number(hunk[1]);
        else if (path && line !== null && /^[ +]/.test(value)) {
          node.id = diffAnchor(path, line);
          line += 1;
        }
      }
      pre.appendChild(node);
    });
    return pre;
  }

  function renderDiffSection(task) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Diff"));
    var query = new URLSearchParams({ revision: task.candidate_revision });
    if (task.candidate_sha) query.set("candidate_sha", task.candidate_sha);
    return fetch("/api/tasks/" + encodeURIComponent(task.id) + "/diff?" + query).then(function (resp) {
      if (resp.status === 409) {
        wrap.appendChild(h("p", { class: "muted" }, "Candidate changed. Refresh this task to compare its current diff and review."));
        return wrap;
      }
      if (resp.status === 404) {
        wrap.appendChild(h("p", { class: "muted" }, "no diff for this task"));
        return wrap;
      }
      if (!resp.ok) throw new Error(resp.status + " " + resp.statusText);
      return resp.text().then(function (text) {
        var lines = text ? text.split("\n").length : 0;
        wrap.appendChild(h("p", { class: "muted" }, lines + " lines"));
        wrap.appendChild(renderDiffLines(text));
        return wrap;
      });
    }).catch(function (err) {
      wrap.appendChild(h("p", { class: "error" }, "diff error: " + err.message));
      return wrap;
    });
  }

  // -- providers ----------------------------------------------------------------------

  function renderProviders() {
    return getJSON("/api/providers").then(function (data) {
      clearNode(APP);
      var panel = h("div", { class: "panel" });
      panel.appendChild(h("h1", null, "Providers"));
      panel.appendChild(tableOf(
        ["id", "family", "auth", "first_class", "model", "state", "reset_at", "suggested_alternative"],
        data.providers,
        function (p) {
          return [
            p.id, p.family, p.auth, p.first_class ? "yes" : "no", p.model || "-",
            badge(p.availability.state, p.availability.state),
            p.availability.reset_at || "-",
            p.availability.suggested_alternative || "-",
          ];
        }
      ));
      APP.appendChild(panel);

      var windowRows = [];
      data.providers.forEach(function (p) {
        p.windows.forEach(function (w) { windowRows.push({ provider: p.id, window: w }); });
      });
      var windowsPanel = h("div", { class: "panel" });
      windowsPanel.appendChild(h("h2", null, "Windows"));
      windowsPanel.appendChild(tableOf(
        ["provider", "window", "status", "used_percent", "resets_at", "observed_at"],
        windowRows,
        function (r) {
          return [
            r.provider, r.window.window, r.window.status || "-",
            r.window.used_percent == null ? "-" : r.window.used_percent,
            r.window.resets_at || "-", r.window.observed_at,
          ];
        }
      ));
      if (!windowRows.length) windowsPanel.appendChild(h("p", { class: "muted" }, "no window observations"));
      APP.appendChild(windowsPanel);

      APP.appendChild(renderDoctor(data.doctor));
      setRefreshed();
    });
  }

  function renderDoctorChecks(doctor) {
    var list = h("ul");
    if (!doctor || !doctor.checks) return list;
    doctor.checks.forEach(function (c) {
      var mark = c.ok ? "[ok]" : (c.advisory ? "[warn]" : "[FAIL]");
      list.appendChild(h("li", null, mark + " " + c.name + ": " + c.detail));
    });
    return list;
  }

  function renderDoctor(doctor) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Doctor"));
    var btn = h("button", { type: "button" }, "run live probes");
    btn.addEventListener("click", function () {
      btn.disabled = true;
      var original = btn.textContent;
      btn.textContent = "running…";
      getJSON("/api/doctor?live=1").then(function (result) {
        var old = wrap.querySelector("ul");
        var fresh = renderDoctorChecks(result);
        if (old) wrap.replaceChild(fresh, old);
        else wrap.appendChild(fresh);
      }).catch(showError).then(function () {
        btn.disabled = false;
        btn.textContent = original;
      });
    });
    wrap.appendChild(btn);
    wrap.appendChild(renderDoctorChecks(doctor));
    return wrap;
  }

  // -- subscriptions ------------------------------------------------------------------

  function friendlyBillingDate(value, precision) {
    if (!value) return "-";
    if (precision === "date" && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
      var parts = value.split("-").map(Number);
      return new Intl.DateTimeFormat(undefined, {
        year: "numeric", month: "short", day: "numeric", timeZone: "UTC",
      }).format(new Date(Date.UTC(parts[0], parts[1] - 1, parts[2])));
    }
    var parsed = new Date(value);
    return isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  }

  function remainingText(days) {
    if (days == null) return "";
    if (days < 0) return "";
    if (days === 0) return " — today";
    return " — " + days + (Math.abs(days) === 1 ? " day remaining" : " days remaining");
  }

  function subscriptionState(row) {
    var errorCode = row.error && row.error.code;
    if (errorCode === "SETUP_REQUIRED") {
      return { text: "Chrome setup required", className: "warn" };
    }
    if (errorCode === "AUTH_REQUIRED" || errorCode === "ACCOUNT_MISMATCH") {
      return { text: "Reconnect required", className: "warn" };
    }
    if (errorCode === "UNSUPPORTED_BILLING_CHANNEL") {
      return { text: "Unsupported billing channel", className: "warn" };
    }
    if (row.error && !row.last_success_at) return { text: "Verification failed", className: "fail" };
    if (row.status === "renewing") {
      return {
        text: row.renews_at ?
          "Renews on " + friendlyBillingDate(row.renews_at, row.date_precision) : "Renewing",
        className: "ok",
      };
    }
    if (row.status === "cancelled") {
      return {
        text: row.access_ends_at ?
          "Cancelled — access ends " + friendlyBillingDate(row.access_ends_at, row.date_precision) +
            remainingText(row.days_remaining) : "Cancelled",
        className: "warn",
      };
    }
    if (row.status === "expired") return { text: "Expired", className: "fail" };
    if (row.status === "free") return { text: "Free", className: "unknown" };
    if (row.status === "none") return { text: "No subscription", className: "unknown" };
    return { text: "Unavailable", className: "unknown" };
  }

  function operationState(row) {
    var operation = pendingSubscriptionActions[row.provider] || row.operation;
    if (!operation) return null;
    return {
      action: operation.action,
      status: String(operation.status || "queued").toLowerCase(),
      origin: operation.origin || "manual",
    };
  }

  function operationMessage(operation) {
    if (operation.status === "requesting") return "Submitting the request…";
    if (operation.origin === "scheduled") {
      return operation.status === "running" ?
        "A scheduled subscription check is running." : "A scheduled subscription check is queued.";
    }
    if (operation.action === "connect") {
      if (operation.status === "running") {
        return "Chrome verification is running. Connection state updates only after verification succeeds.";
      }
      return "Connect queued — your normal Chrome profile will open for verification.";
    }
    return operation.status === "running" ? "Subscription refresh is running." : "Refresh queued.";
  }

  function workerState(availability) {
    var states = {
      ok: { text: "Available", className: "ok" },
      unknown: { text: "Not verified", className: "unknown" },
      throttled: { text: "Rate limited", className: "warn" },
      auth_expired: { text: "Sign-in required", className: "fail" },
      access_denied: { text: "Access denied", className: "fail" },
      model_unavailable: { text: "Model unavailable", className: "warn" },
    };
    return states[availability && availability.state] || states.unknown;
  }

  function workerSource(source) {
    var sources = {
      task_success: "Successful worker task",
      turn_ok: "Successful worker task",
      native_auth_check: "Native CLI authentication check",
      rate_limit_event: "Provider quota report",
      acp_prompt_response: "Worker response",
      acp_error: "Worker refusal",
      worker_error: "Worker refusal",
    };
    return sources[source] || (source ? "Worker status record" : "No worker evidence");
  }

  function workerVerification(availability) {
    if (!availability) return "Unavailable";
    if (availability.stale) return "Stale observation";
    if (availability.state === "ok") return "Confirmed by a successful worker task";
    if (availability.state === "unknown") return "No current evidence";
    return "Current refusal";
  }

  function workerScope(availability, worker) {
    if (!availability || !availability.scope) return "No active refusal";
    if (["ok", "unknown"].includes(availability.state) && !availability.stale) {
      return "No active refusal";
    }
    if (availability.scope === "account") return "Worker profile/account";
    if (availability.scope === "model") {
      return "Model · " + textOrDash(availability.affected_model || worker.model || "selected model");
    }
    return "Provider";
  }

  function workerNextAction(availability, worker) {
    var action = availability && availability.next_action;
    if (action === "start") return "Eligible for a new task.";
    if (action === "retry") return "Retry a worker task to verify access.";
    if (action === "wait") {
      return availability.reset_at ?
        "Wait until " + friendlyBillingDate(availability.reset_at, "datetime") + ", then retry." :
        "Wait for the provider limit to reset, then retry.";
    }
    if (action === "sign_in") return "Sign in to the " + worker.id + " worker CLI, then retry a task.";
    if (action === "review_access") return "Review this worker profile's access, then retry a task.";
    if (action === "choose_model") return "Choose another model for this worker profile, then retry.";
    return "Run a worker task to establish current access.";
  }

  function renderModelAvailability(worker, availability) {
    var selectedModel = availability.scope === "model" ? availability.affected_model : null;
    var observations = (worker.model_availability || []).filter(function (observation) {
      return observation && observation.affected_model && observation.affected_model !== selectedModel;
    });
    if (!observations.length) return null;
    var wrap = h("div", { class: "model-availability" });
    wrap.appendChild(h("h4", null, "Model-specific status"));
    observations.forEach(function (observation) {
      var state = workerState(observation);
      var row = h("div", { class: "model-status-row" },
        h("div", { class: "worker-heading" },
          h("strong", null, observation.affected_model), badge(state.text, state.className)
        ),
        h("p", { class: "muted" },
          workerSource(observation.source), " · observed ",
          friendlyBillingDate(observation.observed_at, "datetime")
        ),
        h("p", null, workerNextAction(observation, worker))
      );
      wrap.appendChild(row);
    });
    return wrap;
  }

  function renderWorkerAccess(row, providerData) {
    var section = h("section", { class: "worker-access" });
    section.appendChild(h("h3", null, "Worker access"));
    if (row.provider === "chatgpt") {
      section.appendChild(h("div", { class: "worker-heading" },
        badge("Coordinator only", "unknown")
      ));
      section.appendChild(h("p", { class: "muted" },
        "ChatGPT is used by the Codex coordinator. It has no TaskSpindle worker profile."
      ));
      return section;
    }

    var workerId = SUBSCRIPTION_WORKERS[row.provider];
    var providers = providerData && providerData.providers || [];
    var worker = providers.find(function (candidate) { return candidate.id === workerId; });
    if (!worker) {
      section.appendChild(badge(providerData ? "Not configured" : "Status unavailable", "unknown"));
      section.appendChild(h("p", { class: "muted" }, providerData ?
        "The associated worker profile is not configured." :
        "Worker status could not be loaded. Subscription details remain independent."
      ));
      return section;
    }

    var availability = worker.availability || {};
    var state = workerState(availability);
    section.appendChild(h("div", { class: "worker-heading" },
      h("strong", null, worker.id), badge(state.text, state.className)
    ));
    section.appendChild(kv([
      ["Profile", worker.id],
      ["Model", worker.model || "Provider default"],
      ["Refusal scope", workerScope(availability, worker)],
      ["Verification", workerVerification(availability)],
      ["Last worker success", friendlyBillingDate(availability.last_success_at, "datetime")],
      ["Status observed", friendlyBillingDate(availability.observed_at, "datetime")],
      ["Source", workerSource(availability.source)],
    ]));
    section.appendChild(h("p", { class: "worker-next-action" }, workerNextAction(availability, worker)));
    var modelStatus = renderModelAvailability(worker, availability);
    if (modelStatus) section.appendChild(modelStatus);
    if (row.provider === "google_ai") {
      section.appendChild(h("p", { class: "muted" },
        "Product association only: the agy worker is shown with Google AI. Its account is not bound to the browser subscription account."
      ));
    }
    section.appendChild(h("p", { class: "muted" },
      "Browser subscription and worker CLI accounts are not assumed to match."
    ));
    return section;
  }

  function subscriptionActionButton(row, action, operation) {
    var reconnect = action === "connect" && row.connected;
    var label = action === "connect" ? (reconnect ? "Reconnect" : "Connect") : "Refresh now";
    var running = operation && operation.action === action;
    if (running && operation.status === "requesting") {
      label = "Requesting…";
    } else if (running && operation.status === "running") {
      label = action === "connect" ? (reconnect ? "Reconnecting…" : "Connecting…") : "Refreshing…";
    } else if (running) {
      label += " queued";
    }
    var button = h("button", {
      type: "button",
      "aria-label": label + " " + (row.label || row.provider),
      "data-subscription-provider": row.provider,
      "data-subscription-action": action,
    }, label);
    button.disabled = Boolean(operation) || (action === "refresh" && !row.connected);
    button.addEventListener("click", function () {
      requestSubscriptionAction(row.provider, action, button).catch(showError);
    });
    return button;
  }

  function renderSubscriptionCard(row, providerData) {
    var state = subscriptionState(row);
    var operation = operationState(row);
    var card = h("section", { class: "panel subscription-card" });
    card.appendChild(h("div", { class: "subscription-heading" },
      h("h2", null, row.label || row.provider),
      badge(state.text, state.className)
    ));
    card.appendChild(h("h3", { class: "card-section-title" }, "Subscription"));
    var billingLabels = {
      provider_web: "Provider website",
      apple: "Apple App Store",
      google_play: "Google Play",
      x_premium: "X Premium",
      unknown: "Unknown",
    };
    card.appendChild(kv([
      ["Account", row.account_label],
      ["Plan", row.plan],
      ["Billing", billingLabels[row.billing_channel]],
      ["Renews", friendlyBillingDate(row.renews_at, row.date_precision)],
      ["Access ends", friendlyBillingDate(row.access_ends_at, row.date_precision)],
    ]));

    var activity = h("div", { class: "subscription-activity" });
    activity.appendChild(h("p", null,
      h("span", { class: "muted" }, "Last verified: "),
      row.last_success_at ? friendlyBillingDate(row.last_success_at, "datetime") : "-"
    ));
    activity.appendChild(h("p", null,
      h("span", { class: "muted" }, "Last attempt: "),
      row.last_attempt_at ? friendlyBillingDate(row.last_attempt_at, "datetime") : "-"
    ));
    activity.appendChild(h("p", null,
      h("span", { class: "muted" }, "Source: "), "Browser subscription check"
    ));
    activity.appendChild(h("p", null,
      h("span", { class: "muted" }, "Verification: "),
      row.freshness === "fresh" ? "Current" :
        (row.freshness === "stale" ? "Stale" : "Not yet verified")
    ));
    if (row.error) {
      if (row.error.code === "SETUP_REQUIRED") {
        activity.appendChild(h("p", { class: "warning" },
          "Automatic verification needs the Playwright Chrome extension connection. " +
          "Existing verified details are retained."
        ));
        activity.appendChild(h("p", null,
          "Run ", h("code", null, "taskspindle subscriptions setup-extension"),
          " once, enter the private connection token only in its hidden prompt, then choose Connect again."
        ));
      } else {
        activity.appendChild(h("p", { class: "error" },
          "Verification failed: " + textOrDash(row.error.message)
        ));
      }
    } else {
      activity.appendChild(h("p", { class: "muted" }, "Error: none"));
    }
    if (row.freshness === "stale") {
      activity.appendChild(h("p", { class: "warning" },
        "Verification is stale. Refresh to confirm the current subscription."
      ));
    }
    if (row.end_passed_unverified) {
      activity.appendChild(h("p", { class: "warning" },
        "Verification needed — the recorded access end has passed. Refresh before treating this subscription as expired."
      ));
    } else if (row.upcoming_end_warning === "within_1_day") {
      activity.appendChild(h("p", { class: "warning" },
        "Access is scheduled to end within one day. Refresh now to confirm the deadline."
      ));
    } else if (row.upcoming_end_warning === "within_7_days") {
      activity.appendChild(h("p", { class: "warning" },
        "Access is scheduled to end within seven days. Refresh now to confirm the deadline."
      ));
    }
    if (operation) {
      activity.appendChild(h("p", { class: "operation", "aria-live": "polite" },
        operationMessage(operation)
      ));
    }
    card.appendChild(activity);
    card.appendChild(renderWorkerAccess(row, providerData));

    var actions = h("div", { class: "subscription-actions" });
    actions.appendChild(subscriptionActionButton(row, "connect", operation));
    actions.appendChild(subscriptionActionButton(row, "refresh", operation));
    card.appendChild(actions);
    return card;
  }

  function focusedSubscriptionControl() {
    var active = document.activeElement;
    if (!active || !active.dataset || !active.dataset.subscriptionProvider) return null;
    return {
      provider: active.dataset.subscriptionProvider,
      action: active.dataset.subscriptionAction,
    };
  }

  function restoreSubscriptionFocus(focused) {
    if (!focused) return;
    Array.prototype.some.call(document.querySelectorAll("[data-subscription-provider]"), function (control) {
      if (control.dataset.subscriptionProvider === focused.provider &&
          control.dataset.subscriptionAction === focused.action && !control.disabled) {
        control.focus({ preventScroll: true });
        return true;
      }
      return false;
    });
  }

  function renderSubscriptions() {
    var request = ++subscriptionRequest;
    var focused = focusedSubscriptionControl();
    var providerRequest = getJSON("/api/providers").catch(function () { return null; });
    return Promise.all([getJSON("/api/subscriptions"), providerRequest]).then(function (responses) {
      var data = responses[0];
      var providerData = responses[1];
      if (request !== subscriptionRequest || parseRoute().name !== "subscriptions") return;
      subscriptionCsrf = data.csrf_token;
      clearNode(APP);
      var heading = h("div", { class: "panel" });
      heading.appendChild(h("h1", null, "Subscriptions"));
      heading.appendChild(h("p", null,
        "Connect opens the provider billing page in your normal Chrome profile, where your saved passwords are available. ",
        "TaskSpindle marks the account connected only after it verifies the account and billing details."
      ));
      heading.appendChild(h("p", { class: "muted" },
        "One-time browser setup: ",
        h("a", {
          href: "https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm",
          target: "_blank",
          rel: "noopener noreferrer",
        }, "install the Playwright Chrome extension"),
        ", then run ",
        h("code", null, "taskspindle subscriptions setup-extension"),
        ". Enter the private connection token only in that command's hidden prompt."
      ));
      var collector = data.collector_running ? "Collector running" : "Collector unavailable";
      if (data.collector_last_seen_at) {
        collector += " · last seen " + friendlyBillingDate(data.collector_last_seen_at, "datetime");
      }
      heading.appendChild(h("p", { class: data.collector_running ? "muted" : "error" }, collector));
      heading.appendChild(h("p", { class: "schedule-state" },
        data.scheduled_refresh_enabled === true ?
          "Scheduled browser checks: On" : "Scheduled browser checks: Off (default)"
      ));
      heading.appendChild(h("p", { class: "muted" },
        "Use Connect or Refresh now to check billing."
      ));
      APP.appendChild(heading);
      var grid = h("div", { class: "subscription-grid" });
      data.subscriptions.forEach(function (row) { grid.appendChild(renderSubscriptionCard(row, providerData)); });
      APP.appendChild(grid);
      restoreSubscriptionFocus(focused);
      setRefreshed();
    });
  }

  function markSubscriptionPending(provider, action, button) {
    pendingSubscriptionActions[provider] = { provider: provider, action: action, status: "requesting" };
    Array.prototype.forEach.call(document.querySelectorAll("[data-subscription-provider]"), function (control) {
      if (control.dataset.subscriptionProvider === provider) control.disabled = true;
    });
    button.textContent = "Requesting…";
  }

  function requestSubscriptionAction(provider, action, button) {
    if (!subscriptionCsrf || pendingSubscriptionActions[provider]) return Promise.resolve();
    markSubscriptionPending(provider, action, button);
    return fetch("/api/subscriptions/" + encodeURIComponent(provider) + "/" + action, {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-TaskSpindle-CSRF": subscriptionCsrf,
      },
      body: "{}",
    }).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (body) {
        if (!response.ok) {
          var messages = {
            LOOPBACK_REQUIRED: "Open the dashboard on this computer to connect an account.",
            ORIGIN_REQUIRED: "Reload this page from the local dashboard and try again.",
            CSRF_INVALID: "Reload this page and try again.",
            ACTION_NOT_AVAILABLE: "Connect this account before refreshing it.",
            SUBSCRIPTION_UNAVAILABLE: "Subscription tracking is temporarily unavailable.",
          };
          throw new Error(messages[body && body.error] || "Unable to request a subscription check. Try again.");
        }
        pendingSubscriptionActions[provider] = body.job;
        return renderSubscriptions();
      });
    }).catch(function (error) {
      delete pendingSubscriptionActions[provider];
      return renderSubscriptions().then(function () { throw error; });
    }).then(function () {
      delete pendingSubscriptionActions[provider];
    });
  }

  // -- usage --------------------------------------------------------------------------

  function renderUsage() {
    var params = new URLSearchParams();
    if (usageFilters.since) params.set("since", usageFilters.since);
    params.set("group_by", usageFilters.group_by);
    if (usageFilters.provider) params.set("provider", usageFilters.provider);
    return getJSON("/api/usage?" + params.toString()).then(function (data) {
      clearNode(APP);
      var panel = h("div", { class: "panel" });
      panel.appendChild(h("h1", null, "Usage"));
      panel.appendChild(renderUsageControls());
      APP.appendChild(panel);
      APP.appendChild(renderUsageTable(data.usage));
      APP.appendChild(renderOutcomesTable(data.outcomes));
      APP.appendChild(renderTimingSummary(data.turns, data.checks));
      APP.appendChild(renderViolations(data.violations));
      APP.appendChild(renderWindowsNotes(data.windows));
      var note = h("div", { class: "panel" });
      note.appendChild(h("p", { class: "muted" }, data.cost_note));
      APP.appendChild(note);
      setRefreshed();
    });
  }

  function renderUsageControls() {
    var wrap = h("div", { class: "filters" });
    wrap.appendChild(textInput("since (7d, 24h, ISO-8601)", usageFilters.since, function (v) {
      usageFilters.since = v;
      renderUsage().catch(showError);
    }));
    wrap.appendChild(plainSelect(usageFilters.group_by, GROUP_BY_OPTIONS, function (v) {
      usageFilters.group_by = v;
      renderUsage().catch(showError);
    }));
    wrap.appendChild(textInput("provider", usageFilters.provider, function (v) {
      usageFilters.provider = v;
      renderUsage().catch(showError);
    }));
    return wrap;
  }

  function renderUsageTable(rows) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Token usage"));
    var keys = ["provider", "day", "model", "mode", "repository_id"].filter(function (k) {
      return rows.some(function (r) { return k in r; });
    });
    var columns = keys.concat([
      "turns", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_estimate_usd",
    ]);
    wrap.appendChild(tableOf(columns.map(function (c) {
      return c === "repository_id" ? "repository" : c;
    }), rows, function (r) {
      return columns.map(function (c) {
        if (c === "cost_estimate_usd") return r.priced_turns ? String(r[c]) : "-";
        if (c === "repository_id") return r.repository_path || r.repository_id || "No repository";
        return textOrDash(r[c]);
      });
    }));
    if (!rows.length) wrap.appendChild(h("p", { class: "muted" }, "nothing recorded"));
    return wrap;
  }

  function renderOutcomesTable(rows) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Outcomes"));
    wrap.appendChild(tableOf(["provider", "mode", "state", "count"], rows, function (r) {
      return [r.provider, r.mode, r.state, r.count];
    }));
    if (!rows.length) wrap.appendChild(h("p", { class: "muted" }, "nothing recorded"));
    return wrap;
  }

  function renderTimingSummary(turns, checks) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Timings"));
    wrap.appendChild(h("p", null,
      "turns: " + turns.count + " (mean " + turns.mean_ms + " ms, p50 " + turns.p50_ms + " ms); " +
      "checks: " + checks.passed + "/" + checks.count + " passed"));
    return wrap;
  }

  function renderViolations(rows) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Violations"));
    if (!rows.length) {
      wrap.appendChild(h("p", { class: "muted" }, "none"));
      return wrap;
    }
    wrap.appendChild(tableOf(["provider", "kind", "count"], rows, function (r) {
      return [r.provider, r.kind, r.count];
    }));
    return wrap;
  }

  function renderWindowsNotes(entries) {
    var wrap = h("div", { class: "panel" });
    wrap.appendChild(h("h2", null, "Window telemetry"));
    var ul = h("ul");
    entries.forEach(function (e) {
      ul.appendChild(h("li", null, e.provider + ": " + e.state + " — " + e.note));
    });
    wrap.appendChild(ul);
    return wrap;
  }
})();
