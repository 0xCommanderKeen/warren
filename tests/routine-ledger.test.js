"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const routines = require("../viewer/routine-ledger.js");
const { validateEvent } = require("../viewer/projection.js");
const lines = fs.readFileSync(path.join(__dirname, "fixtures/routines.jsonl"), "utf8").trim().split("\n").map(JSON.parse);
const NOW = Date.parse("2026-08-25T12:00:00Z");
const BOOT_ID = "0123456789abcdef0123456789abcdef";
const eventCursor = (offset, generation = 3) =>
  `v1:${BOOT_ID}:1:2:${generation}:${offset}`;
const key = (...fields) => JSON.stringify(fields);

test("fixture projects finished, failed, stale and malformed runs truthfully", () => {
  const result = routines.project(lines, NOW, { staleMs: 60_000, validateEvent });
  assert.equal(result.byRoutine.get(key("steward:life-agent", "summary"))[0].state, "finished");
  assert.equal(result.byRoutine.get(key("steward:life-agent", "summary"))[0].outcome, "ok");
  assert.deepEqual(result.byRoutine.get(key("steward:life-agent", "summary"))[0].artifacts, ["summary.md"]);
  assert.equal(result.byRoutine.get(key("steward:life-agent", "inbox"))[0].state, "failed");
  assert.equal(result.byRoutine.get(key("steward:life-agent", "inbox"))[0].error, "mail service unavailable");
  assert.equal(result.byRoutine.get(key("steward:life-agent", "stuck"))[0].state, "stale");
  assert.ok(result.diagnostics.some(x => x.reason.includes("malformed")));
});

test("fixture routine events pass the shared v0 validator before ledger projection", () => {
  assert.equal(lines.filter(event => validateEvent(event) === null).length, lines.length - 1);
  assert.equal(validateEvent(lines.find(event => event.payload.routine === "broken")), "invalid payload.run_id");
});

test("watchdog failure without unknowable duration is a valid closed run", () => {
  const events = [
    {v:0,ts:"2026-08-25T11:00:00.000Z",source:"steward",agent_id:"a",project:"life",type:"routine_started",payload:{routine:"r",run_id:"lost",trigger:"schedule"}},
    {v:0,ts:"2026-08-25T11:40:00.000Z",source:"steward",agent_id:"a",project:"life",type:"routine_failed",payload:{routine:"r",run_id:"lost",error:"run never reported back"}},
  ];
  assert.equal(events.map(validateEvent).filter(Boolean).length, 0);
  const [run] = routines.project(events, NOW, { validateEvent }).byRoutine.get(key("a", "r"));
  assert.equal(run.state, "failed"); assert.equal(run.duration_s, null);
});

test("a finish without an explicit artifacts array is skipped, never synthesized", () => {
  const start = {v:0,ts:"2026-08-25T11:00:00.000Z",source:"steward",agent_id:"a",
    project:"life",type:"routine_started",payload:{routine:"r",run_id:"missing",trigger:"schedule"}};
  const finish = {...start,ts:"2026-08-25T11:01:00.000Z",type:"routine_finished",
    payload:{routine:"r",run_id:"missing",outcome:"ok",duration_s:60}};
  assert.equal(validateEvent(finish), "invalid payload.artifacts");
  const result = routines.project([start, finish], NOW, {validateEvent, staleMs:1});
  assert.equal(result.byRoutine.get(key("a", "r"))[0].state, "stale");
  assert.equal(result.diagnostics.length, 1);
});

test("out-of-order close is paired by run id, while a close before start cannot manufacture completion", () => {
  const result = routines.project(lines, NOW, { staleMs: 1 });
  const history = result.byRoutine.get(key("steward:life-agent", "out-of-order"));
  assert.equal(history[0].state, "stale");
  assert.ok(result.diagnostics.some(x => x.reason.includes("predates")));
});

test("histories are newest first and bounded", () => {
  const events = [];
  for (let i = 0; i < 30; i++) events.push({ v:0, source:"steward", agent_id:"a", project:"p",
    ts:new Date(NOW+i).toISOString(), type:"routine_started", payload:{routine:"r",run_id:String(i),trigger:"schedule"} });
  const history = routines.project(events, NOW + 100).byRoutine.get(key("a", "r"));
  assert.equal(history.length, routines.MAX_RUNS); assert.equal(history[0].run_id, "29");
});

test("declared routines include never observed while visitors have no declaration", () => {
  const projection = routines.project([], NOW);
  const resident = { valid:true, match:{agent_id:"a"}, routines:[{id:"summary",next_fire:"tomorrow"}] };
  assert.equal(routines.declared([resident], projection)[0].state, "never-observed");
  assert.deepEqual(routines.declared([{ valid:false, routines:[{id:"x"}] }], projection), []);
});

test("project-matched residents correlate routine history without an agent-id match", () => {
  const projection = routines.project(lines, NOW, { validateEvent });
  const resident = { valid:true, match:{project:"life"}, routines:[{id:"summary"}] };
  const [row] = routines.declared([resident], projection);
  assert.equal(row.latest.run_id, "ok-1"); assert.equal(row.agent_id, "steward:life-agent");
});

test("acknowledgement uses pre-POST identities, not timestamps, and never starts optimistically", () => {
  const ack = routines.createAcknowledgements(1000);
  const start = (agent, routine, trigger, ts, project="life") => ({v:0,source:"steward",project,agent_id:agent,ts:new Date(ts).toISOString(),type:"routine_started",payload:{routine,run_id:"run-1",trigger}});
  const retainedFuture = start("a", "summary", "manual", NOW + 60_000);
  retainedFuture.payload.run_id = "future-before-request";
  ack.requested("a", "summary", NOW, [retainedFuture], validateEvent);
  ack.accepted("a", "summary", "request-1", NOW + 10);
  assert.equal(ack.get("a", "summary").state, "pending");
  ack.observe([retainedFuture, start("a","other","manual",NOW+1),
    start("a","summary","schedule",NOW+2)], NOW+500, validateEvent);
  assert.equal(ack.get("a", "summary").state, "pending");
  const newlyIngested = start("a", "summary", "manual", NOW - 60_000);
  newlyIngested.payload.run_id = "new-despite-old-clock";
  ack.observe([retainedFuture, newlyIngested], NOW+500, validateEvent);
  assert.equal(ack.get("a", "summary").state, "running");
  assert.equal(ack.get("a", "summary").run_id, "new-despite-old-clock");
});

