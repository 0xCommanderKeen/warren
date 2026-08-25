"use strict";

const assert = require("node:assert/strict");
const { createBrowserRuntime } = require("../viewer/browser-runtime.js");
const routines = require("../viewer/routine-ledger.js");
const { validateEvent } = require("../viewer/projection.js");
const BOOT_ID = "0123456789abcdef0123456789abcdef";
const RESET_BOOT_ID = "fedcba9876543210fedcba9876543210";
const cursor = (offset, boot = BOOT_ID) => `v1:${boot}:1:2:3:${offset}`;
const resetCursor = offset => `v1:${RESET_BOOT_ID}:7:8:9:${offset}`;

const streams = [];
class FakeEventSource {
  constructor(url) { this.url = url; this.listeners = {}; streams.push(this); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  close() { this.closed = true; }
}

let eventRequests = 0;
let retry;
let finishCatchup;
const fleetViews = [];
const response = (cursor, body = "", reset = false) => ({
  ok: true,
  headers: { get: name => name === "X-Burrow-Cursor" ? cursor :
    name === "X-Burrow-Reset" && reset ? "1" : null },
  text: async () => body,
});
const event = (agent, type = "tool_called") => JSON.stringify({
  v: 0, ts: "2026-08-25T10:00:00.000Z", source: "test", agent_id: agent,
  project: "burrow", cwd: "/project", type, payload: { tool: "Read" },
});
const routineStart = (runId, ts = "2026-08-25T10:00:01.000Z") => JSON.stringify({
  v:0,ts,source:"steward",agent_id:"baseline",project:"burrow",
  type:"routine_started",payload:{routine:"summary",run_id:runId,trigger:"manual"},
});
const acknowledgements = routines.createAcknowledgements();
const runtime = createBrowserRuntime({
  now: () => Date.now(), EventSource: FakeEventSource,
  setTimeout(fn) { retry = fn; return 1; }, clearTimeout() {},
  onFleet(view) {
    fleetViews.push(view);
    acknowledgements.observe({events:view.routineBatch,cursor:view.cursor,reset:view.reset},
      Date.now(), validateEvent);
  },
  fetch(url) {
    if (url === "/villagers") return Promise.resolve({ ok: false });
    eventRequests += 1;
    if (eventRequests === 2)
      return new Promise(resolve => { finishCatchup = () => resolve(response(cursor(60))); });
    if (eventRequests === 3)
      return Promise.resolve(response(resetCursor(40), routineStart("replayed") + "\n", true));
    return Promise.resolve(response(cursor(10)));
  },
});

(async () => {
  await runtime.poll();
  runtime.connectStream();
  assert.equal(streams[0].url, "/events/stream?since=" + encodeURIComponent(cursor(10)));

  runtime.poll();
  streams[0].onerror();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(streams[0].closed, true);
  assert.equal(eventRequests, 2, "polling resumes when SSE fails");
  assert.equal(retry, undefined, "reconnect waits for the in-flight poll");

  finishCatchup();
  await new Promise(resolve => setImmediate(resolve));

  retry();
  assert.equal(streams[1].url, "/events/stream?since=" + encodeURIComponent(cursor(60)));
  acknowledgements.request("baseline", "summary", Date.now(),
    {cursor:runtime.snapshot().cursor}, validateEvent);
  acknowledgements.accepted("baseline", "summary", "request-1", Date.now());
  streams[1].onmessage({lastEventId:cursor(70), data:routineStart("caught-up")});
  assert.equal(acknowledgements.get("baseline", "summary").state, "pending",
    "pre-ready evidence remains unavailable until the boundary is validated");
  streams[1].listeners.ready({lastEventId:cursor(70),
    data:JSON.stringify({cursor:cursor(70)})});
  assert.equal(acknowledgements.get("baseline", "summary").run_id, "caught-up",
    "a start between fallback polling and ready acknowledges after valid catch-up");

  acknowledgements.request("reset-agent", "summary", Date.now(),
    {cursor:runtime.snapshot().cursor}, validateEvent);
  acknowledgements.accepted("reset-agent", "summary", "request-reset", Date.now());
  streams[1].listeners.reset();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(streams[1].closed, true,
    "an un-ID'd reset closes the ambiguous stream before rebasing");
  assert.equal(runtime.snapshot().cursor, resetCursor(40));
  assert.equal(acknowledgements.get("reset-agent", "summary").state, "pending",
    "the production reset callback cannot acknowledge matching replay");
  const baseline = fleetViews.findLast(view => view.reset === true);
  assert.equal(baseline.cursor, resetCursor(40),
    "the reset publication carries the ending cursor of its grouped baseline");
  assert.equal(baseline.routineBatch[0].payload.run_id, "replayed",
    "the reset publication groups the full replay with its ending boundary");
  retry();
  streams[2].listeners.ready({lastEventId:resetCursor(40),
    data:JSON.stringify({cursor:resetCursor(40)})});
  streams[2].onmessage({lastEventId:resetCursor(50),
    data:JSON.stringify({...JSON.parse(routineStart("later", "2026-08-25T10:00:02.000Z")),
      agent_id:"reset-agent"})});
  assert.equal(acknowledgements.get("reset-agent", "summary").run_id, "later",
    "only later evidence beyond the production reset boundary can acknowledge");

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
      if (requests === 1) return Promise.resolve(eventResponse(cursor(10), event("old", "idle") + "\n"));
      if (requests === 2) return Promise.resolve(eventResponse(cursor(20)));
      return new Promise(resolve => {
        finishLatePoll = () => resolve(eventResponse(cursor(20), event("old", "idle") + "\n", true));
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
  assert.equal(raceStreams[1].url,
    "/events/stream?since=" + encodeURIComponent(cursor(20)));

  const raceReady = cursor(20);
  raceStreams[1].listeners.ready({lastEventId:raceReady,
    data:JSON.stringify({cursor:raceReady})});
  raceStreams[1].onmessage({ lastEventId: cursor(30), data: event("new") });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(raceRuntime.snapshot().cursor, cursor(30),
    "a completed fallback poll cannot regress a newer stream cursor to 20");
  assert.deepEqual(raceRuntime.snapshot().villagers.map(v => v.id).sort(),
    ["new", "old"], "a late reset cannot erase newer stream state");

  const bootstrapViews = [];
  const bootstrapRuntime = createBrowserRuntime({
    now: () => Date.now(), EventSource: FakeEventSource,
    setTimeout() { return 1; }, clearTimeout() {},
    fetch: async () => { throw new Error("bootstrap should not poll"); },
    onFleet: view => bootstrapViews.push(view),
  });
  bootstrapRuntime.connectStream();
  const bootstrapStream = streams.at(-1);
  bootstrapStream.onmessage({lastEventId:cursor(1), data:routineStart("bootstrap")});
  bootstrapStream.listeners.ready({lastEventId:cursor(1),
    data:JSON.stringify({cursor:cursor(1)})});
  assert.equal(bootstrapViews.some(view => view.routineBatch.length), false,
    "a bootstrap snapshot is never published as acknowledgement evidence");
  assert.equal(bootstrapViews.at(-2).reset, true,
    "valid bootstrap ready explicitly rebases at its ending cursor");
  assert.equal(bootstrapRuntime.snapshot().fleetState.routineRecent.filter(entry =>
    entry.event.payload.run_id === "bootstrap").length, 1,
  "bootstrap suppression does not fold the normal routine ledger twice");

  const overflowAcks = routines.createAcknowledgements();
  const overflowRuntime = createBrowserRuntime({
    now: () => Date.now(), EventSource: FakeEventSource,
    setTimeout() { return 1; }, clearTimeout() {},
    fetch: url => Promise.resolve(url === "/villagers" ? {ok:false} : response(cursor(100))),
    onFleet(view) {
      overflowAcks.observe({events:view.routineBatch,cursor:view.cursor,reset:view.reset},
        Date.now(), validateEvent);
    },
  });
  await overflowRuntime.poll();
  overflowAcks.request("target", "summary", Date.now(),
    {cursor:overflowRuntime.snapshot().cursor}, validateEvent);
  overflowAcks.accepted("target", "summary", "overflow-request", Date.now());
  overflowRuntime.connectStream();
  const overflowStream = streams.at(-1);
  overflowStream.onmessage({lastEventId:cursor(101), data:JSON.stringify({
    ...JSON.parse(routineStart("would-be-partial")), agent_id:"target",
  })});
  for (let index = 0; index < 4000; index++) {
    overflowStream.onmessage({lastEventId:cursor(102 + index), data:JSON.stringify({
      ...JSON.parse(routineStart(`overflow-${index}`)), agent_id:`overflow-${index}`,
    })});
  }
  overflowStream.listeners.ready({lastEventId:cursor(4101),
    data:JSON.stringify({cursor:cursor(4101)})});
  assert.equal(overflowAcks.get("target", "summary").state, "pending",
    "staging overflow suppresses the entire partial batch and rebases conservatively");
  overflowStream.onmessage({lastEventId:cursor(4102), data:JSON.stringify({
    ...JSON.parse(routineStart("after-overflow")), agent_id:"target",
  })});
  assert.equal(overflowAcks.get("target", "summary").run_id, "after-overflow",
    "evidence after the overflow rebase remains eligible");
  console.log("SSE resumes by cursor and falls back to polling");
})().catch(error => { console.error(error); process.exitCode = 1; });
