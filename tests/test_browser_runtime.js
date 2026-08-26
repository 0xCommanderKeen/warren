"use strict";

const assert = require("node:assert/strict");
const { STALE_MS, DROP_MS, ago, validateEvent } = require("../viewer/projection.js");
const { createBrowserRuntime } = require("../viewer/browser-runtime.js");
const nursery = require("../viewer/nursery.js");

const BASE = Date.parse("2026-08-25T10:00:00.000Z");
const BOOT_ID = "0123456789abcdef0123456789abcdef";
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
const deliveryStatuses = [];
const fleetViews = [];
const resident = name => ({
  file: `${name}.resident.json`, valid: true, manifest_version: 1, home: 0,
  match: { agent_id: "agent-1" }, meta: { name, char: "Monk", accent: "#fff" },
});
let currentResident = resident("Old Name");
let residentsResponse = "ok";

const runtime = createBrowserRuntime({
  now: () => now,
  EventSource: FakeEventSource,
  fetch: async url => {
    if (url.startsWith("/events")) {
      fetchEvents += 1;
      return {
        ok: true,
        headers: { get: name => name === "X-Burrow-Cursor" ? `v1:${BOOT_ID}:1:2:3:100` : null },
        text: async () => fetchEvents === 1 ? event + "\n" : "",
      };
    }
    if (url === "/villagers") return { ok: true, json: async () => [currentResident] };
    if (url === "/residents") {
      if (residentsResponse === "error") return { ok: false, status: 503 };
      if (residentsResponse === "malformed") return { ok: true, json: async () => ({ residents: null }) };
      return { ok: true, json: async () => ({ residents: [currentResident], diagnostics: [] }) };
    }
    if (url === "/transport/status") return {
      ok: true, json: async () => ({
        ingest: { duplicates: 2, dedupe_window: 4096 },
        notifications: { queued: 3, queue_capacity: 64, dropped: 1 },
      }),
    };
    throw new Error(`unexpected fetch ${url}`);
  },
  setTimeout: fn => { runtime.retry = fn; return 1; },
  clearTimeout() {},
  onProjection: view => projections.push(view),
  onFleet: view => fleetViews.push(view),
  onTransport: state => transports.push(state),
  onTransportStatus: status => deliveryStatuses.push(status),
  warn() {},
});