test("logical start identity survives display eviction and rejects timestamp-mutated replays", () => {
  const ack = routines.createAcknowledgements();
  const started = (run_id, ts, source="steward") => ({v:0,source,project:"life",agent_id:"a",
    ts:new Date(ts).toISOString(),type:"routine_started",
    payload:{routine:"summary",run_id,trigger:"manual"}});
  const original = started("old-run", NOW);
  ack.requested("a", "summary", NOW + 1, [original], validateEvent);
  ack.accepted("a", "summary", "request", NOW + 2);
  ack.observe([started("old-run", NOW + 10_000)], NOW + 3, validateEvent);
  assert.equal(ack.get("a", "summary").state, "pending");
  ack.observe([started("forged", NOW + 20_000, "codex")], NOW + 4, validateEvent);
  assert.equal(ack.get("a", "summary").state, "pending");
  ack.observe([started("new-run", NOW - 10_000)], NOW + 5, validateEvent);
  assert.equal(ack.get("a", "summary").state, "running");
  assert.equal(ack.get("a", "summary").run_id, "new-run");
});

test("a start ingested while POST is in flight acknowledges after acceptance", () => {
  const ack = routines.createAcknowledgements();
  const start = {v:0,source:"steward",project:"life",agent_id:"a",
    ts:new Date(NOW).toISOString(),type:"routine_started",
    payload:{routine:"summary",run_id:"racing",trigger:"manual"}};
  ack.requested("a", "summary", NOW);
  ack.observe([start], NOW + 1, validateEvent);
  assert.equal(ack.get("a", "summary").state, "requesting");
  ack.accepted("a", "summary", "request", NOW + 2);
  assert.equal(ack.get("a", "summary").state, "running");
});

test("a complete lifecycle observed before POST acceptance is terminal immediately", () => {
  const ack = routines.createAcknowledgements();
  const cursor = eventCursor;
  const start = {v:0,source:"steward",project:"life",agent_id:"a",
    ts:new Date(NOW).toISOString(),type:"routine_started",
    payload:{routine:"summary",run_id:"fast",trigger:"manual"}};
  const finish = {...start,ts:new Date(NOW+1).toISOString(),type:"routine_finished",
    payload:{routine:"summary",run_id:"fast",outcome:"ok",artifacts:[],duration_s:1}};
  ack.requested("a", "summary", NOW, {events:[],cursor:cursor(100)}, validateEvent);
  ack.observe({events:[start],cursor:cursor(200)}, NOW + 1, validateEvent);
  ack.observe({events:[finish],cursor:cursor(300)}, NOW + 2, validateEvent);
  ack.accepted("a", "summary", "request", NOW + 3);
  assert.equal(ack.get("a", "summary").state, "completed");
  assert.equal(ack.get("a", "summary").closedAt, NOW + 1);
});

test("requesting candidate terminal follows shared ordering across publications", () => {
  const ack = routines.createAcknowledgements();
  const base = {v:0,source:"steward",project:"life",agent_id:"a"};
  const start = {...base,ts:new Date(NOW).toISOString(),type:"routine_started",
    payload:{routine:"summary",run_id:"before-accept",trigger:"manual"}};
  const finish = {...base,ts:new Date(NOW+2).toISOString(),type:"routine_finished",
    payload:{routine:"summary",run_id:"before-accept",outcome:"ok",artifacts:[],duration_s:1}};
  const failure = {...base,ts:new Date(NOW+3).toISOString(),type:"routine_failed",
    payload:{routine:"summary",run_id:"before-accept",error:"newer truth"}};
  ack.requested("a", "summary", NOW);
  ack.observe([start, finish], NOW + 1, validateEvent);
  ack.observe([failure], NOW + 2, validateEvent);
  ack.accepted("a", "summary", "request", NOW + 3);
  assert.deepEqual({state:ack.get("a","summary").state,
    closedAt:ack.get("a","summary").closedAt},
  {state:"failed",closedAt:NOW+3});
});

test("requesting candidate reconsiders retained terminal after delayed earlier start", () => {
  const ack = routines.createAcknowledgements();
  const base = {v:0,source:"steward",project:"life",agent_id:"a"};
  const event = (type, ts, payload) => ({...base,ts:new Date(ts).toISOString(),type,
    payload:{routine:"summary",run_id:"request-race",...payload}});
  ack.requested("a", "summary", NOW);
  ack.observe([event("routine_started",NOW+4,{trigger:"manual"})], NOW+1, validateEvent);
  ack.observe([event("routine_finished",NOW+3,
    {outcome:"ok",artifacts:[],duration_s:1})], NOW+2, validateEvent);
  ack.observe([event("routine_started",NOW+2,{trigger:"manual"})], NOW+3, validateEvent);
  ack.accepted("a", "summary", "request", NOW+4);
  assert.deepEqual({state:ack.get("a","summary").state,
    closedAt:ack.get("a","summary").closedAt},
  {state:"completed",closedAt:NOW+3});
});

test("production cursor boundary rejects retained replay and cursor namespace resets", () => {
  const ack = routines.createAcknowledgements();
  const cursor = eventCursor;
  const start = {v:0,source:"steward",project:"life",agent_id:"a",
    ts:new Date(NOW).toISOString(),type:"routine_started",
    payload:{routine:"summary",run_id:"old",trigger:"manual"}};
  ack.requested("a", "summary", NOW, {events:[],cursor:cursor(100)}, validateEvent);
  ack.accepted("a", "summary", "request", NOW + 1);
  ack.observe({events:[start],cursor:cursor(100)}, NOW + 2, validateEvent);
  assert.equal(ack.get("a", "summary").state, "pending");
  ack.observe({events:[{...start,ts:new Date(NOW+1000).toISOString()}],
    cursor:cursor(500, 4), reset:true}, NOW + 3, validateEvent);
  assert.equal(ack.get("a", "summary").state, "pending");
});

