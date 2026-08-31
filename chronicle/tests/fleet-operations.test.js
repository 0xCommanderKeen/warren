"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fleet = require("../viewer/fleet-operations.js");
const routines = require("../viewer/routine-ledger.js");

const BASE = Date.parse("2026-08-25T12:00:00.000Z");
const routineKey = (agent, routine) => JSON.stringify([agent, routine]);
function event(index, overrides = {}) {
  return { v: 0, ts: new Date(BASE + index).toISOString(), source: "claude-code",
    agent_id: "agent-a", project: "burrow", type: "tool_called",
    payload: { tool: "Read", detail: `file-${index}` }, ...overrides };
}
function routine(index, type, runId, overrides = {}) {
  const payload = type === "routine_started" ? {routine:"summary",run_id:runId,trigger:"schedule"} :
    {routine:"summary",run_id:runId,outcome:"ok",artifacts:[],duration_s:1};
  return event(index, {source:"steward",type,payload,...overrides});
}

test("recent activity stays bounded while incremental batches remain searchable", () => {
  const state = fleet.createFleetState();
  for (let i = 0; i < fleet.MAX_RECENT_EVENTS + 37; i++) fleet.foldFleet(state, [event(i)]);
  assert.equal(state.recent.length, fleet.MAX_RECENT_EVENTS);
  assert.equal(state.recent[0].event.payload.detail, `file-${fleet.MAX_RECENT_EVENTS + 36}`);
  assert.equal(fleet.filterRecent(state, { query: "file-236", project: "burrow",
    source: "claude-code", state: "working", villager: "agent-a" }).length, 1);
  assert.equal(fleet.filterRecent(state, { project: "life" }).length, 0);
});

test("activity merges journals into one newest-200 append-ordered window", () => {
  const state = fleet.createFleetState();
  const ordinary = Array.from({length:200}, (_, index) => event(index, {
    // Producer clocks are deliberately reversed and heavily tied. Append
    // ordinals, not timestamps, own Fleet recency.
    ts: new Date(BASE + (index % 2)).toISOString(),
    agent_id:`ordinary-${index}`, project:index % 2 ? "odd" : "even",
  }));
  let ordinal = 0;
  fleet.foldFleet(state, ordinary, 0, 0, BASE, () => String(++ordinal));
  const journals = Array.from({length:40}, (_, index) => {
    const journal = event(-10_000 - index, {source:"steward",
      agent_id:`journal-${index}`,project:index % 2 ? "odd" : "even",
      type:"journal_written",payload:{routine:"close-of-day",
        day:`2026-07-${String(index % 28 + 1).padStart(2,"0")}`,
        path:`/journal/${index}.md`}});
    return {event:journal,ts:Date.parse(journal.ts),agent_id:journal.agent_id,
      project:journal.project,source:journal.source,state:"journal observed",
      ordinal:String(201 + index)};
  });
  const merged = fleet.recentActivity(state, journals);
  assert.equal(merged.length, fleet.MAX_RECENT_EVENTS);
  assert.deepEqual(merged.slice(0,40).map(entry => entry.agent_id),
    journals.slice().reverse().map(entry => entry.agent_id));
  assert.equal(merged.at(-1).ordinal,"41",
    "the forty journals displace the forty oldest ordinary entries after merging");
  assert.equal(new Set(merged.map(entry => entry.event)).size,merged.length);
  assert.equal(fleet.filterEntries(merged,{source:"steward"}).length,40);
  assert.equal(fleet.filterEntries(merged,{source:"claude-code"}).length,160);
  assert.equal(fleet.filterEntries(merged,{project:"odd"}).length,100);
  assert.deepEqual(fleet.optionsForEntries(merged,"source"),["claude-code","steward"]);
});

test("activity merge is stable for equal ordinals and never duplicates one event", () => {
  const state = fleet.createFleetState(), shared = event(0);
  fleet.foldFleet(state,[shared],0,0,BASE,()=>"7");
  const other = {...event(999),agent_id:"other"};
  const merged = fleet.recentActivity(state,[
    {event:shared,ordinal:"7"},
    {event:other,ordinal:"7"},
  ]);
  assert.deepEqual(merged.map(entry=>entry.event),[shared,other]);
});

