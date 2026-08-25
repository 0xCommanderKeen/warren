"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createBrowserRuntime } = require("../viewer/browser-runtime.js");
const jobs = require("../viewer/job-board.js");
const routines = require("../viewer/routine-ledger.js");
const { validateEvent } = require("../viewer/projection.js");

const BOOT = "0123456789abcdef0123456789abcdef";
const RESET = "fedcba9876543210fedcba9876543210";
const cursor = (offset, boot = BOOT) => `v1:${boot}:1:2:3:${offset}`;
const postObject = (id, ts = "2026-08-25T10:00:00.000Z", overrides = {}) => ({
  v:0,ts,source:"steward",agent_id:"steward:api",project:"steward",type:"task_posted",
  payload:{task_id:id,title:`Task ${id}`,required_skills:[],posted_by:"api",...overrides},
});
const post = (id, ts, overrides) => JSON.stringify(postObject(id, ts, overrides));
const response = (at, body = "", reset = false) => ({ok:true,
  headers:{get:name=>name === "X-Burrow-Cursor" ? at : name === "X-Burrow-Reset" && reset ? "1" : null},
  text:async()=>body});

test("production polling/SSE projection updates incrementally and reset removes ghosts", async () => {
  const streams = [];
  class EventSource {
    constructor(url) { this.url=url; this.listeners={}; streams.push(this); }
    addEventListener(name, fn) { this.listeners[name]=fn; }
    close() { this.closed=true; }
  }
  let request = 0, retry;
  const views = [];
  const acks = jobs.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
  const runtime = createBrowserRuntime({
    now:()=>Date.parse("2026-08-25T10:01:00.000Z"),EventSource,
    setTimeout:fn=>{retry=fn;return 1;},clearTimeout(){},
    fetch:async url=>{
      if (url === "/villagers") return {ok:false};
      request += 1;
      if (request === 1) return response(cursor(10), post("baseline") + "\n");
      return response(cursor(40, RESET), post("fresh", "2026-08-25T10:02:00.000Z") + "\n", true);
    },
    onFleet(view) {
      views.push(view);
      acks.observe({events:view.taskEvidence,cursor:view.cursor,reset:view.reset},validateEvent);
    },
  });
  await runtime.poll();
  assert.deepEqual(jobs.rows(runtime.snapshot().jobState).map(row=>row.id), ["baseline"]);
  const pending = acks.request(cursor(10)); acks.accepted(pending.id,"live","request-live");
  runtime.connectStream();
  const stream = streams[0];
  stream.onmessage({lastEventId:cursor(20),data:post("live","2026-08-25T10:01:01.000Z")});
  assert.equal(acks.get(pending.id).state,"pending","pre-ready backlog is not write evidence");
  stream.listeners.ready({lastEventId:cursor(20),data:JSON.stringify({cursor:cursor(20)})});
  assert.equal(acks.get(pending.id).state,"acknowledged");
  assert.deepEqual(jobs.rows(runtime.snapshot().jobState).map(row=>row.id),["live","baseline"]);

  stream.onmessage({lastEventId:cursor(21),data:post("bad","2026-08-25T10:01:02.000Z",{title:""})});
  assert.equal(runtime.snapshot().jobState.malformed,1);
  stream.listeners.reset();
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(jobs.rows(runtime.snapshot().jobState).map(row=>row.id),["fresh"],
    "grouped X-Burrow-Reset baseline replaces rather than appends board state");
  assert.equal(runtime.snapshot().jobState.malformed,0);
  assert.equal(views.findLast(view=>view.reset).taskEvidence[0].payload.task_id,"fresh");
  assert.equal(typeof retry,"function");
});

