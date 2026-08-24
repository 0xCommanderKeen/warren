"use strict";

const assert = require("node:assert/strict");
const { STALE_MS, DROP_MS, ago } = require("../viewer/projection.js");
const { createBrowserRuntime } = require("../viewer/browser-runtime.js");

const BASE = Date.parse("2026-08-25T10:00:00.000Z");
const event = JSON.stringify({
  v: 0, ts: new Date(BASE).toISOString(), source: "test", agent_id: "agent-1",
  project: "burrow", cwd: "/private/project", type: "tool_called",
  payload: { tool: "Read" },
});

class FakeEventSource {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.listeners = {};
    FakeEventSource.instances.push(this);
  }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  close() { this.closed = true; }
}

let now = BASE;
let fetchEvents = 0;
const projections = [];
const transports = [];
const resident = name => ({
  file: `${name}.resident.json`, valid: true, manifest_version: 1, home: 0,
  match: { agent_id: "agent-1" }, meta: { name, char: "Monk", accent: "#fff" },
});
let currentResident = resident("Old Name");

const runtime = createBrowserRuntime({
  now: () => now,
  EventSource: FakeEventSource,
  fetch: async url => {
    if (url.startsWith("/events")) {
      fetchEvents += 1;
      return {
        ok: true,
        headers: { get: name => name === "X-Burrow-Cursor" ? "cursor-1" : null },
        text: async () => fetchEvents === 1 ? event + "\n" : "",
      };
    }
    if (url === "/villagers") return { ok: true, json: async () => [currentResident] };
    if (url === "/residents") {
      return { ok: true, json: async () => ({ residents: [currentResident], diagnostics: [] }) };
    }
    throw new Error(`unexpected fetch ${url}`);
  },
  setTimeout: fn => { runtime.retry = fn; return 1; },
  clearTimeout() {},
  onProjection: view => projections.push(view),
  onTransport: state => transports.push(state),
  warn() {},
});

(async () => {
  assert.equal(transports.at(-1), "disconnected");
  await runtime.poll();
  assert.equal(runtime.snapshot().villagers[0].state, "working");
  assert.equal(transports.at(-1), "polling");

  runtime.connectStream();
  const stream = FakeEventSource.instances[0];
  stream.onopen();
  assert.equal(transports.at(-1), "live");

  now = BASE + STALE_MS + 1;
  runtime.tick();
  assert.equal(runtime.snapshot().villagers[0].state, "stale",
    "a silent healthy stream must not freeze working state");
  assert.equal(transports.at(-1), "live", "stale activity does not make transport unhealthy");
  assert.equal(ago(runtime.snapshot().villagers[0].lastTs, projections.at(-1).now), "30m ago");

  currentResident = resident("New Name");
  await runtime.refreshResidents();
  assert.equal(runtime.snapshot().villagers[0].name, "New Name",
    "resident data refreshes while the event stream is silent");

  now = BASE + DROP_MS + 1;
  runtime.tick();
  assert.equal(runtime.snapshot().villagers.length, 0,
    "the same page drops a villager once the event expires");

  stream.onerror();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stream.closed, true);
  assert.ok(transports.includes("reconnecting"));
  assert.equal(transports.at(-1), "reconnecting");

  const offlineStates = [];
  const offline = createBrowserRuntime({
    now: () => now, EventSource: null, setTimeout() {}, clearTimeout() {},
    fetch: async () => { throw new Error("offline"); },
    onTransport: state => offlineStates.push(state),
  });
  await offline.poll();
  assert.deepEqual(offlineStates, ["disconnected", "polling", "disconnected"]);

  console.log("browser clock and transport share the production runtime seam");
})().catch(error => { console.error(error); process.exitCode = 1; });
