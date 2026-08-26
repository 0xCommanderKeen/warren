"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createBrowserRuntime } = require("../viewer/browser-runtime.js");
const projection = require("../viewer/projection.js");

const NOW = Date.parse("2026-08-25T12:00:00.000Z");
const BOOT = "0123456789abcdef0123456789abcdef";
const CURSOR = `v1:${BOOT}:1:2:3:5000`;

function line(fields) {
  return JSON.stringify({v:0,ts:"2026-08-25T11:59:00.000Z",source:"steward",
    agent_id:"codex:pip",project:"life",payload:{},...fields});
}

function runtimeFor(lines, projections) {
  return createBrowserRuntime({now:()=>NOW,EventSource:null,
    setTimeout:()=>1,clearTimeout(){},
    fetch:async url => url === "/villagers" ? {ok:false} : {
      ok:true,
      headers:{get:name => name === "X-Burrow-Cursor" ? CURSOR : null},
      text:async()=>lines.join("\n"),
    },
    onProjection:view=>projections.push(view),warn(){},
  });
}

test("grouped bootstrap projects a routine-only resident outside the 4,000-record tail", async () => {
  const started = line({type:"routine_started",
    payload:{routine:"heartbeat",run_id:"hourly-1",trigger:"schedule"}});
  const unrelated = Array.from({length:4000}, (_, index) => line({
    ts:new Date(NOW - 1000 + index % 999).toISOString(),
    agent_id:`codex:gone-${index}`,type:"session_ended",source:"codex",
  }));
  const projections = [], runtime = runtimeFor([started, ...unrelated], projections);
  await runtime.poll();
  const pip = runtime.snapshot().villagers.find(item => item.id === "codex:pip");
  assert.ok(pip, "complete grouped routine evidence can create the villager");
  assert.deepEqual({state:pip.state,lastLine:pip.lastLine,history:pip.events.map(e=>e.type)},
    {state:"working",lastLine:"woke for heartbeat",history:["routine_started"]});
  assert.ok(projections.length >= 1);
});

test("grouped reset preserves routine terminal state and visible history without a session", async () => {
  const lines = [
    line({ts:"2026-08-25T11:58:00.000Z",type:"routine_started",
      payload:{routine:"heartbeat",run_id:"hourly-2",trigger:"schedule"}}),
    line({type:"routine_finished",
      payload:{routine:"heartbeat",run_id:"hourly-2",outcome:"ok",artifacts:[],duration_s:1.5}}),
  ];
  const runtime = runtimeFor(lines, []);
  await runtime.poll();
  const [pip] = runtime.snapshot().villagers;
  assert.deepEqual({id:pip.id,state:pip.state,lastLine:pip.lastLine,
    history:pip.events.map(event=>event.type)}, {
    id:"codex:pip",state:"resting",lastLine:"finished heartbeat, ok in 1.5s",
    history:["routine_started","routine_finished"],
  });
});

test("incremental polling preserves canonical routine lifecycle ordering", async () => {
  const routine = (type, ts, payload) => line({type, ts,
    payload:{routine:"heartbeat",run_id:"ordered",...payload}});
  const scenarios = [
    {records:[routine("routine_started","2026-08-25T11:58:00.000Z",{trigger:"schedule"}),
      routine("routine_finished","2026-08-25T11:59:00.000Z",
        {outcome:"ok",duration_s:8,artifacts:[]}),
      routine("routine_started","2026-08-25T11:58:00.000Z",{trigger:"schedule"})],
    expected:["resting","finished heartbeat, ok in 8s"]},
    {records:[routine("routine_finished","2026-08-25T11:57:00.000Z",
      {outcome:"ok",duration_s:8,artifacts:[]}),
      routine("routine_started","2026-08-25T11:58:00.000Z",{trigger:"schedule"})],
    expected:["working","woke for heartbeat"]},
    {records:[routine("routine_started","2026-08-25T11:58:00.000Z",{trigger:"schedule"}),
      routine("routine_failed","2026-08-25T11:59:00.000Z",{error:"boom"}),
      routine("routine_finished","2026-08-25T11:59:00.000Z",
        {outcome:"ok",duration_s:8,artifacts:[]})],
    expected:["failed","heartbeat failed — boom"]},
  ];
  for (const scenario of scenarios) {
    let offset = 0;
    const runtime = createBrowserRuntime({now:()=>NOW,EventSource:null,setTimeout:()=>1,
      clearTimeout(){},warn(){},onProjection(){},fetch:async url => url === "/villagers" ?
        {ok:false} : {ok:true,headers:{get:name => name === "X-Burrow-Cursor" ?
          `v1:${BOOT}:1:2:3:${offset + 1}` : null},text:async()=>scenario.records[offset++]}});
    for (let index = 0; index < scenario.records.length; index++) await runtime.poll();
    const pip = runtime.snapshot().villagers[0];
    assert.deepEqual([pip.state,pip.lastLine],scenario.expected);
    assert.deepEqual(pip.events.map(event => event.type),
      scenario.records.map(record => JSON.parse(record).type));
  }
});