test("a reset publication rebases pending correlation without acknowledging its replay", () => {
  const ack = routines.createAcknowledgements();
  const cursor = eventCursor;
  const lifecycle = (type, runId, at, extra) => ({v:0,source:"steward",project:"life",
    agent_id:"a",ts:new Date(at).toISOString(),type,
    payload:{routine:"summary",run_id:runId,...extra}});
  ack.requested("a", "summary", NOW, {events:[],cursor:cursor(100)}, validateEvent);
  ack.accepted("a", "summary", "request", NOW + 1);
  ack.observe({events:[lifecycle("routine_started","replayed",NOW-10,{trigger:"manual"})],
    cursor:cursor(40,4),reset:true}, NOW + 2, validateEvent);
  assert.equal(ack.get("a", "summary").state, "pending");
  ack.observe({events:[lifecycle("routine_started","real",NOW+3,{trigger:"manual"})],
    cursor:cursor(41,4)}, NOW + 3, validateEvent);
  assert.deepEqual({state:ack.get("a","summary").state,run_id:ack.get("a","summary").run_id},
    {state:"running",run_id:"real"});
  ack.observe({events:[lifecycle("routine_finished","real",NOW+4,
    {outcome:"ok",artifacts:[],duration_s:1})],cursor:cursor(42,4)}, NOW + 4, validateEvent);
  assert.equal(ack.get("a", "summary").state, "completed");
});

test("a reset rebases an uncertain request and only later exact evidence resolves it", () => {
  const ack = routines.createAcknowledgements();
  const cursor = eventCursor;
  const lifecycle = (type, runId, at, extra) => ({v:0,source:"steward",project:"life",
    agent_id:"a",ts:new Date(at).toISOString(),type,
    payload:{routine:"summary",run_id:runId,...extra}});
  ack.requested("a", "summary", NOW, {events:[],cursor:cursor("100")}, validateEvent);
  ack.uncertain("a", "summary", "connection lost", NOW + 1);
  ack.observe({events:[lifecycle("routine_started","snapshot",NOW-10,{trigger:"manual"})],
    cursor:cursor("40",4),reset:true}, NOW + 2, validateEvent);
  assert.equal(ack.get("a", "summary").state, "uncertain");
  assert.equal(ack.get("a", "summary").boundary.offset, "40");
  ack.observe({events:[lifecycle("routine_started","real",NOW+3,{trigger:"manual"})],
    cursor:cursor("41",4)}, NOW + 3, validateEvent);
  assert.deepEqual({state:ack.get("a","summary").state,run_id:ack.get("a","summary").run_id},
    {state:"running",run_id:"real"});
  ack.observe({events:[lifecycle("routine_finished","real",NOW+4,
    {outcome:"ok",artifacts:[],duration_s:1})],cursor:cursor("42",4)}, NOW + 4, validateEvent);
  assert.equal(ack.get("a", "summary").state, "completed");
});

test("v1 boundaries preserve exact uint64 ordering above Number precision", () => {
  const start = runId => ({v:0,source:"steward",project:"life",agent_id:"a",
    ts:new Date(NOW).toISOString(),type:"routine_started",
    payload:{routine:"summary",run_id:runId,trigger:"manual"}});
  const cases = [
    ["9007199254740992", "9007199254740993"],
    ["18446744073709551614", "18446744073709551615"],
  ];
  for (const [before, after] of cases) {
    const ack = routines.createAcknowledgements();
    ack.requested("a", "summary", NOW,
      {events:[],cursor:eventCursor(before)}, validateEvent);
    ack.accepted("a", "summary", "request", NOW + 1);
    ack.observe({events:[start("same")],cursor:eventCursor(before)},
      NOW + 2, validateEvent);
    assert.equal(ack.get("a", "summary").state, "pending");
    ack.observe({events:[start("next")],cursor:eventCursor(after)},
      NOW + 3, validateEvent);
    assert.equal(ack.get("a", "summary").run_id, "next");
  }
  assert.deepEqual(routines.boundaryAvailability(eventCursor("18446744073709551616")),
    {ok:false,reason:"boundary",
      message:"Run Now unavailable: exact telemetry cursor is not available; no request was sent"});
});

test("request requires a valid supplied v1 boundary and treats offset zero as exact", () => {
  const ack = routines.createAcknowledgements();
  const unavailable = {ok:false,reason:"boundary",
    message:"Run Now unavailable: exact telemetry cursor is not available; no request was sent"};
  assert.deepEqual(ack.request("a","summary",NOW,{events:[],cursor:null},validateEvent), unavailable);
  assert.deepEqual(ack.request("a","summary",NOW,{events:[],cursor:"10"},validateEvent), unavailable);
  assert.equal(ack.size(), 0);
  assert.equal(ack.request("a","summary",NOW,
    {events:[],cursor:eventCursor("0")},validateEvent).ok, true);
  assert.equal(ack.get("a","summary").boundary.offset, "0");
});

