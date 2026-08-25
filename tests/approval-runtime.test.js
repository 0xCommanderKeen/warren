"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {createBrowserRuntime} = require("../viewer/browser-runtime.js");
const approvals = require("../viewer/approval-knocks.js");

const BOOT="0123456789abcdef0123456789abcdef", RESET="fedcba9876543210fedcba9876543210";
const cursor=(offset,boot=BOOT)=>`v1:${boot}:1:2:3:${offset}`;
const knock=(id,ts="2026-08-25T10:00:00.000Z")=>({v:0,ts,source:"codex",
  agent_id:"codex:keeper",project:"life",type:"needs_human",payload:{message:"May I?",
    request_id:id,action:"send_email",detail:{to:"a@example.com"},options:["approve","deny"]}});
const close=(id,ts="2026-08-25T10:01:00.000Z")=>({v:0,ts,source:"steward",
  agent_id:"codex:keeper",project:"life",type:"needs_human_resolved",payload:{request_id:id,
    decision:"approve",decided_by:"api",action:"send_email"}});
const expected=id=>{const event=knock(id);return {agent_id:event.agent_id,project:event.project,
  action:event.payload.action,lifecycle:approvals.lifecycleIdentity(event,approvals.classify(event))};};
const response=(at,events=[],reset=false)=>({ok:true,headers:{get:name=>
  name==="X-Burrow-Cursor"?at:name==="X-Burrow-Reset"&&reset?"1":null},
  text:async()=>events.map(JSON.stringify).join("\n")});

test("production SSE stages closing evidence until ready, then reset removes approval ghosts", async()=>{
  const streams=[];
  class EventSource { constructor(){this.listeners={};streams.push(this);}
    addEventListener(name,fn){this.listeners[name]=fn;} close(){this.closed=true;} }
  let reads=0,retry;
  const acks=approvals.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
  const runtime=createBrowserRuntime({now:()=>Date.parse("2026-08-25T10:02:00.000Z"),EventSource,
    setTimeout:fn=>{retry=fn;return 1;},clearTimeout(){},fetch:async url=>{
      if(url==="/villagers")return {ok:false}; reads++;
      return reads===1?response(cursor(10),[knock("r1")]):response(cursor(30,RESET),[],true);
    },onFleet:view=>acks.observe({events:view.approvalEvidence,cursor:view.cursor,reset:view.reset,
      approvalState:view.approvalState})});
  await runtime.poll();
  assert.equal(runtime.snapshot().villagers[0].state,"knocking");
  acks.request("r1","approve",null,cursor(10),expected("r1"));acks.accepted("r1",{replay:false});
  runtime.connectStream();const stream=streams[0];
  stream.onmessage({lastEventId:cursor(20),data:JSON.stringify(close("r1"))});
  assert.equal(acks.get("r1").state,"pending","pre-ready backlog is not write acknowledgement");
  assert.equal(runtime.snapshot().villagers[0].state,"resting","real log closure updates projection");
  stream.listeners.ready({lastEventId:cursor(20),data:JSON.stringify({cursor:cursor(20)})});
  assert.equal(acks.get("r1").state,"acknowledged");
  stream.listeners.reset();await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(runtime.snapshot().villagers,[]);
  assert.equal(runtime.snapshot().approvalState.requests.size,0);
  assert.equal(typeof retry,"function");
});

test("pre-ready overflow conservatively invalidates pending approval correlation", async()=>{
  const streams=[];
  class EventSource {constructor(){this.listeners={};streams.push(this);}
    addEventListener(name,fn){this.listeners[name]=fn;}close(){this.closed=true;}}
  const acks=approvals.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
  const runtime=createBrowserRuntime({now:()=>0,EventSource,setTimeout:()=>1,clearTimeout(){},
    fetch:async url=>url==="/villagers"?{ok:false}:response(cursor(10),[]),
    onFleet:view=>acks.observe({events:view.approvalEvidence,cursor:view.cursor,reset:view.reset,
      approvalState:view.approvalState})});
  await runtime.poll();acks.request("target","approve",null,cursor(10),expected("target"));
  acks.accepted("target",{replay:false});runtime.connectStream();const stream=streams[0];
  for(let i=0;i<4001;i++)stream.onmessage({lastEventId:cursor(11+i),
    data:JSON.stringify(close(`other-${i}`))});
  stream.listeners.ready({lastEventId:cursor(4011),data:JSON.stringify({cursor:cursor(4011)})});
  assert.equal(acks.get("target").state,"ambiguous");
  assert.match(acks.get("target").message,/reset/);
});

