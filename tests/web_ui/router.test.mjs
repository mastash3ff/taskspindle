import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const source = await readFile(new URL("../../src/taskspindle/web/static/router.js", import.meta.url), "utf8");
const { parseHash, routeHref } = await import(`data:text/javascript,${encodeURIComponent(source)}`);

test("routes preserve task ids, filters, and the providers alias", () => {
  const detail = parseHash("#/tasks/task%2F17?state=RUNNING&q=a%20b");
  assert.equal(detail.name, "tasks");
  assert.equal(detail.id, "task/17");
  assert.equal(detail.query.get("state"), "RUNNING");
  assert.equal(detail.query.get("q"), "a b");
  assert.equal(parseHash("#/providers").name, "workers");
});

test("routeHref encodes task identity and query state", () => {
  assert.equal(routeHref("tasks", "task/17", { state: "RESULT_READY" }), "#/tasks/task%2F17?state=RESULT_READY");
});