test("boundary accepts only canonical server-issued v1 cursors", () => {
  const unavailable = {ok:false,reason:"boundary",
    message:"Run Now unavailable: exact telemetry cursor is not available; no request was sent"};
  for (const value of [eventCursor("0"),
    `v1:${BOOT_ID}:0:0:0:0`,
    `v1:${BOOT_ID}:18446744073709551615:18446744073709551615:` +
      "18446744073709551615:18446744073709551615"]) {
    assert.deepEqual(routines.boundaryAvailability(value), {ok:true}, value);
  }
  for (const value of [
    `v1:short:1:2:3:4`, `v1:${BOOT_ID.toUpperCase()}:1:2:3:4`,
    `v1:${BOOT_ID}:1:2:3`, `v1:${BOOT_ID}:1:2:3:4:5`,
    `v2:${BOOT_ID}:1:2:3:4`, `v1:${BOOT_ID}:+1:2:3:4`,
    `v1:${BOOT_ID}:-1:2:3:4`, `v1:${BOOT_ID}:01:2:3:4`,
    `v1:${BOOT_ID}:1:02:3:4`, `v1:${BOOT_ID}:1:2:03:4`,
    `v1:${BOOT_ID}:1:2:3:04`, `v1:${BOOT_ID}:1:2:3:18446744073709551616`,
    `v1:${BOOT_ID}:18446744073709551616:2:3:4`,
    `v1:${BOOT_ID}:1:18446744073709551616:3:4`,
    `v1:${BOOT_ID}:1:2:18446744073709551616:4`,
    `v1:${BOOT_ID}:1:2:3:`, `v1:${BOOT_ID}:1:2:3:٤`, "", null,
  ]) assert.deepEqual(routines.boundaryAvailability(value), unavailable, String(value));
});

test("Run Now requires a currently observable transport as well as an exact cursor", () => {
  for (const transport of ["live", "polling"])
    assert.deepEqual(routines.telemetryAvailability(transport, eventCursor(1)), {ok:true});
  for (const transport of ["disconnected", "recovering", "reconnecting", null]) {
    const result = routines.telemetryAvailability(transport, eventCursor(1));
    assert.equal(result.ok, false); assert.equal(result.reason, "transport");
    assert.match(result.message, /no request was sent/);
    assert.equal(routines.runDisabled({enabled:true}, {enabled:true,retired:false}, "loaded",
      null, [], result), true);
  }
});

test("confirmed run closes across a cursor generation reset", () => {
  const ack = routines.createAcknowledgements();
  const cursor = eventCursor;
  const base = {v:0,source:"steward",project:"life",agent_id:"a"};
  const start = {...base,ts:new Date(NOW).toISOString(),type:"routine_started",
    payload:{routine:"summary",run_id:"confirmed",trigger:"manual"}};
  const close = {...base,ts:new Date(NOW+1).toISOString(),type:"routine_finished",
    payload:{routine:"summary",run_id:"confirmed",outcome:"ok",artifacts:[],duration_s:1}};
  ack.requested("a", "summary", NOW, {events:[],cursor:cursor(100)}, validateEvent);
  ack.accepted("a", "summary", "request", NOW + 1);
  ack.observe({events:[start],cursor:cursor(200)}, NOW + 2, validateEvent);
  assert.equal(ack.get("a", "summary").state, "running");
  ack.observe({events:[close],cursor:cursor(10, 4),reset:true}, NOW + 3, validateEvent);
  assert.equal(ack.get("a", "summary").state, "completed");
});

test("a reset cannot select an unrelated start or terminal before identity confirmation", () => {
  const ack = routines.createAcknowledgements();
  const cursor = eventCursor;
  const start = {v:0,source:"steward",project:"life",agent_id:"a",
    ts:new Date(NOW).toISOString(),type:"routine_started",
    payload:{routine:"summary",run_id:"replayed",trigger:"manual"}};
  const close = {...start,ts:new Date(NOW+1).toISOString(),type:"routine_finished",
    payload:{routine:"summary",run_id:"replayed",outcome:"ok",artifacts:[],duration_s:1}};
  ack.requested("a", "summary", NOW, {events:[],cursor:cursor(100)}, validateEvent);
  ack.accepted("a", "summary", "request", NOW + 1);
  ack.observe({events:[start, close],cursor:cursor(10, 4),reset:true}, NOW + 2, validateEvent);
  assert.equal(ack.get("a", "summary").state, "pending");
});

test("terminal acknowledgement history is bounded without evicting active requests", () => {
  const ack = routines.createAcknowledgements(1000, 5);
  for (let i = 0; i < 7; i++) {
    assert.equal(ack.requested(`a-${i}`, "summary", NOW + i), true);
    ack.failed(`a-${i}`, "summary", "no", NOW + i);
  }
  assert.equal(ack.size(), 5);
  for (let i = 0; i < 5; i++) {
    const active = routines.createAcknowledgements(1000, 1);
    assert.equal(active.requested(`active-${i}`, "summary", NOW), true);
    assert.equal(active.requested(`overflow-${i}`, "summary", NOW + 1), false);
    assert.equal(active.size(), 1);
  }
});

test("acknowledgement capacity refusal is explicit and leaves active truth untouched", () => {
  const ack = routines.createAcknowledgements(1000, 2);
  assert.equal(ack.request("a", "r", NOW).ok, true);
  assert.equal(ack.request("b", "r", NOW + 1).ok, true);
  assert.deepEqual(ack.availability("c", "r"), {ok:false,reason:"capacity",
    message:"run-request tracking is full of unresolved requests; no request was sent"});
  assert.deepEqual(ack.request("c", "r", NOW + 2), {ok:false,reason:"capacity",
    message:"run-request tracking is full of unresolved requests; no request was sent"});
  assert.equal(ack.size(), 2);
  assert.equal(ack.get("a", "r").state, "requesting");
});