test("outstanding needs-human means the latest retained event still needs a human", () => {
  const state = fleet.createFleetState();
  fleet.foldFleet(state, [
    event(1, { agent_id: "waiting", project: "life", type: "needs_human",
      payload: { message: "Choose a time" } }),
    event(2, { agent_id: "resolved", type: "needs_human", payload: { message: "Approve" } }),
    event(3, { agent_id: "resolved", type: "idle", payload: {} }),
  ], 2);
  assert.deepEqual(fleet.outstandingNeeds(state).map(x => x.agent_id), ["waiting"]);
  assert.equal(state.malformed, 2);
});

test("malformed routine diagnostics remain separately observable", () => {
  const state = fleet.createFleetState();
  fleet.foldFleet(state, [], 2, 1);
  assert.equal(state.malformed, 2);
  assert.equal(state.routineMalformed, 1);
});

test("routine lifecycle stays in its separate bounded ledger", () => {
  const state = fleet.createFleetState();
  fleet.foldFleet(state, [event(1), event(2, { type:"routine_started",
    payload:{routine:"summary",run_id:"run-1",trigger:"manual"} })]);
  assert.deepEqual(state.recent.map(entry => entry.event.type), ["tool_called"]);
  assert.deepEqual(state.routineRecent.map(entry => entry.event.type), ["routine_started"]);
  assert.equal(fleet.filterRecent(state, {}).length, 1);
});

test("folded routine retention keeps an open start until its close and bounds completed history", () => {
  const state = fleet.createFleetState();
  fleet.foldFleet(state, [routine(0, "routine_started", "open")]);
  for (let i = 1; i <= 35; i++) fleet.foldFleet(state, [
    routine(i * 2, "routine_started", `done-${i}`),
    routine(i * 2 + 1, "routine_finished", `done-${i}`),
  ]);
  assert.ok(state.routineRecent.some(entry => entry.event.payload.run_id === "open"),
    "completed traffic cannot evict a run whose close has not arrived");
  assert.equal(state.routineRecent.length, 1 + fleet.MAX_ROUTINE_RUNS * 2);
  fleet.foldFleet(state, [routine(1000, "routine_finished", "open")]);
  const open = state.routineRecent.filter(entry => entry.event.payload.run_id === "open");
  assert.deepEqual(open.map(entry => entry.event.type).sort(), ["routine_finished", "routine_started"]);
  assert.equal(state.routineRecent.length, fleet.MAX_ROUTINE_RUNS * 2);
});

test("routine retention deduplicates replays, pairs close-before-start, and evicts old overflow diagnostically", () => {
  const state = fleet.createFleetState();
  const start = routine(20, "routine_started", "same");
  fleet.foldFleet(state, [start, {...start, ts:new Date(BASE + 21).toISOString()}]);
  assert.equal(state.routineRecent.length, 1);
  fleet.foldFleet(state, [routine(30, "routine_finished", "late"),
    routine(29, "routine_started", "late")]);
  assert.equal(state.routineRecent.filter(entry => entry.event.payload.run_id === "late").length, 2);
  fleet.foldFleet(state, [routine(40, "routine_finished", "separate")]);
  fleet.foldFleet(state, [routine(39, "routine_started", "separate")]);
  assert.deepEqual(state.routineRecent.filter(entry => entry.event.payload.run_id === "separate")
    .map(entry => entry.event.type).sort(), ["routine_finished", "routine_started"],
  "bounded orphan staging still supports evidence delivered in separate polls");
  const bounded = fleet.createFleetState();
  for (let i = 0; i <= fleet.MAX_OPEN_RUNS; i++) {
    fleet.foldFleet(bounded, [routine(i, "routine_started", `open-${i}`)]);
  }
  assert.equal(bounded.routineRecent.length, fleet.MAX_OPEN_RUNS);
  assert.equal(bounded.routineMalformed, 0);
  assert.equal(bounded.routineCapacityDropped, 1);
  assert.equal(bounded.routineRecent.some(entry => entry.event.payload.run_id === "open-0"), false);
  assert.equal(bounded.routineRecent.some(entry => entry.event.payload.run_id === `open-${fleet.MAX_OPEN_RUNS}`), true);
});