test("grouped/reset keeps canonical routine support inside the 80-event history cap", async () => {
  const start = line({ts:"2026-08-25T11:58:00.000Z",type:"routine_started",
    payload:{routine:"heartbeat",run_id:"bounded",trigger:"schedule"}});
  const finish = line({ts:"2026-08-25T11:59:00.000Z",type:"routine_finished",
    payload:{routine:"heartbeat",run_id:"bounded",outcome:"ok",artifacts:[],duration_s:8}});
  const noise = Array.from({length:4000}, (_, index) => line({
    ts:new Date(NOW - 4000 + index).toISOString(),agent_id:`codex:gone-cap-${index}`,
    type:"session_ended",source:"codex",
  }));
  for (const count of [79, 80, 81]) {
    const runtime = runtimeFor([start, finish, ...Array.from({length:count}, () => start), ...noise], []);
    await runtime.poll();
    const pip = runtime.snapshot().villagers.find(item => item.id === "codex:pip");
    assert.deepEqual([pip.state, pip.lastLine], ["resting", "finished heartbeat, ok in 8s"]);
    assert.equal(pip.events.length, 80);
  }

  const terminalOnly = line({type:"routine_failed",
    payload:{routine:"heartbeat",run_id:"orphan",error:"must stay hidden"}});
  const runtime = runtimeFor([terminalOnly, ...noise], []);
  await runtime.poll();
  assert.equal(runtime.snapshot().villagers.some(item => item.id === "codex:pip"), false);
});

test("grouped projection includes later ordinary superseders of a pre-tail routine", async () => {
  const started = line({ts:"2026-08-25T11:57:00.000Z",type:"routine_started",
    payload:{routine:"heartbeat",run_id:"superseded",trigger:"schedule"}});
  const noise = Array.from({length:4000}, (_, index) => line({
    agent_id:`codex:gone-${index}`,type:"session_ended",source:"codex",
  }));
  for (const [superseder, expected] of [
    [line({ts:"2026-08-25T11:58:00.000Z",type:"idle",source:"codex"}),
      {present:true,state:"resting",history:["routine_started","idle"]}],
    [line({ts:"2026-08-25T11:58:00.000Z",type:"session_ended",source:"codex"}),
      {present:false}],
  ]) {
    const runtime = runtimeFor([started, superseder, ...noise], []);
    await runtime.poll();
    const pip = runtime.snapshot().villagers.find(item => item.id === "codex:pip");
    assert.equal(Boolean(pip), expected.present);
    if (pip) assert.deepEqual({state:pip.state,history:pip.events.map(event => event.type)},
      {state:expected.state,history:expected.history});
  }
});

test("projection witness cap is exact and deterministically keeps newest live agents", () => {
  const events = Array.from({length:12000}, (_, index) => line({
    ts:new Date(NOW - 12000 + index).toISOString(),agent_id:`codex:routine-${index}`,
    type:"routine_started",payload:{routine:"heartbeat",run_id:`run-${index}`,trigger:"schedule"},
  }));
  const parsed = projection.parseEvents(events);
  const exact = projection.projectionWitnesses(parsed, NOW, 4000);
  assert.equal(exact.length, 4000);
  assert.equal(exact[0].agent_id, "codex:routine-8000");
  assert.equal(exact.at(-1).agent_id, "codex:routine-11999");
  assert.equal(projection.projectionWitnesses(parsed, NOW, 3999).length, 3999);
  assert.equal(new Set(exact.map(event => event.agent_id)).size, 4000);
  const boundarySource = projection.validatedSelection(parsed, parsed.slice(-4001));
  const boundary = projection.projectionWitnesses(boundarySource, NOW, 4000);
  assert.equal(boundary.length, 4000);
  assert.equal(boundary[0].agent_id, "codex:routine-8000");
});