(async () => {
  assert.equal(transports.at(-1), "disconnected");
  const startup = runtime.poll();
  assert.equal(transports.at(-1), "recovering",
    "startup remains non-observable until its first event projection succeeds");
  await startup;
  assert.equal(runtime.snapshot().villagers[0].state, "working");
  assert.equal(transports.at(-1), "polling");

  runtime.connectStream();
  const stream = FakeEventSource.instances[0];
  stream.onopen();
  assert.equal(transports.at(-1), "recovering",
    "HTTP open cannot expose queued SSE backlog as live evidence");
  stream.onmessage({ lastEventId:`v1:${BOOT_ID}:1:2:3:150`, data: JSON.stringify({
    v:0, ts:new Date(BASE + 1).toISOString(), source:"steward", agent_id:"agent-1",
    project:"burrow", type:"routine_started",
    payload:{routine:"summary",run_id:"queued",trigger:"manual"},
  }) });
  assert.deepEqual(fleetViews.at(-1).routineBatch, [],
    "pre-ready backlog is not published before the readiness boundary");
  stream.listeners.ready({lastEventId:`v1:${BOOT_ID}:1:2:3:150`,
    data:JSON.stringify({cursor:`v1:${BOOT_ID}:1:2:3:150`})});
  assert.equal(transports.at(-1), "live");
  assert.equal(fleetViews.findLast(view => view.routineBatch.length)
    .routineBatch[0].payload.run_id, "queued",
  "normal resume evidence is published once after ready validation");
  assert.equal(fleetViews.findLast(view => view.eventEvidence.length)
    .eventEvidence[0].payload.run_id, "queued",
  "all validated pre-ready events cross the same exact cursor boundary for nursery correlation");
  assert.equal(runtime.snapshot().fleetState.routineRecent.filter(entry =>
    entry.event.payload.run_id === "queued").length, 1,
  "republishing acknowledgement evidence does not fold it into the ledger twice");
  assert.deepEqual({state:runtime.snapshot().villagers[0].state,
    lastLine:runtime.snapshot().villagers[0].lastLine},
  {state:"working",lastLine:"woke for summary"},
  "validated staged routine evidence refreshes the villager after readiness");

  stream.onmessage({ data: JSON.stringify({
    v:0, ts:new Date(BASE + 1).toISOString(), source:"steward", agent_id:"agent-1",
    project:"burrow", type:"routine_started", payload:{routine:"summary",trigger:"manual"},
  }) });
  assert.equal(runtime.snapshot().fleetState.routineMalformed, 1,
    "malformed routine evidence survives the production parse/runtime seam");
  assert.equal(runtime.snapshot().villagers[0].lastTs, BASE + 1,
    "a rejected routine does not refresh the last valid activity");
  stream.onmessage({ data: JSON.stringify({
    v:0, ts:new Date(BASE + 2).toISOString(), source:"steward", agent_id:"agent-1",
    project:"burrow", type:"routine_finished",
    payload:{routine:"summary",run_id:"run-1",outcome:"ok",artifacts:[],duration_s:2},
  }) });
  assert.equal(runtime.snapshot().villagers[0].state, "working",
    "an unmatched close for another run cannot rewrite the current routine");
  assert.equal(runtime.snapshot().villagers[0].lastTs, BASE + 1);
  assert.equal(runtime.snapshot().villagers[0].lastLine, "woke for summary");
  stream.onmessage({ lastEventId:`v1:${BOOT_ID}:1:2:3:200`, data: JSON.stringify({
    v:0, ts:new Date(BASE + 3).toISOString(), source:"steward", agent_id:"agent-1",
    project:"burrow", type:"routine_started",
    payload:{routine:"summary",run_id:"run-2",trigger:"manual"},
  }) });
  assert.equal(fleetViews.at(-1).cursor, `v1:${BOOT_ID}:1:2:3:200`);
  assert.equal(fleetViews.at(-1).routineBatch[0].payload.run_id, "run-2",
    "acknowledgements receive only the newly ingested cursor batch");
  assert.equal(runtime.snapshot().villagers[0].state, "working");
  assert.equal(runtime.snapshot().villagers[0].lastLine, "woke for summary");

  // Production SSE publishes one message/fold at a time. Saturate renderable
  // keys, then prove a terminal survives its own publication without stealing
  // a key and is promoted atomically when the exact start arrives later.
  for (let i = 0; i < 200; i++) {
    const common = {v:0,source:"steward",agent_id:`saturated-${i}`,
      project:"burrow",payload:{routine:`routine-${i}`,run_id:`run-${i}`}};
    stream.onmessage({data:JSON.stringify({...common,
      ts:new Date(BASE + 10 + i * 2).toISOString(),type:"routine_started",
      payload:{...common.payload,trigger:"schedule"}})});
    stream.onmessage({data:JSON.stringify({...common,
      ts:new Date(BASE + 11 + i * 2).toISOString(),type:"routine_finished",
      payload:{...common.payload,outcome:"ok",artifacts:[],duration_s:1}})});
  }
  const stagedClose = {v:0,source:"steward",agent_id:"late-agent",project:"burrow",
    ts:new Date(BASE + 10_001).toISOString(),type:"routine_finished",
    payload:{routine:"late-routine",run_id:"late-run",outcome:"ok",artifacts:[],duration_s:1}};
  stream.onmessage({data:JSON.stringify(stagedClose)});
  assert.equal(runtime.snapshot().fleetState.routineKeys.length, 200);
  assert.equal(runtime.snapshot().fleetState.routineOrphans.some(entry =>
    entry.event.payload.run_id === "late-run"), true,
  "separate SSE terminal publication remains in bounded non-renderable staging");
  assert.equal(runtime.snapshot().fleetState.routineRecent.some(entry =>
    entry.event.payload.run_id === "late-run"), false);
  stream.onmessage({data:JSON.stringify({...stagedClose,
    ts:new Date(BASE + 10_000).toISOString(),type:"routine_started",
    payload:{routine:"late-routine",run_id:"late-run",trigger:"schedule"}})});
  assert.deepEqual(runtime.snapshot().fleetState.routineRecent.filter(entry =>
    entry.event.payload.run_id === "late-run").map(entry => entry.event.type).sort(),
  ["routine_finished", "routine_started"]);
  assert.equal(runtime.snapshot().fleetState.routineOrphans.some(entry =>
    entry.event.payload.run_id === "late-run"), false);
  assert.equal(runtime.snapshot().fleetState.routineMalformed, 1,
    "valid orphan staging and promotion do not add malformed diagnostics");

  now = BASE + STALE_MS + 4;
  runtime.tick();
  assert.deepEqual(fleetViews.at(-1).routineBatch, [],
    "clock publications cannot replay retained routine evidence as a fresh acknowledgement");
  assert.deepEqual(fleetViews.at(-1).eventEvidence, [],
    "clock publications cannot replay retained ordinary events as a fresh resident wake-up");
  assert.equal(runtime.snapshot().villagers[0].state, "stale",
    "a silent healthy stream must not freeze working state");
  assert.equal(transports.at(-1), "live", "stale activity does not make transport unhealthy");
  assert.equal(ago(runtime.snapshot().villagers[0].lastTs, projections.at(-1).now), "30m ago");

  currentResident = resident("New Name");
  await runtime.refreshResidents();
  assert.equal(runtime.snapshot().villagers[0].name, "New Name",
    "resident data refreshes while the event stream is silent");
  residentsResponse = "error";
  await runtime.refreshResidents();
  assert.equal(runtime.snapshot().residentReport.available, false,
    "a non-OK refresh truthfully marks a previously loaded directory unavailable");
  assert.equal(runtime.snapshot().residentReport.residents[0].meta.name, "New Name",
    "cached declarations remain deliberate but are no longer claimed available");
  residentsResponse = "malformed";
  await runtime.refreshResidents();
  assert.equal(runtime.snapshot().residentReport.available, false,
    "a malformed successful response cannot make cached declarations appear available");
  await runtime.refreshTransportStatus();
  assert.equal(deliveryStatuses.at(-1).notifications.dropped, 1);
  assert.equal(runtime.snapshot().transportStatus.notifications.queued, 3,
    "server delivery pressure is consumable through the live runtime seam");

  now = BASE + DROP_MS + 10_002;
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
  assert.deepEqual(offlineStates, ["disconnected", "recovering", "disconnected"]);

  const invalidStates = [];
  let invalidFetches = 0;
  const invalid = createBrowserRuntime({
    now: () => now, EventSource: FakeEventSource,
    setTimeout() { return 1; }, clearTimeout() {},
    fetch: async url => {
      if (url === "/villagers") return { ok: false };
      invalidFetches += 1;
      return { ok: true, headers: { get: name =>
        name === "X-Burrow-Cursor" ? `v1:${BOOT_ID}:1:2:3:300` : null },
      text: async () => "" };
    },
    onTransport: state => invalidStates.push(state),
  });
  await invalid.poll();
  invalid.connectStream();
  const invalidStream = FakeEventSource.instances.at(-1);
  invalidStream.onopen();
  await invalidStream.listeners.ready({lastEventId:`v1:${BOOT_ID}:1:2:3:301`,
    data:JSON.stringify({cursor:`v1:${BOOT_ID}:1:2:3:999`})});
  assert.equal(invalidStream.closed, true);
  assert.equal(invalidStates.includes("live"), false,
    "a mismatched readiness marker never establishes an observable boundary");
  assert.equal(invalidFetches, 2,
    "invalid readiness falls back to a grouped polling baseline");

  const oldBoundary=`v1:${BOOT_ID}:1:2:3:5`;
  const rotatedBoot="fedcba9876543210fedcba9876543210";
  const resetTracker=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
  const nurseryDraft={name:"Rotation Keeper",char:"Monk",accent:"#4f7ea6",role:"keeper",
    mission:"Keep rotation truth.",duties:"Observe",rules:"Never replay",
    escalation:"Ask",skills:"research",runner:"codex"};
  const pending=resetTracker.begin(oldBoundary,nurseryDraft).item;
  resetTracker.accepted(pending.key,{request_id:"rotation-request",changed:true,
    declaration_written:true,register_ok:true,register_problems:[]});
  const wake=JSON.stringify({v:0,ts:new Date(BASE+20_000).toISOString(),source:"codex",
    agent_id:"codex:rotation-keeper",project:"rotation",type:"task_started",
    payload:{prompt:"Wake after rotation"}});
  let rotationRead=0;
  const rotated=createBrowserRuntime({now:()=>now,EventSource:null,setTimeout(){return 1;},clearTimeout(){},
    fetch:async url=>{
      if(url==="/villagers")return {ok:false};
      if(!url.startsWith("/events"))throw new Error(`unexpected rotation fetch ${url}`);
      rotationRead+=1; const reset=rotationRead===1;
      return {ok:true,headers:{get:name=>name==="X-Burrow-Cursor"?
        `v1:${rotatedBoot}:4:5:6:${reset?50:51}`:name==="X-Burrow-Reset"&&reset?"1":null},
      text:async()=>wake+"\n"};
    },
    onFleet:view=>resetTracker.observe({events:view.eventEvidence,cursor:view.cursor,reset:view.reset},
      validateEvent),
  });
  await rotated.poll();
  assert.equal(pending.state,"pending",
    "grouped rotation replay is excluded while its validated ending cursor rebases pending nursery truth");
  assert.equal(pending.boundary.namespace,`v1:${rotatedBoot}:4:5:6`);
  await rotated.poll();
  assert.equal(pending.state,"alive",
    "the same exact event is accepted only when a later incremental publication crosses the reset boundary");

  console.log("browser clock and transport share the production runtime seam");
})().catch(error => { console.error(error); process.exitCode = 1; });
