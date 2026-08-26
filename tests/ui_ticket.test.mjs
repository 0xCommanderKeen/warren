import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

class FakeNode {
  constructor(className = "") {
    this.className = className;
    this.dataset = {};
    this.isConnected = true;
    this.listeners = {};
    this.textContent = "";
    this.children = [];
  }

  addEventListener(type, listener) { this.listeners[type] = listener; }
  append(...children) { this.children.push(...children); }
  remove() { this.isConnected = false; }
}

function descendants(node) {
  return [node, ...node.children.filter((child) => child instanceof FakeNode).flatMap(descendants)];
}

function loadTicket() {
  const source = readFileSync(new URL("../ui/app.js", import.meta.url), "utf8");
  const start = source.indexOf('const ledger = document.getElementById("ledger");');
  const end = source.indexOf("/** Confirm a run-now", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);

  const timers = new Map();
  let nextTimer = 1;
  let renders = 0;
  const ledger = new FakeNode("ledger");
  const context = {
    document: { getElementById: () => ledger },
    el(tag, attrs, ...children) {
      const node = new FakeNode(attrs?.class || "");
      for (const [type, listener] of Object.entries(attrs?.on || {})) {
        node.addEventListener(type, listener);
      }
      node.append(...children.filter((child) => child !== null));
      return node;
    },
    setTimeout(callback) {
      const id = nextTimer++;
      timers.set(id, callback);
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    AbortController,
    parseHash: () => ({ view: "residents" }),
    render: () => { renders += 1; },
    schedulerBlame: () => "scheduler detail",
    schedulerLiveness: async () => ({}),
    REPROMPT: Symbol("reprompt"),
  };
  const names = Object.keys(context);
  const values = Object.values(context);
  const ticket = Function(...names, `${source.slice(start, end)}\nreturn ticket;`)(...values);
  const runNextTimer = async () => {
    const entry = timers.entries().next().value;
    assert.ok(entry, "expected a pending poll timer");
    timers.delete(entry[0]);
    await entry[1]();
  };
  return { ticket, timers, runNextTimer, renders: () => renders };
}

function dismiss(node) {
  const button = descendants(node).find((child) => child.className === "dismiss");
  assert.ok(button);
  button.listeners.click();
}

test("dismissal before the first poll cancels its timer", async () => {
  const harness = loadTicket();
  let confirms = 0;
  const node = harness.ticket({
    what: "run",
    confirm: async () => { confirms += 1; return null; },
  });

  dismiss(node);

  assert.equal(harness.timers.size, 0);
  assert.equal(confirms, 0);
  assert.equal(harness.renders(), 0);
});

test("dismissal aborts a confirmation already in flight", async () => {
  const harness = loadTicket();
  let seenSignal;
  const node = harness.ticket({
    what: "run",
    confirm: (signal) => new Promise((resolve, reject) => {
      seenSignal = signal;
      signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }),
  });

  const polling = harness.runNextTimer();
  await Promise.resolve();
  dismiss(node);
  await polling;

  assert.equal(seenSignal.aborted, true);
  assert.equal(harness.timers.size, 0);
  assert.equal(harness.renders(), 0);
});

test("a late result cannot mutate or render after dismissal", async () => {
  const harness = loadTicket();
  let resolveConfirm;
  const node = harness.ticket({
    what: "run",
    confirm: () => new Promise((resolve) => { resolveConfirm = resolve; }),
  });
  const state = descendants(node).find((child) => child.className === "state");
  const reason = descendants(node).find((child) => child.className === "why");

  const polling = harness.runNextTimer();
  await Promise.resolve();
  dismiss(node);
  resolveConfirm({ state: "confirmed", why: "late verdict" });
  await polling;

  assert.equal(state.textContent, "accepted");
  assert.equal(reason.textContent, "accepted, not yet confirmed.");
  assert.equal(harness.timers.size, 0);
  assert.equal(harness.renders(), 0);
});