test("production one-event SSE and grouped bootstrap agree after capacity eviction", async () => {
  const start = Date.parse("2026-08-25T10:00:00.000Z");
  const records = Array.from({length:jobs.MAX_TASKS + 1}, (_, index) =>
    postObject(`task-${index}`, new Date(start + index).toISOString(),
      {required_skills:[`skill-${index}`]}));
  const claim = {v:0,ts:new Date(start + 1000).toISOString(),source:"steward",
    agent_id:"codex:claimer",project:"life",type:"task_claimed",
    payload:{task_id:"task-0",title:"Claimed after eviction",claimant:"codex:claimer"}};
  records.push(claim);

  const grouped = createBrowserRuntime({now:()=>start + 2000,EventSource:null,
    setTimeout:()=>1,clearTimeout(){},fetch:async url=>url === "/villagers" ? {ok:false} :
      response(cursor(100), records.map(JSON.stringify).join("\n") + "\n")});
  await grouped.poll();

  const streams = [];
  class EventSource {
    constructor() { this.listeners={}; streams.push(this); }
    addEventListener(name, fn) { this.listeners[name]=fn; }
    close() {}
  }
  const live = createBrowserRuntime({now:()=>start + 2000,EventSource,
    setTimeout:()=>1,clearTimeout(){},fetch:async url=>url === "/villagers" ? {ok:false} :
      response(cursor(10), "")});
  await live.poll();
  live.connectStream();
  const stream = streams[0];
  stream.listeners.ready({lastEventId:cursor(10),data:JSON.stringify({cursor:cursor(10)})});
  records.forEach((record, index) => stream.onmessage({
    lastEventId:cursor(11 + index),data:JSON.stringify(record)}));

  const canonical = runtime => jobs.rows(runtime.snapshot().jobState, start + 2000)
    .map(row => ({id:row.id,title:row.title,required_skills:row.required_skills,
      posted_at:row.posted_at,updated_at:row.updated_at,state:row.state,claimant:row.claimant}));
  assert.deepEqual(canonical(live), canonical(grouped));
  assert.equal(canonical(live).find(row=>row.id === "task-0").required_skills, null);
  assert.equal(live.snapshot().jobState.tasks.has("task-1"), false);
});

test("production browser rejects globally invalid task-shaped records in its single strict pass", async () => {
  const valid = postObject("valid");
  const variants = [
    {...valid,v:1,payload:{...valid.payload,task_id:"bad-version"}},
    Object.fromEntries(Object.entries({...valid,payload:{...valid.payload,task_id:"missing-version"}})
      .filter(([key])=>key!=="v")),
    {...valid,agent_id:" ",payload:{...valid.payload,task_id:"blank-agent"}},
    Object.fromEntries(Object.entries({...valid,payload:{...valid.payload,task_id:"missing-agent"}})
      .filter(([key])=>key!=="agent_id")),
    {...valid,project:"",payload:{...valid.payload,task_id:"blank-project"}},
    Object.fromEntries(Object.entries({...valid,payload:{...valid.payload,task_id:"missing-project"}})
      .filter(([key])=>key!=="project")),
    {...valid,ts:"2026-08-25 10:00:00",payload:{...valid.payload,task_id:"bad-time"}},
    {...valid,payload:{...valid.payload,task_id:"malformed-task",title:""}},
  ];
  const runtime = createBrowserRuntime({
    now:()=>Date.parse("2026-08-25T10:01:00.000Z"),EventSource:null,
    setTimeout:()=>1,clearTimeout(){},
    fetch:async url=>url === "/villagers" ? {ok:false} :
      response(cursor(20), [valid,...variants].map(JSON.stringify).join("\n") + "\n"),
  });
  await runtime.poll();
  const state = runtime.snapshot().jobState;
  assert.deepEqual(jobs.rows(state).map(row=>row.id),["valid"]);
  assert.equal(state.malformed,variants.length,
    "task diagnostics survive shared rejection while invalid records never project");
});

test("exact pre-ready post evidence survives stream error and converges without timeout", async () => {
  const streams = [];
  class EventSource {
    constructor(url) { this.url=url; this.listeners={}; streams.push(this); }
    addEventListener(name, fn) { this.listeners[name]=fn; }
    close() { this.closed=true; }
  }
  let eventReads = 0;
  const acks = jobs.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
  const evidence = [];
  const runtime = createBrowserRuntime({
    now:()=>Date.parse("2026-08-25T10:01:00.000Z"),EventSource,
    setTimeout:()=>1,clearTimeout(){},
    fetch:async url=>{
      if (url === "/villagers") return {ok:false};
      eventReads += 1;
      return eventReads === 1 ? response(cursor(10),post("baseline")+"\n") :
        response(cursor(20),"");
    },
    onFleet(view) {
      evidence.push(...view.taskEvidence.map(event=>event.payload.task_id));
      acks.observe({events:view.taskEvidence,cursor:view.cursor,reset:view.reset},validateEvent);
    },
  });
  await runtime.poll();
  const tracking = acks.request(cursor(10));
  acks.accepted(tracking.id,"accepted","request-accepted");
  runtime.connectStream();
  const stream = streams[0];
  stream.onmessage({lastEventId:cursor(20),data:post("accepted","2026-08-25T10:01:01.000Z")});
  assert.equal(acks.get(tracking.id).state,"pending");
  await stream.onerror();
  assert.equal(acks.get(tracking.id).state,"acknowledged");
  assert.equal(evidence.filter(id=>id==="accepted").length,1,
    "recovery publishes staged exact evidence once and polls after its cursor");
  assert.deepEqual(jobs.rows(runtime.snapshot().jobState).map(row=>row.id),["accepted","baseline"]);
  assert.equal(runtime.snapshot().cursor,cursor(20));
});