test("lost terminal candidates make a late start indeterminate until exact evidence returns", () => {
  const ack = routines.createAcknowledgements();
  const lifecycle = (type, runId, at, extra={}) => ({v:0,source:"steward",project:"life",
    agent_id:"a",ts:new Date(at).toISOString(),type,
    payload:{routine:"summary",run_id:runId,...extra}});
  ack.requested("a", "summary", NOW);
  ack.accepted("a", "summary", "request", NOW + 1);
  ack.observe([lifecycle("routine_finished","target",NOW+3,
    {outcome:"ok",artifacts:[],duration_s:1})], NOW + 2, validateEvent);
  for (let i = 0; i <= routines.MAX_RUNS; i++) {
    ack.observe([lifecycle("routine_finished",`other-${i}`,NOW+10+i,
      {outcome:"ok",artifacts:[],duration_s:1})], NOW + 10 + i, validateEvent);
  }
  ack.observe([lifecycle("routine_started","target",NOW+2,{trigger:"manual"})],
    NOW + 40, validateEvent);
  const uncertain = ack.get("a", "summary");
  assert.equal(uncertain.state, "indeterminate");
  assert.ok(uncertain.terminalEvidenceLoss.count >= 1);
  assert.ok(uncertain.terminalEvidenceLoss.through >= NOW + 3);
  assert.equal(routines.runDisabled({enabled:true}, null, "unconfigured", uncertain, []), true);
  ack.observe([lifecycle("routine_finished","target",NOW+3,
    {outcome:"ok",artifacts:[],duration_s:1})], NOW + 41, validateEvent);
  assert.equal(ack.get("a", "summary").state, "completed");
});

test("acknowledgement closes only on the captured run id and exact agent", () => {
  const ack = routines.createAcknowledgements(1000);
  const event = (type, agent, runId, ts, extra = {}) => ({ v:0, source:"steward",
    project:"life", agent_id:agent, ts:new Date(ts).toISOString(), type,
    payload:{ routine:"summary", run_id:runId, ...extra } });
  ack.requested("a", "summary", NOW);
  ack.accepted("a", "summary", "request-1", NOW + 1);
  ack.observe([event("routine_started", "a", "captured", NOW + 2, {trigger:"manual"})],
    NOW + 2, validateEvent);
  assert.equal(ack.get("a", "summary").state, "running");
  ack.observe([
    event("routine_finished", "a", "other", NOW + 3, {outcome:"ok",artifacts:[],duration_s:1}),
    event("routine_failed", "b", "captured", NOW + 4, {error:"wrong agent"}),
  ], NOW + 4, validateEvent);
  assert.equal(ack.get("a", "summary").state, "running");
  ack.observe([event("routine_finished", "a", "captured", NOW + 5,
    {outcome:"ok",artifacts:[],duration_s:3})], NOW + 5, validateEvent);
  assert.equal(ack.get("a", "summary").state, "completed");
  assert.equal(ack.get("a", "summary").closedAt, NOW + 5);
});

test("acknowledged run exposes an explicit failed terminal state", () => {
  const ack = routines.createAcknowledgements();
  const base = {v:0,source:"steward",project:"life",agent_id:"a"};
  ack.requested("a", "summary", NOW); ack.accepted("a", "summary", "request", NOW);
  ack.observe([{...base,ts:new Date(NOW+1).toISOString(),type:"routine_started",
    payload:{routine:"summary",run_id:"captured",trigger:"manual"}}], NOW+1, validateEvent);
  ack.observe([{...base,ts:new Date(NOW+2).toISOString(),type:"routine_failed",
    payload:{routine:"summary",run_id:"captured",error:"boom"}}], NOW+2, validateEvent);
  assert.equal(ack.get("a", "summary").state, "failed");
});

test("project acknowledgement still requires exact project, routine, and manual trigger", () => {
  const ack = routines.createAcknowledgements();
  ack.requested({project:"life"}, "summary", NOW); ack.accepted({project:"life"}, "summary", "q", NOW+10);
  const start = (project, run_id) => ({v:0,source:"steward",project,agent_id:"dynamic-agent",ts:new Date(NOW+1).toISOString(),type:"routine_started",payload:{routine:"summary",run_id,trigger:"manual"}});
  ack.observe([start("other", "other-run")], NOW+20, validateEvent); assert.equal(ack.get({project:"life"}, "summary").state, "pending");
  ack.observe([start("life", "life-run")], NOW+20, validateEvent); assert.equal(ack.get({project:"life"}, "summary").state, "running");
});

test("project acknowledgement follows a mixed runner lifecycle, not the active project agent", () => {
  const ack = routines.createAcknowledgements();
  const correlation = {project:"life"};
  const base = {v:0,source:"steward",project:"life",agent_id:"steward:dynamic-runner"};
  ack.requested(correlation, "summary", NOW);
  ack.accepted(correlation, "summary", "request", NOW + 1);
  ack.observe([{...base,ts:new Date(NOW+2).toISOString(),type:"routine_started",
    payload:{routine:"summary",run_id:"mixed",trigger:"manual"}}], NOW+2, validateEvent);
  assert.deepEqual({state:ack.get(correlation,"summary").state,
    agent_id:ack.get(correlation,"summary").agent_id},
  {state:"running",agent_id:"steward:dynamic-runner"});
  ack.observe([{...base,ts:new Date(NOW+3).toISOString(),type:"routine_finished",
    payload:{routine:"summary",run_id:"mixed",outcome:"ok",artifacts:[],duration_s:1}}],
  NOW+3, validateEvent);
  assert.equal(ack.get(correlation,"summary").state, "completed");
  assert.equal(ack.get({agent_id:"codex:active-owner"},"summary"), null);
});

test("acknowledgement chooses newest terminal time and deterministic equal-time failure", () => {
  const lifecycle = (type, ts, payload) => ({v:0,source:"steward",project:"life",agent_id:"a",
    ts:new Date(ts).toISOString(),type,payload:{routine:"summary",run_id:"truth",...payload}});
  for (const terminals of [
    [lifecycle("routine_failed",NOW+4,{error:"newest"}),
      lifecycle("routine_finished",NOW+3,{outcome:"ok",artifacts:[],duration_s:1})],
    [lifecycle("routine_finished",NOW+4,{outcome:"ok",artifacts:[],duration_s:1}),
      lifecycle("routine_failed",NOW+4,{error:"tie failure"})],
  ]) {
    const ack = routines.createAcknowledgements();
    ack.requested("a","summary",NOW); ack.accepted("a","summary","request",NOW+1);
    ack.observe([lifecycle("routine_started",NOW+2,{trigger:"manual"}), ...terminals],
      NOW+5, validateEvent);
    assert.equal(ack.get("a","summary").state, "failed");
    assert.equal(ack.get("a","summary").closedAt, NOW+4);
  }
});