test("a saturated ledger stages a close and promotes its later start atomically", () => {
  const state = fleet.createFleetState();
  for (let i = 0; i < fleet.MAX_ROUTINE_KEYS; i++) {
    fleet.foldFleet(state, [
      routine(i * 2, "routine_started", `done-${i}`, {agent_id:`agent-${i}`,
        payload:{routine:`routine-${i}`,run_id:`done-${i}`,trigger:"schedule"}}),
      routine(i * 2 + 1, "routine_finished", `done-${i}`, {agent_id:`agent-${i}`,
        payload:{routine:`routine-${i}`,run_id:`done-${i}`,outcome:"ok",artifacts:[],duration_s:1}}),
    ]);
  }
  const close = routine(10_001, "routine_finished", "reverse", {agent_id:"new-agent",
    payload:{routine:"new-routine",run_id:"reverse",outcome:"ok",artifacts:[],duration_s:1}});
  const start = routine(10_000, "routine_started", "reverse", {agent_id:"new-agent",
    payload:{routine:"new-routine",run_id:"reverse",trigger:"schedule"}});
  const beforeKeys = [...state.routineKeys];
  fleet.foldFleet(state, [close]);
  assert.deepEqual(state.routineKeys, beforeKeys,
    "an orphan must not consume or displace renderable key capacity");
  assert.equal(state.routineOrphans.length, 1);
  assert.equal(state.routineRecent.some(entry => entry.event.payload.run_id === "reverse"), false);
  assert.equal(state.routineMalformed, 0);
  fleet.foldFleet(state, [start]);
  const retained = state.routineRecent.filter(entry =>
    entry.event.agent_id === "new-agent" && entry.event.payload.run_id === "reverse");
  assert.deepEqual(retained.map(entry => entry.event.type).sort(),
    ["routine_finished", "routine_started"]);
  assert.equal(routines.project(retained.map(entry => entry.event), BASE + 20_000)
    .byRoutine.get(routineKey("new-agent", "new-routine"))[0].state, "finished");
  assert.equal(state.routineKeys.length, fleet.MAX_ROUTINE_KEYS);
  assert.equal(state.routineOrphans.length, 0);
  assert.equal(state.routineMalformed, 0);
});

test("orphan staging is separately bounded by ingestion recency and shared terminal truth", () => {
  const state = fleet.createFleetState();
  for (let i = 0; i <= fleet.MAX_ROUTINE_ORPHANS; i++) {
    fleet.foldFleet(state, [routine(i, "routine_finished", `orphan-${i}`, {
      agent_id:`orphan-agent-${i}`,
      payload:{routine:`orphan-routine-${i}`,run_id:`orphan-${i}`,
        outcome:"ok",artifacts:[],duration_s:1},
    })]);
  }
  assert.equal(state.routineOrphans.length, fleet.MAX_ROUTINE_ORPHANS);
  assert.equal(state.routineKeys.length, 0);
  assert.equal(state.routineMalformed, 0);
  assert.equal(state.routineCapacityDropped, 1,
    "valid staging overflow is an explicit capacity diagnostic");
  assert.equal(state.routineOrphans.some(entry =>
    entry.event.payload.run_id === "orphan-0"), false);

  const runId = "orphan-1";
  const failure = routine(1, "routine_failed", runId, {agent_id:"orphan-agent-1",
    payload:{routine:"orphan-routine-1",run_id:runId,error:"conservative"}});
  fleet.foldFleet(state, [failure]);
  const staged = state.routineOrphans.find(entry => entry.event.payload.run_id === runId);
  assert.equal(staged.event.type, "routine_failed",
    "staging uses the shared equal-time conservative terminal comparator");
});

test("more than 200 routine keys retain current and newest evidence and allow re-entry", () => {
  const state = fleet.createFleetState();
  for (let i = 0; i <= fleet.MAX_ROUTINE_KEYS; i++) {
    fleet.foldFleet(state, [routine(i, "routine_started", `run-${i}`, {
      agent_id:`agent-${i}`, payload:{routine:`routine-${i}`,run_id:`run-${i}`,trigger:"schedule"},
    })]);
  }
  assert.equal(state.routineKeys.length, fleet.MAX_ROUTINE_KEYS);
  assert.equal(state.routineKeys.some(key => key.includes("routine-0")), false);
  assert.equal(state.routineKeys.some(key => key.includes(`routine-${fleet.MAX_ROUTINE_KEYS}`)), true);
  assert.equal(state.routineMalformed, 0);
  assert.equal(state.routineCapacityDropped, 1);

  fleet.foldFleet(state, [routine(10_000, "routine_started", "returned", {
    agent_id:"agent-0", payload:{routine:"routine-0",run_id:"returned",trigger:"schedule"},
  })]);
  assert.equal(state.routineKeys.some(key => key.includes("routine-0")), true,
    "an evicted key can return when it has newer evidence");
  assert.equal(state.routineKeys.length, fleet.MAX_ROUTINE_KEYS);
});

