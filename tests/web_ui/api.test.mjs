import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../../src/taskspindle/web/static/api.js", import.meta.url), "utf8");
const { clearCache, getJSON, postJSON } = await import(`data:text/javascript,${encodeURIComponent(source)}`);

test("GET refresh failure retains the last successful projection", async () => {
  clearCache();
  let fail = false;
  globalThis.fetch = async () => fail ? Promise.reject(new Error("offline")) : new Response('{"value":7}', { status: 200 });
  assert.deepEqual(await getJSON("/api/example", { fresh: true }), { data: { value: 7 }, cached: false, stale: false });
  fail = true;
  const fallback = await getJSON("/api/example", { fresh: true });
  assert.deepEqual(fallback.data, { value: 7 });
  assert.equal(fallback.cached, true);
  assert.equal(fallback.stale, true);
});

test("subscription POST sends JSON and the supplied CSRF token", async () => {
  let request;
  globalThis.fetch = async (path, options) => { request = { path, options }; return new Response('{"job":{"status":"queued"}}', { status: 202 }); };
  await postJSON("/api/subscriptions/claude/refresh", {}, "csrf-value");
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.headers["X-TaskSpindle-CSRF"], "csrf-value");
  assert.equal(request.options.body, "{}");
});
