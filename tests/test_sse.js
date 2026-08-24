"use strict";

const assert = require("node:assert/strict");
const { createBrowserRuntime } = require("../viewer/browser-runtime.js");

const streams = [];
class FakeEventSource {
  constructor(url) { this.url = url; this.listeners = {}; streams.push(this); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  close() { this.closed = true; }
}

let eventRequests = 0;
let retry;
let finishCatchup;
const response = cursor => ({
  ok: true,
  headers: { get: name => name === "X-Burrow-Cursor" ? cursor : null },
  text: async () => "",
});
const event = (agent, type = "tool_called") => JSON.stringify({
  v: 0, ts: "2026-08-25T10:00:00.000Z", source: "test", agent_id: agent,
  project: "burrow", cwd: "/project", type, payload: { tool: "Read" },
});
const runtime = createBrowserRuntime({
  now: () => Date.now(), EventSource: FakeEventSource,
  setTimeout(fn) { retry = fn; return 1; }, clearTimeout() {},
  fetch(url) {
    if (url === "/villagers") return Promise.resolve({ ok: false });
    eventRequests += 1;
    if (eventRequests === 2)
      return new Promise(resolve => { finishCatchup = () => resolve(response("1:2:60")); });
    return Promise.resolve(response("1:2:10"));
  },
});

(async () => {
  await runtime.poll();
  runtime.connectStream();
  assert.equal(streams[0].url, "/events/stream?since=1%3A2%3A10");

  runtime.poll();
  streams[0].onerror();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(streams[0].closed, true);
  assert.equal(eventRequests, 2, "polling resumes when SSE fails");
  assert.equal(retry, undefined, "reconnect waits for the in-flight poll");

  finishCatchup();
  await new Promise(resolve => setImmediate(resolve));

  retry();
  assert.equal(streams[1].url, "/events/stream?since=1%3A2%3A60");
  streams[1].listeners.reset();
  assert.equal(runtime.snapshot().cursor, 0);
  assert.equal(runtime.snapshot().villagers.length, 0);

  let clock = 0;
  let timerId = 0;
  const timers = new Map();
  const advance = milliseconds => {
    clock += milliseconds;
    for (const [id, timer] of [...timers]) {
      if (timer.at > clock) continue;
      timers.delete(id);
      timer.fn();
    }
  };
  let requests = 0;
  let finishLatePoll;
  const raceStreams = [];
  class RaceEventSource extends FakeEventSource {
    constructor(url) { super(url); raceStreams.push(this); }
  }
  const eventResponse = (cursor, body = "", reset = false) => ({
    ok: true,
    headers: { get: name => name === "X-Burrow-Cursor" ? cursor :
      name === "X-Burrow-Reset" && reset ? "1" : null },
    text: async () => body,
  });
  const raceRuntime = createBrowserRuntime({
    now: () => Date.parse("2026-08-25T10:01:00.000Z"),
    EventSource: RaceEventSource,
    setTimeout(fn, delay) {
      const id = ++timerId;
      timers.set(id, { at: clock + delay, fn });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    fetch(url) {
      if (url === "/villagers") return Promise.resolve({ ok: false });
      requests += 1;
      if (requests === 1) return Promise.resolve(eventResponse("10", event("old", "idle") + "\n"));
      if (requests === 2) return Promise.resolve(eventResponse("20"));
      return new Promise(resolve => {
        finishLatePoll = () => resolve(eventResponse("20", event("old", "idle") + "\n", true));
      });
    },
  });

  await raceRuntime.poll();
  raceRuntime.connectStream();
  raceStreams[0].onerror();
  await new Promise(resolve => setImmediate(resolve));

  const latePoll = raceRuntime.poll();
  advance(2000);
  assert.equal(raceStreams.length, 1,
    "the reconnect timer must not open SSE over a fallback poll");

  finishLatePoll();
  await latePoll;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(raceStreams[1].url, "/events/stream?since=20");

  raceStreams[1].onmessage({ lastEventId: "30", data: event("new") });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(raceRuntime.snapshot().cursor, "30",
    "a completed fallback poll cannot regress a newer stream cursor to 20");
  assert.deepEqual(raceRuntime.snapshot().villagers.map(v => v.id).sort(),
    ["new", "old"], "a late reset cannot erase newer stream state");
  console.log("SSE resumes by cursor and falls back to polling");
})().catch(error => { console.error(error); process.exitCode = 1; });