test("stale start-only keys yield capacity to new completed truth but keep bounded pairing", () => {
  const state = fleet.createFleetState();
  const now = BASE + fleet.DEFAULT_STALE_MS + 10_000;
  for (let i = 0; i < fleet.MAX_ROUTINE_KEYS; i++) {
    fleet.foldFleet(state, [routine(i, "routine_started", `stale-${i}`, {
      agent_id:`stale-agent-${i}`,
      payload:{routine:`stale-routine-${i}`,run_id:`stale-${i}`,trigger:"schedule"},
    })], 0, 0, now);
  }
  for (let i = 0; i < 12; i++) {
    const index = fleet.DEFAULT_STALE_MS + 100 + i * 2;
    fleet.foldFleet(state, [
      routine(index, "routine_started", `complete-${i}`, {agent_id:`complete-agent-${i}`,
        payload:{routine:`complete-routine-${i}`,run_id:`complete-${i}`,trigger:"schedule"}}),
      routine(index + 1, "routine_finished", `complete-${i}`, {agent_id:`complete-agent-${i}`,
        payload:{routine:`complete-routine-${i}`,run_id:`complete-${i}`,
          outcome:"ok",artifacts:[],duration_s:1}}),
    ], 0, 0, now);
  }
  assert.equal(state.routineKeys.length, fleet.MAX_ROUTINE_KEYS);
  for (let i = 0; i < 12; i++) {
    assert.ok(state.routineKeys.some(key => key.includes(`complete-routine-${i}`)),
      `completed routine ${i} must not be crowded out by ancient starts`);
  }
  const staged = state.routineRecent.find(entry =>
    entry.event.type === "routine_started" && entry.event.payload.run_id.startsWith("stale-"));
  assert.ok(staged, "some bounded stale evidence remains available for late pairing");
  const close = routine(500, "routine_finished", staged.event.payload.run_id, {
    agent_id:staged.event.agent_id, payload:{routine:staged.event.payload.routine,
      run_id:staged.event.payload.run_id,outcome:"ok",artifacts:[],duration_s:1},
  });
  fleet.foldFleet(state, [close], 0, 0, now);
  assert.deepEqual(state.routineRecent.filter(entry =>
    entry.event.payload.run_id === staged.event.payload.run_id)
    .map(entry => entry.event.type).sort(), ["routine_finished", "routine_started"]);
});

test("future-skewed orphan closes cannot exhaust key capacity ahead of real evidence", () => {
  const state = fleet.createFleetState();
  for (let i = 0; i < fleet.MAX_ROUTINE_KEYS; i++) {
    fleet.foldFleet(state, [routine(10_000_000 + i, "routine_finished", `orphan-${i}`, {
      agent_id:`orphan-agent-${i}`,
      payload:{routine:`orphan-routine-${i}`,run_id:`orphan-${i}`,outcome:"ok",artifacts:[],duration_s:1},
    })]);
  }
  const realStart = routine(-10_000_000, "routine_started", "real", {
    agent_id:"real-agent", payload:{routine:"real-routine",run_id:"real",trigger:"schedule"},
  });
  const realClose = routine(-9_999_999, "routine_finished", "real", {
    agent_id:"real-agent",
    payload:{routine:"real-routine",run_id:"real",outcome:"ok",artifacts:[],duration_s:1},
  });
  fleet.foldFleet(state, [realStart, realClose]);
  assert.deepEqual(state.routineRecent.filter(entry => entry.event.payload.run_id === "real")
    .map(entry => entry.event.type).sort(), ["routine_finished", "routine_started"]);
  assert.equal(state.routineKeys.length, 1,
    "close-only staging never manufactures renderable keys");
});