test("projection witnesses preserve reconstructible heartbeats at exact limits", () => {
  const prior = type => type === "routine_finished" ? line({
    agent_id:"codex:boundary",type,source:"steward",
    payload:{routine:"heartbeat",run_id:"boundary",outcome:"ok",artifacts:[],duration_s:1},
  }) : line({agent_id:"codex:boundary",type,source:"codex",
    payload:type === "tool_called" ? {tool:"Read"} : {}});
  const heartbeat = line({agent_id:"codex:boundary",type:"heartbeat",source:"codex"});
  const noise = Array.from({length:3999}, (_, index) => line({
    ts:new Date(NOW - 3999 + index).toISOString(),agent_id:`codex:live-${index}`,
    type:"idle",source:"codex",
  }));
  const selectedTypes = (records, limit) => projection.projectionWitnesses(records, NOW, limit)
    .filter(event => event.agent_id === "codex:boundary").map(event => event.type);

  for (const optional of ["idle", "routine_finished"]) {
    assert.deepEqual(selectedTypes([prior(optional), heartbeat], 1), ["heartbeat"],
      `${optional} is optional at limit 1`);
    assert.deepEqual(selectedTypes([prior(optional), heartbeat, ...noise], 4000), ["heartbeat"],
      `${optional} is optional at the 4,000-record boundary`);
  }
  assert.deepEqual(selectedTypes([prior("tool_called"), heartbeat], 1), [],
    "an action-decorating heartbeat remains atomic at limit 1");
  assert.deepEqual(selectedTypes([prior("tool_called"), heartbeat, ...noise], 4000), [],
    "an action-decorating heartbeat remains atomic at the 4,000-record boundary");
  assert.deepEqual(selectedTypes([prior("tool_called"), prior("idle"), heartbeat], 1),
    ["heartbeat"], "an intervening non-action makes the older action optional");
});

test("projection witness defaults agree for raw lines, direct objects and validated batches", () => {
  const records = [line({agent_id:"codex:raw",type:"idle",source:"codex"})];
  const direct = records.map(JSON.parse);
  const validated = projection.parseEvents(records);
  const shape = source => projection.projectionWitnesses(source, NOW, 1)
    .map(event => [event.agent_id, event.type, event.ts]);
  assert.deepEqual(shape(records), shape(direct));
  assert.deepEqual(shape(validated), shape(direct));
  assert.deepEqual(shape(records), [["codex:raw", "idle", "2026-08-25T11:59:00.000Z"]]);
});

test("grouped browser projection remains bounded for 12,000 routine-only agents", async () => {
  const events = Array.from({length:12000}, (_, index) => line({
    ts:new Date(NOW - 12000 + index).toISOString(),agent_id:`codex:wide-${index}`,
    type:"routine_started",payload:{routine:"heartbeat",run_id:`wide-${index}`,trigger:"schedule"},
  }));
  const runtime = runtimeFor(events, []);
  await runtime.poll();
  const villagers = runtime.snapshot().villagers;
  assert.equal(villagers.length, 4000);
  assert.equal(villagers.some(item => item.id === "codex:wide-7999"), false);
  assert.equal(villagers.some(item => item.id === "codex:wide-8000"), true);
  assert.equal(villagers.some(item => item.id === "codex:wide-11999"), true);
});

test("grouped projection charges a pending knock once at the exact 4,000 cap", async () => {
  const routines = Array.from({length:3999}, (_, index) => line({
    ts:new Date(NOW - 3999 + index).toISOString(),agent_id:`codex:r${String(index).padStart(4,"0")}`,
    type:"routine_started",payload:{routine:"heartbeat",run_id:`run-${index}`,trigger:"schedule"},
  }));
  const pending = line({ts:new Date(NOW - 1).toISOString(),source:"codex",
    agent_id:"codex:doorstep",type:"needs_human",payload:{message:"Approve deploy?",
      request_id:"exact-cap",action:"deploy",detail:null,options:["approve","deny"]}});
  const runtime = runtimeFor([...routines, pending], []);
  await runtime.poll();
  const villagers = runtime.snapshot().villagers;
  assert.equal(villagers.length, 4000);
  assert.equal(villagers.some(item => item.id === "codex:r0000"), true);
  assert.equal(villagers.find(item => item.id === "codex:doorstep").state, "knocking");
});