test("runtime-staged post evidence reconciles a valid response received after its deadline", async () => {
  let now = 1_000, eventReads = 0;
  const acks = jobs.createAcknowledgements({timeoutMs:10,now:()=>now,
    setTimeout:()=>1,clearTimeout(){}});
  const runtime = createBrowserRuntime({now:()=>now,EventSource:null,
    setTimeout:()=>1,clearTimeout(){},
    fetch:async url=>{
      if (url === "/villagers") return {ok:false};
      eventReads += 1;
      return eventReads === 1 ? response(cursor(10),"") :
        response(cursor(11),post("late-runtime","2026-08-25T10:01:01.000Z")+"\n");
    },
    onFleet(view) {
      acks.observe({events:view.taskEvidence,cursor:view.cursor,reset:view.reset},validateEvent);
    },
  });
  await runtime.poll();
  const tracking = acks.request(cursor(10));
  await runtime.poll();
  assert.equal(acks.get(tracking.id).state,"requesting");
  assert.equal(acks.get(tracking.id).candidates.has("late-runtime"),true);
  now = 1_011;
  acks.accepted(tracking.id,"late-runtime","late-runtime-request");
  const resolved = acks.get(tracking.id);
  assert.equal(resolved.state,"acknowledged");
  assert.equal(resolved.request_id,"late-runtime-request");
  assert.equal(resolved.deadlineElapsedAt,1_011);
  assert.match(resolved.message,/after the acknowledgement deadline had elapsed/);
});

test("malformed and mismatched ready markers publish staged task evidence before recovery", async t => {
  for (const scenario of [
    {name:"malformed ready",ready:{lastEventId:cursor(20),data:"{"}},
    {name:"mismatched ready",ready:{lastEventId:cursor(20),
      data:JSON.stringify({cursor:cursor(19)})}},
  ]) await t.test(scenario.name, async () => {
    const streams = [];
    class EventSource {
      constructor(url) { this.url=url; this.listeners={}; streams.push(this); }
      addEventListener(name, fn) { this.listeners[name]=fn; }
      close() { this.closed=true; }
    }
    let eventReads = 0;
    const acks = jobs.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
    const runtime = createBrowserRuntime({
      now:()=>Date.parse("2026-08-25T10:01:00.000Z"),EventSource,
      setTimeout:()=>1,clearTimeout(){},
      fetch:async url=>{
        if (url === "/villagers") return {ok:false};
        eventReads += 1;
        return eventReads === 1 ? response(cursor(10),post("baseline")+"\n") :
          response(cursor(20),"");
      },
      onFleet(view) {
        acks.observe({events:view.taskEvidence,cursor:view.cursor,reset:view.reset},validateEvent);
      },
    });
    await runtime.poll();
    const tracking = acks.request(cursor(10));
    acks.accepted(tracking.id,"accepted","request-accepted");
    runtime.connectStream();
    const stream = streams[0];
    stream.onmessage({lastEventId:cursor(20),
      data:post("accepted","2026-08-25T10:01:01.000Z")});
    assert.equal(acks.get(tracking.id).state,"pending");
    await stream.listeners.ready(scenario.ready);
    assert.equal(acks.get(tracking.id).state,"acknowledged",
      "invalid readiness cannot erase exact post evidence");
    assert.deepEqual(jobs.rows(runtime.snapshot().jobState).map(row=>row.id),
      ["accepted","baseline"],"recovery preserves the already-projected visible task");
    assert.equal(runtime.snapshot().cursor,cursor(20));
  });
});