test("orphan close never binds when its knock arrives live or in reset replay", async()=>{
  let reads=0;
  const orphan=close("orphan-first","2026-08-25T09:59:00.000Z");
  const request=knock("orphan-first","2026-08-25T10:00:00.000Z");
  const runtime=createBrowserRuntime({now:()=>Date.parse("2026-08-25T10:02:00.000Z"),
    EventSource:null,setTimeout:()=>1,clearTimeout(){},fetch:async url=>{
      if(url==="/villagers")return {ok:false};reads++;
      if(reads===1)return response(cursor(10),[orphan]);
      if(reads===2)return response(cursor(20),[request]);
      return response(cursor(30,RESET),[orphan,request],true);
    }});
  await runtime.poll();
  assert.equal(runtime.snapshot().approvalState.requests.size,0);
  assert.match(runtime.snapshot().approvalState.diagnostics[0].reason,/unknown request_id/);
  await runtime.poll();
  assert.equal(runtime.snapshot().approvalState.requests.get("orphan-first").resolution,null);
  assert.equal(runtime.snapshot().villagers[0].state,"knocking");
  await runtime.poll();
  assert.equal(runtime.snapshot().approvalState.requests.get("orphan-first").resolution,null);
  assert.equal(runtime.snapshot().villagers[0].state,"knocking",
    "grouped reset replay cannot turn earlier orphan evidence into authority");
});

test("bootstrap incremental batches and reset replay preserve append-authoritative approval state",async()=>{
  const futureActivity={v:0,ts:"2026-08-25T23:00:00.000Z",source:"codex",
    agent_id:"codex:keeper",project:"life",type:"tool_called",payload:{tool:"Read"}};
  const laterActivity={...futureActivity,ts:"2026-08-25T10:01:30.000Z",
    payload:{tool:"Write"}};
  const olderClose=close("append-runtime","2026-08-25T09:00:00.000Z");
  const request=knock("append-runtime","2026-08-25T10:00:00.000Z");
  let reads=0;
  const runtime=createBrowserRuntime({now:()=>Date.parse("2026-08-25T10:02:00.000Z"),
    EventSource:null,setTimeout:()=>1,clearTimeout(){},fetch:async url=>{
      if(url==="/villagers")return {ok:false};reads++;
      if(reads===1)return response(cursor(10),[request,futureActivity,olderClose]);
      if(reads===2)return response(cursor(20),[laterActivity]);
      if(reads===3)return response(cursor(30,RESET),[request,futureActivity,olderClose,laterActivity],true);
      return response(cursor(40,RESET),[request,futureActivity,laterActivity,olderClose],true);
    }});
  await runtime.poll();
  assert.equal(runtime.snapshot().villagers[0].state,"resting",
    "bootstrap uses append order despite producer timestamps");
  await runtime.poll();
  assert.equal(runtime.snapshot().villagers[0].state,"working",
    "a later incremental append becomes authoritative");
  await runtime.poll();
  assert.equal(runtime.snapshot().villagers[0].state,"working",
    "reset replay reconstructs the identical order");
  await runtime.poll();
  assert.equal(runtime.snapshot().villagers[0].state,"resting",
    "changing only replay append order changes authority");
});

test("session terminal survives close, incremental delivery, and grouped reset replay", async()=>{
  const streams=[];
  class EventSource {constructor(){this.listeners={};streams.push(this);}
    addEventListener(name,fn){this.listeners[name]=fn;}close(){this.closed=true;}}
  const ended={v:0,ts:"2026-08-25T10:00:30.000Z",source:"codex",agent_id:"codex:keeper",
    project:"life",type:"session_ended",payload:{}};
  let reads=0;
  const runtime=createBrowserRuntime({now:()=>Date.parse("2026-08-25T10:02:00.000Z"),EventSource,
    setTimeout:()=>1,clearTimeout(){},fetch:async url=>{
      if(url==="/villagers")return {ok:false};reads++;
      return reads===1?response(cursor(10),[knock("parked"),ended]):
        response(cursor(30,RESET),[knock("parked"),ended,close("parked")],true);
    }});
  await runtime.poll();
  assert.equal(runtime.snapshot().villagers[0].state,"knocking");
  runtime.connectStream();
  const stream=streams[0];
  stream.onmessage({lastEventId:cursor(20),data:JSON.stringify(close("parked"))});
  assert.deepEqual(runtime.snapshot().villagers,[],"incremental close cannot resurrect terminal session");
  assert.deepEqual(approvals.recentConfirmations(runtime.snapshot().approvalState)
    .map(item=>[item.request_id,item.resolution.payload.decision]),[["parked","approve"]],
  "incremental projection retains a bounded global decision confirmation");
  stream.listeners.reset();await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(runtime.snapshot().villagers,[],"grouped reset replay keeps terminal authority");
  assert.deepEqual(approvals.recentConfirmations(runtime.snapshot().approvalState)
    .map(item=>[item.request_id,item.resolution.payload.decision]),[["parked","approve"]],
  "grouped reset reconstructs the same global decision confirmation");
});