test("terminal acknowledgement remains reactive to newer conflicts across publications", () => {
  const ack = routines.createAcknowledgements();
  const lifecycle = (type, ts, payload) => ({v:0,source:"steward",project:"life",agent_id:"a",
    ts:new Date(ts).toISOString(),type,payload:{routine:"summary",run_id:"reactive",...payload}});
  ack.requested("a","summary",NOW); ack.accepted("a","summary","request",NOW+1);
  ack.observe([lifecycle("routine_started",NOW+2,{trigger:"manual"}),
    lifecycle("routine_finished",NOW+3,{outcome:"ok",artifacts:[],duration_s:1})],
  NOW+3, validateEvent);
  assert.equal(ack.get("a","summary").state, "completed");
  ack.observe([lifecycle("routine_failed",NOW+4,{error:"late failure"})],
    NOW+4, validateEvent);
  assert.deepEqual({state:ack.get("a","summary").state,
    closedAt:ack.get("a","summary").closedAt}, {state:"failed",closedAt:NOW+4});
  ack.observe([lifecycle("routine_finished",NOW+4,
    {outcome:"ok",artifacts:[],duration_s:2})], NOW+5, validateEvent);
  assert.equal(ack.get("a","summary").state, "failed",
    "equal-time failure remains the deterministic conservative truth");
});

test("delayed earlier duplicate start makes retained terminal acknowledgement valid", () => {
  const ack = routines.createAcknowledgements();
  const base = {v:0,source:"steward",project:"life",agent_id:"a"};
  const event = (type, ts, payload) => ({...base,ts:new Date(ts).toISOString(),type,
    payload:{routine:"summary",run_id:"earlier-start",...payload}});
  ack.requested("a","summary",NOW); ack.accepted("a","summary","request",NOW+1);
  ack.observe([event("routine_started",NOW+4,{trigger:"manual"})], NOW+4, validateEvent);
  ack.observe([event("routine_finished",NOW+3,
    {outcome:"ok",artifacts:[],duration_s:1})], NOW+5, validateEvent);
  assert.equal(ack.get("a","summary").state, "running");
  ack.observe([event("routine_started",NOW+2,{trigger:"manual"})], NOW+6, validateEvent);
  assert.deepEqual({state:ack.get("a","summary").state,
    closedAt:ack.get("a","summary").closedAt}, {state:"completed",closedAt:NOW+3});
});

test("pending acknowledgement retains an initially-too-early close for a corrected start", () => {
  const ack = routines.createAcknowledgements();
  const base = {v:0,source:"steward",project:"life",agent_id:"a"};
  const event = (type, ts, payload) => ({...base,ts:new Date(ts).toISOString(),type,
    payload:{routine:"summary",run_id:"post-accept-correction",...payload}});
  ack.requested("a","summary",NOW); ack.accepted("a","summary","request",NOW+1);
  ack.observe([event("routine_started",NOW+4,{trigger:"manual"}),
    event("routine_finished",NOW+3,{outcome:"ok",artifacts:[],duration_s:1})],
  NOW+4, validateEvent);
  assert.equal(ack.get("a","summary").state, "running");
  ack.observe([event("routine_started",NOW+2,{trigger:"manual"})], NOW+5, validateEvent);
  assert.deepEqual({state:ack.get("a","summary").state,
    closedAt:ack.get("a","summary").closedAt}, {state:"completed",closedAt:NOW+3});
});

test("pending acknowledgement times out explicitly", () => {
  const ack = routines.createAcknowledgements(10); ack.accepted("a", "r", "q", NOW); ack.observe([], NOW+10);
  assert.equal(ack.get("a", "r").state, "unacknowledged");
});

test("an unacknowledged timeout reconciles late exact lifecycle evidence", () => {
  const ack = routines.createAcknowledgements(10);
  const cursor = eventCursor;
  const base = {v:0,source:"steward",project:"life",agent_id:"a"};
  const start = {...base,ts:new Date(NOW+20).toISOString(),type:"routine_started",
    payload:{routine:"summary",run_id:"late",trigger:"manual"}};
  const finish = {...base,ts:new Date(NOW+30).toISOString(),type:"routine_finished",
    payload:{routine:"summary",run_id:"late",outcome:"ok",artifacts:[],duration_s:10}};
  ack.requested("a","summary",NOW,{events:[],cursor:cursor(10)},validateEvent);
  ack.accepted("a","summary","request",NOW);
  ack.observe({events:[],cursor:cursor(11)}, NOW+10, validateEvent);
  assert.equal(ack.get("a","summary").state, "unacknowledged");
  ack.observe({events:[finish],cursor:cursor(12)}, NOW+31, validateEvent);
  assert.equal(ack.get("a","summary").state, "unacknowledged");
  ack.observe({events:[start],cursor:cursor(13)}, NOW+32, validateEvent);
  assert.deepEqual({state:ack.get("a","summary").state,
    closedAt:ack.get("a","summary").closedAt}, {state:"completed",closedAt:NOW+30});
});