test("readiness uses the canonical v1 namespace and uint64 cursor contract", async t => {
  const MAX = "18446744073709551615";
  const otherBoot = "fedcba9876543210fedcba9876543210";
  const samples = [
    {name:"ordinary canonical cursor",baseline:cursor(10),ready:cursor(10),live:true},
    {name:"maximum uint64 fields",
      baseline:`v1:${BOOT}:${MAX}:${MAX}:${MAX}:${MAX}`,
      ready:`v1:${BOOT}:${MAX}:${MAX}:${MAX}:${MAX}`,live:true},
    {name:"changed valid namespace",baseline:cursor(10),
      ready:`v1:${otherBoot}:1:2:3:10`,live:false},
    {name:"wrong version namespace",baseline:"v2:0123456789abcdef0123456789abcdef:1:2:3:10",
      ready:"v2:0123456789abcdef0123456789abcdef:1:2:3:10",live:false},
    {name:"uppercase boot namespace",baseline:"v1:0123456789ABCDEF0123456789ABCDEF:1:2:3:10",
      ready:"v1:0123456789ABCDEF0123456789ABCDEF:1:2:3:10",live:false},
    {name:"non-canonical leading zero",baseline:`v1:${BOOT}:01:2:3:10`,
      ready:`v1:${BOOT}:01:2:3:10`,live:false},
    {name:"uint64 overflow",baseline:`v1:${BOOT}:1:2:3:18446744073709551616`,
      ready:`v1:${BOOT}:1:2:3:18446744073709551616`,live:false},
  ];
  for (const sample of samples) await t.test(sample.name, async () => {
    const streams = [];
    class EventSource {
      constructor(url) { this.url=url;this.listeners={};streams.push(this); }
      addEventListener(name,fn) { this.listeners[name]=fn; }
      close() { this.closed=true; }
    }
    const states = [];
    const runtime = createBrowserRuntime({now:()=>0,EventSource,
      setTimeout:()=>1,clearTimeout(){},
      fetch:async url=>url === "/villagers" ? {ok:false} : response(sample.baseline),
      onTransport:state=>states.push(state),
    });
    await runtime.poll();
    runtime.connectStream();
    const stream = streams[0];
    await stream.listeners.ready({lastEventId:sample.ready,
      data:JSON.stringify({cursor:sample.ready})});
    assert.equal(runtime.snapshot().transport === "live",sample.live);
    assert.equal(Boolean(routines.parseCursor(sample.ready)),
      sample.live || sample.name === "changed valid namespace",
      "readiness parser acceptance stays in parity with the exported canonical parser");
    if (!sample.live) assert.equal(stream.closed,true);
    else assert.equal(states.at(-1),"live");
  });
});

test("pre-ready overflow plus stream error makes pending post explicitly ambiguous", async () => {
  const streams = [], reads = [];
  class EventSource {
    constructor(url) { this.url=url; this.listeners={}; streams.push(this); }
    addEventListener(name, fn) { this.listeners[name]=fn; }
    close() { this.closed=true; }
  }
  const acks = jobs.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
  const runtime = createBrowserRuntime({
    now:()=>Date.parse("2026-08-25T10:01:00.000Z"),EventSource,
    setTimeout:()=>1,clearTimeout(){},
    fetch:async url=>{
      if (url === "/villagers") return {ok:false};
      reads.push(url);
      return reads.length === 1 ? response(cursor(10), "") : response(cursor(4011), "");
    },
    onFleet(view) {
      acks.observe({events:view.taskEvidence,cursor:view.cursor,reset:view.reset},validateEvent);
    },
  });
  await runtime.poll();
  const tracking = acks.request(cursor(10));
  acks.accepted(tracking.id,"accepted-before-overflow","request-overflow");
  runtime.connectStream();
  const stream = streams[0];
  stream.onmessage({lastEventId:cursor(11),
    data:post("accepted-before-overflow","2026-08-25T10:01:01.000Z")});
  for (let index=0; index<4000; index++) {
    stream.onmessage({lastEventId:cursor(12+index),
      data:post(`overflow-${index}`,new Date(Date.parse("2026-08-25T10:02:00.000Z")+index).toISOString())});
  }
  assert.equal(acks.get(tracking.id).state,"pending");
  await stream.onerror();
  assert.equal(acks.get(tracking.id).state,"ambiguous",
    "overflow cannot silently lose exact post evidence while advancing the poll cursor");
  assert.equal(reads.at(-1),`/events?since=${cursor(4011)}`);
  assert.equal(runtime.snapshot().cursor,cursor(4011));
});
