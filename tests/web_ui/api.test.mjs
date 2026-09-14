import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../../src/taskspindle/web/static/api.js", import.meta.url), "utf8");
const { clearCache, getJSON, putJSON } = await import(`data:text/javascript,${encodeURIComponent(source)}`);

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

test("putJSON sends the CSRF header and same-origin credentials, and clears the policy cache", async () => {
  clearCache();
  let captured = null;
  globalThis.fetch = async () => new Response('{"revision":8}', { status: 200 });
  await getJSON("/api/policy", { fresh: true }); // populate the cache so the clear-on-write can be observed below

  globalThis.fetch = async (path, init) => {
    captured = { path, init };
    return new Response(JSON.stringify({ revision: 9 }), { status: 200 });
  };
  const result = await putJSON("/api/policy", { if_revision: 8, policy: { version: 1 } }, "csrf-token-1");
  assert.equal(result.revision, 9);
  assert.equal(captured.path, "/api/policy");
  assert.equal(captured.init.method, "PUT");
  assert.equal(captured.init.headers["X-TaskSpindle-CSRF"], "csrf-token-1");
  assert.equal(captured.init.headers["Content-Type"], "application/json");
  assert.equal(captured.init.cache, "no-store");
  assert.equal(captured.init.credentials, "same-origin");
  assert.deepEqual(JSON.parse(captured.init.body), { if_revision: 8, policy: { version: 1 } });

  globalThis.fetch = async () => new Response('{"revision":8,"marker":"stale-cache-check"}', { status: 200 });
  const refetched = await getJSON("/api/policy", { fresh: true });
  assert.equal(refetched.data.marker, "stale-cache-check");
});

test("putJSON surfaces server errors as APIError with status and payload", async () => {
  clearCache();
  globalThis.fetch = async () => new Response(JSON.stringify({ error: "POLICY_REVISION_CONFLICT", current_revision: 11 }), { status: 409 });
  await assert.rejects(
    () => putJSON("/api/policy", { if_revision: 8, policy: {} }, "csrf-token-1"),
    (error) => {
      assert.equal(error.name, "APIError");
      assert.equal(error.status, 409);
      assert.equal(error.payload.current_revision, 11);
      return true;
    },
  );
});