test("ingestion order, not producer clocks, selects recency and late orphans preserve completion", () => {
  const state = fleet.createFleetState();
  for (let i = 0; i < fleet.MAX_ROUTINE_KEYS; i++) {
    fleet.foldFleet(state, [routine(1_000_000 + i, "routine_started", `open-${i}`, {
      agent_id:`agent-${i}`, payload:{routine:`routine-${i}`,run_id:`open-${i}`,trigger:"schedule"},
    })]);
  }
  fleet.foldFleet(state, [routine(-1_000_000, "routine_started", "new", {
    agent_id:"new-agent", payload:{routine:"new-routine",run_id:"new",trigger:"schedule"},
  })]);
  assert.equal(state.routineKeys.some(key => key.includes("new-routine")), true,
    "new ingestion wins even when its producer timestamp is old");

  const completed = fleet.createFleetState();
  fleet.foldFleet(completed, [routine(1, "routine_started", "done"),
    routine(2, "routine_finished", "done")]);
  fleet.foldFleet(completed, [routine(9_000_000, "routine_finished", "evicted-start")]);
  assert.deepEqual(completed.routineRecent.filter(entry => entry.event.payload.run_id === "done")
    .map(entry => entry.event.type).sort(), ["routine_finished", "routine_started"],
    "an orphan is staging evidence, not a replacement for rendered completion");
});

test("retention and projection keep the newest terminal truth despite delayed conflicts", () => {
  const state = fleet.createFleetState();
  const started = routine(10, "routine_started", "conflict");
  const newestFailure = routine(30, "routine_failed", "conflict", {payload:{
    routine:"summary",run_id:"conflict",error:"newest truth",duration_s:20,
  }});
  const delayedOlderFinish = routine(20, "routine_finished", "conflict", {payload:{
    routine:"summary",run_id:"conflict",outcome:"ok",artifacts:["stale.md"],duration_s:10,
  }});
  fleet.foldFleet(state, [started, newestFailure]);
  fleet.foldFleet(state, [delayedOlderFinish]);
  const retained = state.routineRecent.filter(entry =>
    entry.event.payload.run_id === "conflict");
  assert.deepEqual(retained.map(entry => entry.event.type).sort(),
    ["routine_failed", "routine_started"]);
  const projected = routines.project(retained.slice().reverse().map(entry => entry.event),
    BASE + 40).byRoutine.get(routineKey("agent-a", "summary"))[0];
  assert.deepEqual({state:projected.state,error:projected.error,artifacts:projected.artifacts},
    {state:"failed",error:"newest truth",artifacts:[]});
});

test("retention and projection share earliest-start truth for delayed duplicates", () => {
  const state = fleet.createFleetState();
  const laterStart = routine(30, "routine_started", "delayed-start");
  const finish = routine(20, "routine_finished", "delayed-start");
  const delayedEarlierStart = routine(10, "routine_started", "delayed-start");
  fleet.foldFleet(state, [laterStart, finish]);
  assert.equal(routines.project(state.routineRecent.map(entry => entry.event), BASE + 40)
    .byRoutine.get(routineKey("agent-a", "summary"))[0].state, "running");
  fleet.foldFleet(state, [delayedEarlierStart]);
  const retained = state.routineRecent.filter(entry =>
    entry.event.payload.run_id === "delayed-start").map(entry => entry.event);
  assert.deepEqual(retained.map(event => event.ts).sort(),
    [delayedEarlierStart.ts, finish.ts].sort());
  const projected = routines.project(retained, BASE + 40)
    .byRoutine.get(routineKey("agent-a", "summary"))[0];
  assert.deepEqual({state:projected.state,started_at:projected.started_at,
    closed_at:projected.closed_at},
  {state:"finished",started_at:BASE + 10,closed_at:BASE + 20});
});

test("equal-time duplicate starts use the same deterministic tie-break", () => {
  const scheduled = routine(10, "routine_started", "start-tie");
  const manual = {...scheduled,payload:{...scheduled.payload,trigger:"manual"}};
  for (const starts of [[scheduled, manual], [manual, scheduled]]) {
    const state = fleet.createFleetState();
    fleet.foldFleet(state, starts);
    const retained = state.routineRecent.map(entry => entry.event);
    assert.equal(retained.length, 1);
    assert.equal(retained[0].payload.trigger, "manual");
    assert.equal(routines.project(retained, BASE + 20)
      .byRoutine.get(routineKey("agent-a", "summary"))[0].trigger, "manual");
  }
});