test("an unacknowledged request blocks retry indefinitely and accepts its late lifecycle", () => {
  const ack = routines.createAcknowledgements(10, routines.MAX_ACKNOWLEDGEMENTS);
  const cursor = eventCursor;
  const start = {v:0,source:"steward",project:"life",agent_id:"a",
    ts:new Date(NOW+15).toISOString(),type:"routine_started",
    payload:{routine:"summary",run_id:"original",trigger:"manual"}};
  ack.requested("a", "summary", NOW, {events:[],cursor:cursor(10)}, validateEvent);
  ack.accepted("a", "summary", "request-one", NOW);
  ack.observe({events:[],cursor:cursor(11)}, NOW + 10, validateEvent);
  assert.equal(ack.get("a", "summary").state, "unacknowledged");
  assert.equal(ack.requested("a", "summary", NOW + 11,
    {events:[],cursor:cursor(11)}, validateEvent), false);
  ack.observe({events:[],cursor:cursor(999)}, NOW + 365 * 24 * 60 * 60 * 1000,
    validateEvent);
  assert.equal(ack.get("a", "summary").state, "unacknowledged");
  assert.equal(ack.requested("a", "summary", NOW + 365 * 24 * 60 * 60 * 1000 + 1,
    {events:[],cursor:cursor(999)}, validateEvent), false);
  assert.equal(ack.get("a", "summary").requestId, "request-one");
  ack.observe({events:[start],cursor:cursor(12)}, NOW + 16, validateEvent);
  assert.deepEqual({state:ack.get("a", "summary").state,
    run_id:ack.get("a", "summary").run_id}, {state:"running",run_id:"original"});
});

test("late exact lifecycle resolves permanent uncertainty without a retry boundary", () => {
  const ack = routines.createAcknowledgements(10, routines.MAX_ACKNOWLEDGEMENTS);
  const cursor = eventCursor;
  const lifecycle = (runId, offset) => ({v:0,source:"steward",project:"life",agent_id:"a",
    ts:new Date(NOW+offset).toISOString(),type:"routine_started",
    payload:{routine:"summary",run_id:runId,trigger:"manual"}});
  ack.requested("a", "summary", NOW, {events:[],cursor:cursor(10)}, validateEvent);
  ack.accepted("a", "summary", "request-one", NOW);
  ack.observe({events:[],cursor:cursor(11)}, NOW + 10, validateEvent);
  const original = lifecycle("original", 25);
  ack.observe({events:[],cursor:cursor(12)}, NOW + 30_000_000, validateEvent);
  assert.equal(ack.get("a", "summary").state, "unacknowledged");
  assert.equal(ack.requested("a", "summary", NOW + 30_000_001,
    {events:[],cursor:cursor(12)}, validateEvent), false);
  ack.observe({events:[original],cursor:cursor(13)}, NOW + 30_000_002, validateEvent);
  assert.deepEqual({state:ack.get("a", "summary").state,
    requestId:ack.get("a", "summary").requestId,
    run_id:ack.get("a", "summary").run_id},
  {state:"running",requestId:"request-one",run_id:"original"});
});

test("request refusal remains explicit and retry clears only request failure state", () => {
  const ack = routines.createAcknowledgements();
  ack.requested("a", "summary", NOW);
  ack.failed("a", "summary", "Steward refused the request (503)", NOW + 1);
  assert.deepEqual({state:ack.get("a", "summary").state,error:ack.get("a", "summary").error},
    {state:"request-failed",error:"Steward refused the request (503)"});
  assert.equal(ack.requested("a", "summary", NOW + 2), true);
  assert.equal(ack.get("a", "summary").state, "requesting");
  assert.equal(ack.get("a", "summary").error, undefined);
});

test("a second request cannot overwrite an active acknowledgement correlation", () => {
  const ack = routines.createAcknowledgements();
  assert.equal(ack.requested("a", "r", NOW), true);
  assert.equal(ack.requested("a", "r", NOW + 1), false);
  assert.equal(ack.get("a", "r").requestedAt, NOW);
  ack.accepted("a", "r", "q", NOW + 2);
  assert.equal(ack.requested("a", "r", NOW + 3), false);
  assert.equal(ack.get("a", "r").requestId, "q");
});

test("run now remains disabled for an exact active acknowledgement or observed run", () => {
  const local = {enabled:true}, authoritative = {enabled:true,retired:false};
  assert.equal(routines.runDisabled(local, authoritative, "loaded", null, {state:"finished"}), false);
  for (const state of ["requesting", "pending", "unacknowledged", "running"]) {
    assert.equal(routines.runDisabled(local, authoritative, "loaded", {state}, {state:"finished"}), true);
  }
  assert.equal(routines.runDisabled(local, authoritative, "loaded", null, {state:"running"}), true);
  assert.equal(routines.runDisabled(local, authoritative, "loaded", null, {state:"stale"}), true);
  assert.equal(routines.runDisabled(local, authoritative, "loaded", null,
    [{state:"finished"},{state:"stale"}]), true);
  assert.equal(routines.runDisabled(local, authoritative, "loaded", {state:"completed"}, {state:"finished"}), false);
});

test("Steward declarations supply authoritative next fires without a Burrow proxy", async () => {
  let call;
  const declared = await routines.fetchDeclarations({url:"http://steward:8801/",token:"secret"}, async (url, options) => {
    call={url,options}; return {status:200,json:async()=>({routines:[
      {resident:"life-agent",routine:"summary",next_fire:"2026-08-26T07:00:00+02:00",enabled:true,retired:false},
      {resident:"life-agent",routine:"off",next_fire:null,enabled:false,retired:false},
    ]})};
  });
  assert.equal(call.url, "http://steward:8801/routines");
  assert.equal(call.options.headers.Authorization, "Bearer secret"); assert.equal(call.options.credentials, "omit");
  assert.equal(declared.get(key("life-agent", "summary")).next_fire, "2026-08-26T07:00:00+02:00");
  assert.equal(declared.get(key("life-agent", "off")).next_fire, null);
});

test("authoritative disabled and retired declarations disable run now", () => {
  const local = { enabled:true };
  assert.equal(routines.canRun(local, {enabled:true,retired:false}), true);
  assert.equal(routines.canRun(local, {enabled:false,retired:false}), false);
  assert.equal(routines.canRun(local, {enabled:true,retired:true}), false);
  assert.equal(routines.canRun({enabled:false}, {enabled:true,retired:false}), false);
  assert.equal(routines.canRunAuthoritatively(local, null, "loaded"), false);
  assert.equal(routines.canRunAuthoritatively(local, {enabled:true,retired:false}, "loading"), false);
  assert.equal(routines.canRunAuthoritatively(local, {enabled:true,retired:false}, "loaded"), true);
});

