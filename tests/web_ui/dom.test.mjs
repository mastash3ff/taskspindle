import test from "node:test";
import assert from "node:assert/strict";

class FakeInput {}
class FakeTextArea {}
globalThis.HTMLInputElement = FakeInput;
globalThis.HTMLTextAreaElement = FakeTextArea;

const frames = [];
globalThis.requestAnimationFrame = (callback) => { frames.push(callback); };
const flushFrame = () => {
  const callbacks = frames.splice(0);
  callbacks.forEach((callback) => callback());
};

globalThis.window = {
  scrollX: 0,
  scrollY: 508,
  scrollTo({ left, top }) { this.scrollX = left; this.scrollY = top; },
  getSelection() { return null; },
};
globalThis.document = { activeElement: null };
globalThis.location = { hash: "#/workers" };

const { restoreViewState } = await import("../../src/taskspindle/web/static/dom.js");

test("poll restoration repins scroll after deferred focus and layout anchoring", () => {
  const copy = {
    dataset: { focusKey: "copy-recovery-fixture-model" },
    focus(options) {
      assert.equal(options.preventScroll, true);
      document.activeElement = this;
      requestAnimationFrame(() => { window.scrollY = 508; });
    },
  };
  const root = {
    firstChild: { id: "workers-view" }, isConnected: true,
    querySelectorAll(selector) {
      if (selector === "[data-focus-key]" || selector === "[data-focus-key], [id]") return [copy];
      return [];
    },
  };
  const state = {
    focusKey: "copy-recovery-fixture-model", scrollX: 0, scrollY: 420,
    disclosures: [], controls: [], scrollRegions: [], selectedText: null,
  };

  restoreViewState(root, state);
  assert.equal(document.activeElement, copy);
  assert.equal(window.scrollY, 420);

  flushFrame();
  assert.equal(window.scrollY, 420);
  window.scrollY = 508;
  flushFrame();
  assert.equal(window.scrollY, 420);
});

test("a delayed restoration cannot scroll a replacement route", () => {
  const copy = {
    dataset: { focusKey: "copy-recovery-fixture-model" },
    focus() { document.activeElement = this; },
  };
  const root = {
    firstChild: { id: "workers-view" }, isConnected: true,
    querySelectorAll(selector) {
      if (selector === "[data-focus-key]" || selector === "[data-focus-key], [id]") return [copy];
      return [];
    },
  };
  const state = {
    focusKey: "copy-recovery-fixture-model", scrollX: 0, scrollY: 420,
    disclosures: [], controls: [], scrollRegions: [], selectedText: null,
  };

  location.hash = "#/workers";
  restoreViewState(root, state);
  assert.equal(window.scrollY, 420);

  root.firstChild = { id: "tasks-view" };
  location.hash = "#/tasks";
  window.scrollY = 73;
  flushFrame();
  flushFrame();
  assert.equal(window.scrollY, 73);
});
