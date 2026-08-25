"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createBrowserRuntime } = require("../viewer/browser-runtime.js");
const { validateEvent } = require("../viewer/projection.js");
const nursery = require("../viewer/nursery.js");

const BOOT = "0123456789abcdef0123456789abcdef";
const cursor = offset => `v1:${BOOT}:1:2:3:${offset}`;
const timestamp = index => new Date(Date.parse("2026-08-25T10:00:00.000Z") + index).toISOString();
const response = at => ({ ok: true,
  headers: { get: name => name === "X-Burrow-Cursor" ? at : null }, text: async () => "" });

function specializedEvent(index) {
  const common = { v:0, ts:timestamp(index), source:"steward",
    agent_id:`agent-${index}`, project:"burrow" };
  if (index % 3 === 0) return { ...common, type:"routine_started",
    payload:{routine:"summary",run_id:`run-${index}`,trigger:"manual"} };
  if (index % 3 === 1) return { ...common, type:"task_posted",
    payload:{task_id:`task-${index}`,title:`Task ${index}`,required_skills:[],posted_by:"api"} };
  return { ...common, type:"needs_human_resolved",
    payload:{request_id:`approval-${index}`,decision:"approve",decided_by:"api",action:"publish"} };
}

async function stage(count) {
  const streams = [], publications = [];
  class EventSource {
    constructor() { this.listeners = {}; streams.push(this); }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    close() { this.closed = true; }
  }
  const runtime = createBrowserRuntime({ now:()=>Date.parse("2026-08-25T11:30:00.000Z"),
    EventSource, setTimeout:()=>1, clearTimeout(){},
    fetch:async url => url === "/villagers" ? {ok:false} : response(cursor(10)),
    onFleet:view => {
      if (view.reset || view.eventEvidence.length) publications.push(view);
    },
  });
  await runtime.poll();
  runtime.connectStream();
  const stream = streams[0];
  for (let index = 0; index < count; index++) {
    stream.onmessage({ lastEventId:cursor(11 + index),
      data:JSON.stringify(specializedEvent(index)) });
  }
  const end = cursor(10 + count);
  await stream.listeners.ready({ lastEventId:end, data:JSON.stringify({cursor:end}) });
  return publications;
}

test("exactly 4,000 mixed specialized records cross the pre-ready boundary", async () => {
  const publications = await stage(4000);
  assert.equal(publications.some(view => view.reset), false,
    "the documented capacity is inclusive rather than an early conservative reset");
  assert.equal(publications.reduce((total, view) => total + view.eventEvidence.length, 0), 4000);
  assert.equal(publications.reduce((total, view) => total + view.routineBatch.length, 0), 1334);
  assert.equal(publications.reduce((total, view) => total + view.taskEvidence.length, 0), 1333);
  assert.equal(publications.reduce((total, view) => total + view.approvalEvidence.length, 0), 1333);
  assert.equal(publications.length, 4000,
    "every retained record is published once with its exact cursor boundary");
});

test("record 4,001 overflows one mixed staged batch conservatively", async () => {
  const publications = await stage(4001);
  assert.equal(publications.filter(view => view.reset).length, 1);
  assert.equal(publications.reduce((total, view) => total + view.eventEvidence.length, 0), 0);
  assert.equal(publications.reduce((total, view) => total + view.routineBatch.length, 0), 0);
  assert.equal(publications.reduce((total, view) => total + view.taskEvidence.length, 0), 0);
  assert.equal(publications.reduce((total, view) => total + view.approvalEvidence.length, 0), 0,
    "overflow publishes no partial consumer slice from the discarded batch");
});

test("pre-ready matching Steward evidence cannot wake a nursery request", async () => {
  const streams = [];
  class EventSource {
    constructor() { this.listeners = {}; streams.push(this); }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    close() { this.closed = true; }
  }
  const tracker=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
  const runtime=createBrowserRuntime({now:()=>Date.parse("2026-08-25T11:30:00.000Z"),
    EventSource,setTimeout:()=>1,clearTimeout(){},
    fetch:async url=>url==="/villagers"?{ok:false}:response(cursor(10)),
    onFleet:view=>tracker.observe({events:view.eventEvidence,cursor:view.cursor,reset:view.reset},
      validateEvent),
  });
  await runtime.poll();
  const item=tracker.begin(cursor(10),{name:"Staged Keeper",char:"Monk",accent:"#4f7ea6",
    role:"keeper",mission:"Keep staging truthful.",duties:"Observe",rules:"Never guess",
    escalation:"Ask",skills:"research",runner:"codex"}).item;
  tracker.accepted(item.key,{request_id:"staged-request",changed:true,declaration_written:true,
    register_ok:true,register_problems:[],message:"accepted",truncated:false});
  runtime.connectStream();
  const stream=streams[0];
  const matching={v:0,ts:timestamp(1),source:"steward",agent_id:"codex:staged-keeper",
    project:"burrow",type:"routine_started",
    payload:{routine:"summary",run_id:"staged-wrong-source",trigger:"manual"}};
  stream.onmessage({lastEventId:cursor(11),data:JSON.stringify(matching)});
  await stream.listeners.ready({lastEventId:cursor(11),data:JSON.stringify({cursor:cursor(11)})});
  assert.equal(item.state,"pending",
    "the exact staged cursor and resident identity do not make Steward a runner");
  assert.equal(item.candidates.size,0);

  stream.onmessage({lastEventId:cursor(12),data:JSON.stringify({...matching,source:"codex",
    type:"task_started",payload:{prompt:"Wake after readiness"}})});
  assert.equal(item.state,"alive","the subsequent exact runner-authored event wakes the resident");
  assert.equal(item.event.source,"codex");
});