test("run now sends token only to steward and validates acknowledgement", async () => {
  let call; const response = await routines.runNow({url:"http://steward:8801/",token:"secret"}, "life-agent", "daily-summary",
    async (url, options) => { call={url,options}; return {status:202,json:async()=>({status:"accepted",request_id:"q1"})}; });
  assert.equal(response.request_id, "q1");
  assert.equal(call.url, "http://steward:8801/residents/life-agent/routines/daily-summary/run");
  assert.equal(call.options.headers.Authorization, "Bearer secret"); assert.equal(call.options.credentials, "omit");
});

test("run-now errors distinguish authoritative refusal from ambiguous delivery", async () => {
  let parsed = false;
  await assert.rejects(routines.runNow(
    {url:"http://steward",token:"secret"}, "life-agent", "summary",
    async () => ({status:409,json:async()=>{ parsed = true; return {}; }})), error => {
      assert.equal(error.definitive, true);
      assert.match(error.message, /refused.*409/);
      return true;
    });
  assert.equal(parsed, false, "a non-202 response is authoritative without parsing a body");

  for (const fetchImpl of [
    async () => { throw new Error("network unreachable"); },
    async () => ({status:202,json:async()=>{ throw new Error("truncated response"); }}),
  ]) {
    await assert.rejects(routines.runNow(
      {url:"http://steward",token:"secret"}, "life-agent", "summary", fetchImpl),
    error => {
      assert.notEqual(error.definitive, true);
      return true;
    });
  }
});

test("ambiguous failure preserves a start seen before the request returned", () => {
  const ack = routines.createAcknowledgements();
  const start = {v:0,ts:new Date(NOW+2).toISOString(),source:"steward",
    agent_id:"a",project:"life",type:"routine_started",
    payload:{routine:"summary",run_id:"accepted-before-timeout",trigger:"manual"}};
  ack.request("a", "summary", NOW, [], validateEvent);
  ack.observe([start], NOW + 2, validateEvent);
  ack.uncertain("a", "summary", "Steward request timed out", NOW + 3);
  assert.equal(ack.get("a", "summary").state, "running");
  assert.equal(ack.get("a", "summary").run_id, "accepted-before-timeout");
});

test("ambiguous failure blocks retry and reconciles a start seen afterwards", () => {
  const ack = routines.createAcknowledgements();
  const start = {v:0,ts:new Date(NOW+2).toISOString(),source:"steward",
    agent_id:"a",project:"life",type:"routine_started",
    payload:{routine:"summary",run_id:"accepted-after-timeout",trigger:"manual"}};
  ack.request("a", "summary", NOW, [], validateEvent);
  ack.uncertain("a", "summary", "network unreachable", NOW + 1);
  assert.equal(ack.get("a", "summary").state, "uncertain");
  assert.equal(ack.availability("a", "summary").reason, "active");
  ack.observe([start], NOW + 2, validateEvent);
  assert.equal(ack.get("a", "summary").state, "running");
  assert.equal(ack.get("a", "summary").run_id, "accepted-after-timeout");
});

test("definitive refusal is retryable and ignores unrelated lifecycle events", () => {
  const ack = routines.createAcknowledgements();
  ack.request("a", "summary", NOW, [], validateEvent);
  ack.observe([{v:0,ts:new Date(NOW+1).toISOString(),source:"steward",
    agent_id:"other",project:"life",type:"routine_started",
    payload:{routine:"summary",run_id:"unrelated",trigger:"manual"}}], NOW+1, validateEvent);
  ack.failed("a", "summary", "Steward refused the request (409)", NOW+2);
  assert.equal(ack.get("a", "summary").state, "request-failed");
  assert.equal(ack.availability("a", "summary").ok, true);
});

test("Steward declaration and run keys enforce the lowercase slug contract", async () => {
  const declarations = await routines.fetchDeclarations({url:"http://steward",token:"secret"},
    async () => ({status:200,json:async()=>({routines:[
      {resident:"life-agent",routine:"daily-summary",next_fire:null,enabled:true,retired:false},
      {resident:"Life-Agent",routine:"daily-summary",next_fire:null,enabled:true,retired:false},
      {resident:"life-agent",routine:"daily_summary",next_fire:null,enabled:true,retired:false},
    ]})}));
  assert.deepEqual([...declarations.keys()], [key("life-agent", "daily-summary")]);
  for (const [resident, routine] of [["Life-Agent", "daily-summary"],
    ["life-agent", "daily_summary"]]) {
    await assert.rejects(routines.runNow({url:"http://steward",token:"secret"}, resident, routine,
      async () => { throw new Error("invalid keys must fail before fetch"); }), /lowercase slugs/);
  }
});

test("Steward reads and writes abort and reject on a bounded deadline", async () => {
  for (const operation of [
    (fetchImpl, timing) => routines.fetchDeclarations({url:"http://steward",token:"secret"}, fetchImpl, timing),
    (fetchImpl, timing) => routines.runNow({url:"http://steward",token:"secret"}, "life-agent", "summary", fetchImpl, timing),
  ]) {
    let expire, cleared = 0, signal;
    const request = operation((_url, options) => {
      signal = options.signal;
      return new Promise(() => {});
    }, { timeoutMs: 25, setTimeout(fn) { expire = fn; return 91; },
      clearTimeout(id) { assert.equal(id, 91); cleared++; } });
    expire();
    await assert.rejects(request, /timed out/);
    assert.equal(signal.aborted, true);
    assert.equal(cleared, 1, "the deadline timer is cleaned after failure");
  }
});

test("successful Steward requests clean their deadline timers", async () => {
  let cleared = 0;
  await routines.fetchDeclarations({url:"http://steward",token:"secret"},
    async () => ({status:200,json:async()=>({routines:[]})}),
    { setTimeout() { return 17; }, clearTimeout(id) { assert.equal(id, 17); cleared++; } });
  assert.equal(cleared, 1);
});