test("equal-time terminal conflicts use the same deterministic failure tie-break", () => {
  const start = routine(10, "routine_started", "tie");
  const finished = routine(20, "routine_finished", "tie");
  const failed = routine(20, "routine_failed", "tie", {payload:{
    routine:"summary",run_id:"tie",error:"conservative truth",
  }});
  for (const terminals of [[finished, failed], [failed, finished]]) {
    const state = fleet.createFleetState();
    fleet.foldFleet(state, [start, ...terminals]);
    const retained = state.routineRecent.map(entry => entry.event);
    assert.equal(retained.find(event => event.type !== "routine_started").type,
      "routine_failed");
    const projected = routines.project(retained, BASE + 30)
      .byRoutine.get(routineKey("agent-a", "summary"))[0];
    assert.equal(projected.state, "failed");
    assert.equal(projected.error, "conservative truth");
  }
});

test("newest incomplete runs replace oldest and a retained later close still pairs", () => {
  const state = fleet.createFleetState();
  for (let i = 0; i <= fleet.MAX_OPEN_RUNS; i++) {
    fleet.foldFleet(state, [routine(i, "routine_started", `open-${i}`)]);
  }
  fleet.foldFleet(state, [routine(100, "routine_finished", "open-1")]);
  const paired = state.routineRecent.filter(entry => entry.event.payload.run_id === "open-1");
  assert.deepEqual(paired.map(entry => entry.event.type).sort(), ["routine_finished", "routine_started"]);
  assert.equal(state.routineRecent.filter(entry => entry.event.type === "routine_started").length,
    fleet.MAX_OPEN_RUNS);
});

test("capability truth distinguishes declaration, absence, invalidity, and outage", () => {
  assert.equal(fleet.capabilityStatus({ id: "mail", status_ref: "owner:status" }), "configured");
  assert.equal(fleet.capabilityStatus(null), "missing");
  assert.equal(fleet.capabilityStatus({ id: "mail", status_ref: "invalid" }), "configured",
    "opaque locator text cannot manufacture invalid state");
  assert.equal(fleet.capabilityStatus({ id: "mail", status_ref: "externally-unavailable" }),
    "configured", "opaque locator text cannot manufacture outage state");
  assert.equal(fleet.capabilityStatus({ id: "mail", status_ref: "owner:status" }, false),
    "externally unavailable");
  assert.equal(fleet.capabilityStatus({ id: "missing-status" }), "invalid");
  assert.equal(fleet.capabilityStatus({ id: "mail", status_ref: "owner:status" }, true, true),
    "invalid");
  const directory = fleet.residentDirectory([{ valid: true, home: 3 }, { file: "partial.json" }],
    [{ file: "broken.json", path: "$.memory", message: "is required" }]);
  assert.equal(directory.residents[0].home, 3);
  assert.equal(directory.invalid[0].status, "invalid");
  assert.equal(directory.invalid.length, 2);
  assert.equal(directory.status, "invalid");
  assert.equal(fleet.residentDirectory([], [], true).status, "missing");
  assert.equal(fleet.residentDirectory([], [], false).status, "externally unavailable");
});

test("safe diagnostic residents remain separate and drive row-level invalid state", () => {
  const partial = { file: "unsafe.resident.json", valid: false, diagnostic: true,
    declared_home: 4, match: { agent_id: "safe-agent" },
    capabilities: { skills: [{ id: "summary", status_ref: "config:summary", invalid: true,
      diagnostic_path: "$.skills[0].access_token" }] } };
  const directory = fleet.residentDirectory([], [{ file: partial.file,
    path: "$.skills[0].access_token", message: "credential field is forbidden" }], true, [partial]);
  assert.deepEqual(directory.residents, []);
  assert.equal(directory.diagnosticResidents[0], partial);
  assert.equal(fleet.capabilityStatus(partial.capabilities.skills[0], true,
    partial.capabilities.skills[0].invalid), "invalid");
});

test("keyboard tab navigation wraps and supports home/end", () => {
  assert.equal(fleet.moveFocus(0, "ArrowRight", 3), 1);
  assert.equal(fleet.moveFocus(2, "ArrowDown", 3), 0);
  assert.equal(fleet.moveFocus(0, "ArrowLeft", 3), 2);
  assert.equal(fleet.moveFocus(1, "Home", 3), 0);
  assert.equal(fleet.moveFocus(1, "End", 3), 2);
  assert.equal(fleet.moveFocus(1, "Enter", 3), 1);
});
