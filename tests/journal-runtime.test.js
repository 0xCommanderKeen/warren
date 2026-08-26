"use strict";
const test=require("node:test");
const assert=require("node:assert/strict");
const {createBrowserRuntime}=require("../viewer/browser-runtime.js");

const BASE=Date.parse("2026-08-25T20:31:00.000Z");
const BOOT="0123456789abcdef0123456789abcdef";
const cursor=n=>`v1:${BOOT}:1:2:3:${n}`;
const written=(changes={})=>({v:0,ts:new Date(BASE).toISOString(),source:"steward",
  agent_id:"codex:life",project:"life",type:"journal_written",
  payload:{routine:"close-of-day",day:"2026-08-25",path:"/journal/2026-08-25.md"},...changes});
const resident={file:"life.resident.json",valid:true,manifest_version:1,home:2,
  match:{agent_id:"codex:life"},meta:{name:"Hob",char:"Monk",accent:"#a68a4f"}};
const response=(body,at,reset=false)=>({ok:true,headers:{get:name=>name==="X-Burrow-Cursor"?at:
  name==="X-Burrow-Reset"&&reset?"1":null},text:async()=>body});

test("grouped bootstrap, SSE, diagnostics and reset use one journal authority",async()=>{
  let now=BASE+1000, polls=0, streams=[];
  const invalid=written({source:"codex"});
  const canonical=written(), replay=written({ts:new Date(BASE+500).toISOString()});
  class EventSource{constructor(){this.listeners={};streams.push(this)} addEventListener(n,f){this.listeners[n]=f} close(){this.closed=true}}
  const runtime=createBrowserRuntime({now:()=>now,EventSource,setTimeout:()=>1,clearTimeout(){},
    fetch:async url=>{
      if(url==="/villagers")return{ok:true,json:async()=>[resident]};
      if(url.startsWith("/events")){
        polls++;
        const resetJournal=written({payload:{...canonical.payload,day:"2026-08-26",path:"/journal/2026-08-26.md"}});
        const ended={v:0,ts:new Date(BASE+1).toISOString(),source:"codex",agent_id:"codex:life",
          project:"life",type:"session_ended",payload:{}};
        const body=polls===1?[canonical,invalid].map(JSON.stringify).join("\n"):
          [resetJournal,ended].map(JSON.stringify).join("\n");
        return response(body,cursor(polls*10),polls>1);
      }
      throw new Error(url);
    }});
  await runtime.poll();
  assert.equal(runtime.snapshot().villagers[0].doing,"writing the journal");
  assert.equal(runtime.snapshot().journalState.records.size,1);
  assert.equal(runtime.snapshot().journalState.malformed,1);
  runtime.connectStream(); const stream=streams[0];
  stream.onmessage({lastEventId:cursor(11),data:JSON.stringify(replay)});
  const conflict=written({payload:{...canonical.payload,routine:"nightly"}});
  stream.onmessage({lastEventId:cursor(12),data:JSON.stringify(conflict)});
  await stream.listeners.ready({lastEventId:cursor(12),data:JSON.stringify({cursor:cursor(12)})});
  assert.deepEqual(runtime.snapshot().journalState.records.get(`codex:life\0${"2026-08-25"}`).event,canonical);
  assert.equal(runtime.snapshot().journalState.diagnostics.length,2,
    "malformed source and immutable collision remain separate diagnostics");
  await stream.listeners.reset();
  assert.equal(runtime.snapshot().journalState.records.size,1,"grouped reset discards old authority");
  assert.equal(runtime.snapshot().journalState.records.has(`codex:life\0${"2026-08-26"}`),true);
  assert.equal(runtime.snapshot().journalState.malformed,0);
  assert.equal(runtime.snapshot().villagers.length,0,
    "reset keeps observed recency but a later session terminal prevents resurrection");
});

test("bootstrap and reset reduce full journal authority before the raw 4,000-record tail",async()=>{
  let polls=0;
  const publications=[];
  const chatter=day=>Array.from({length:4001},(_,index)=>({v:0,
    ts:new Date(BASE+index).toISOString(),source:"codex",agent_id:`codex:other-${index}`,
    project:"other",type:"tool_called",payload:{tool:"Read",day}}));
  const grouped=day=>[written({payload:{routine:"close-of-day",day,
    path:`/journal/${day}.md`}}),...chatter(day)].map(JSON.stringify).join("\n");
  const runtime=createBrowserRuntime({now:()=>BASE+5000,EventSource:null,
    setTimeout:()=>1,clearTimeout(){},onFleet:view=>publications.push(view),
    fetch:async url=>{
      if(url==="/villagers")return{ok:true,json:async()=>[resident]};
      if(url.startsWith("/events")){
        polls++;
        return response(grouped(polls===1?"2026-08-25":"2026-08-26"),
          cursor(polls*10),polls===2);
      }
      throw new Error(url);
    }});
  await runtime.poll();
  assert.equal(runtime.snapshot().journalState.records.has(`codex:life\0${"2026-08-25"}`),true);
  assert.equal(runtime.snapshot().fleetState.malformed,0,
    "valid full-window records omitted from the ordinary tail are not malformed");
  assert.equal(runtime.snapshot().villagers.find(item=>item.id==="codex:life").doing,
    "writing the journal");
  assert.equal(publications.findLast(view=>view.eventEvidence.length).eventEvidence.length,4000,
    "ordinary consumers receive only the shared raw transport tail");
  assert.ok(runtime.snapshot().villagers.length<=4001,
    "the early journal adds only bounded retained authority to the consumer window");

  await runtime.poll();
  assert.equal(runtime.snapshot().journalState.records.has(`codex:life\0${"2026-08-25"}`),false);
  assert.equal(runtime.snapshot().journalState.records.has(`codex:life\0${"2026-08-26"}`),true);
  assert.equal(runtime.snapshot().fleetState.malformed,0,
    "reset rebuild preserves the same zero-rejection semantics");
  assert.equal(runtime.snapshot().villagers.find(item=>item.id==="codex:life").journal.event.payload.day,
    "2026-08-26");
  assert.equal(publications.filter(view=>view.reset&&view.eventEvidence.length===4000).length,1,
    "reset publishes exactly one clipped consumer view while rebuilding full journal authority");
});

test("grouped malformed counts cover the full response exactly once",async()=>{
  let polls=0;
  const ordinary=Array.from({length:4000},(_,index)=>({v:0,
    ts:new Date(BASE+index).toISOString(),source:"codex",agent_id:`codex:other-${index}`,
    project:"other",type:"tool_called",payload:{tool:"Read"}}));
  const invalidJournal=JSON.stringify(written({source:"codex"}));
  const body=()=>[invalidJournal,JSON.stringify(written()),...ordinary.map(JSON.stringify),"{"].join("\n");
  const runtime=createBrowserRuntime({now:()=>BASE+5000,EventSource:null,
    setTimeout:()=>1,clearTimeout(){},fetch:async url=>{
      if(url==="/villagers")return{ok:true,json:async()=>[resident]};
      if(url.startsWith("/events")){polls++;return response(body(),cursor(polls*10),polls===2)}
      throw new Error(url);
    }});
  await runtime.poll();
  assert.equal(runtime.snapshot().fleetState.malformed,2,
    "one malformed record before and one inside the raw tail are counted");
  assert.equal(runtime.snapshot().journalState.malformed,1);
  runtime.tick();
  assert.equal(runtime.snapshot().fleetState.malformed,2,"empty republish cannot double count");
  await runtime.poll();
  assert.equal(runtime.snapshot().fleetState.malformed,2,
    "a reset rebuild has the same full-response count rather than accumulating");
});

test("rotation retains exact approval lifecycle for retained journal residents",async()=>{
  const knock=(id="rotation")=>({v:0,ts:new Date(BASE-120000).toISOString(),source:"codex",
    agent_id:"codex:life",project:"life",type:"needs_human",payload:{message:"May I?",
      request_id:id,action:"publish",detail:null,options:["approve","deny"]}});
  const activity={...knock(),type:"tool_called",payload:{tool:"Read"}};
  const journalAt=offset=>written({ts:new Date(BASE+offset).toISOString()});
  const close=(id="rotation")=>({v:0,ts:new Date(BASE-119000).toISOString(),source:"steward",
    agent_id:"codex:life",project:"life",type:"needs_human_resolved",
    payload:{request_id:id,decision:"approve",decided_by:"api",action:"publish"}});
  const chatter=Array.from({length:4001},(_,index)=>({v:0,
    ts:new Date(BASE+index).toISOString(),source:"codex",agent_id:`codex:other-${index}`,
    project:"other",type:"tool_called",payload:{tool:"Read"}}));
  async function project(prefix) {
    const runtime=createBrowserRuntime({now:()=>BASE+5000,EventSource:null,
      setTimeout:()=>1,clearTimeout(){},fetch:async url=>{
        if(url==="/villagers")return{ok:true,json:async()=>[resident]};
        if(url.startsWith("/events"))return response([...prefix,...chatter]
          .map(JSON.stringify).join("\n"),cursor(10),true);
        throw new Error(url);
      }});
    await runtime.poll(); return runtime.snapshot();
  }
  for(const journal of [journalAt(0),journalAt(-120000)]) {
    const snapshot=await project([knock(),activity,journal]);
    const villager=snapshot.villagers.find(item=>item.id==="codex:life");
    assert.equal(villager.state,"knocking");
    assert.equal(villager.knock.request_id,"rotation");
    assert.equal(snapshot.approvalState.requests.size,1);
  }
  const expiredJournal=journalAt(-120000);
  const resolved=await project([knock(),activity,expiredJournal,close()]);
  assert.equal(resolved.villagers.some(item=>item.id==="codex:life"),false,
    "a resolved old lifecycle cannot resurrect an expired journal resident");
  const collision={...knock(),payload:{...knock().payload,message:"Different immutable question"}};
  const collided=await project([knock(),activity,expiredJournal,collision]);
  assert.equal(collided.villagers.some(item=>item.id==="codex:life"),false,
    "a collided request cannot resurrect an expired journal resident");
  assert.equal(collided.approvalState.requests.get("rotation").collided,true);
});

test("grouped bootstrap/reset and incremental delivery preserve displaced knock ordinals",async()=>{
  const request={v:0,ts:new Date(BASE-120000).toISOString(),source:"codex",
    agent_id:"codex:life",project:"life",type:"needs_human",payload:{message:"Structured",
      request_id:"ordinal",action:"publish",detail:null,options:["approve","deny"]}};
  const journal=written({ts:new Date(BASE-60000).toISOString()});
  const chatter=Array.from({length:3999},(_,index)=>({v:0,
    ts:new Date(BASE+index).toISOString(),source:"codex",agent_id:"codex:other",
    project:"other",type:"tool_called",payload:{tool:"Read",index}}));
  const plain={...request,ts:new Date(BASE-30000).toISOString(),
    payload:{message:"Independent plain knock"}};
  const malformed={...plain,payload:{message:"Independent malformed knock",
    request_id:"broken",action:"Publish",detail:null,options:["approve"]}};
  class EventSource{constructor(){this.listeners={}} addEventListener(name,listener){this.listeners[name]=listener}
    close(){this.closed=true}}
  const summary=runtime=>{
    const snapshot=runtime.snapshot();
    const villager=snapshot.villagers.find(item=>item.id==="codex:life");
    return {state:villager&&villager.state,message:villager&&villager.knock&&villager.knock.message,
      request_id:villager&&villager.knock&&villager.knock.request_id,
      malformed:snapshot.approvalState.malformed,
      requestOrdinal:snapshot.approvalState.requests.get("ordinal").knockOrdinal,
      ordinaryOrdinal:snapshot.approvalState.ordinalForEvent(
        villager&&villager.events.find(event=>event.payload&&event.payload.message===
          (villager.knock&&villager.knock.message)))};
  };
  async function grouped(events){
    let polls=0;
    const runtime=createBrowserRuntime({now:()=>BASE+5000,EventSource:null,
      setTimeout:()=>1,clearTimeout(){},fetch:async url=>{
        if(url==="/villagers")return{ok:true,json:async()=>[resident]};
        if(url.startsWith("/events")){polls++;return response(events.map(JSON.stringify).join("\n"),
          cursor(polls*10),polls===2)}
        throw new Error(url);
      }});
    await runtime.poll(); const bootstrap=summary(runtime);
    await runtime.poll(); return {bootstrap,reset:summary(runtime)};
  }
  async function incremental(events){
    let stream;
    const runtime=createBrowserRuntime({now:()=>BASE+5000,
      EventSource:class extends EventSource{constructor(){super();stream=this}},
      setTimeout:()=>1,clearTimeout(){},fetch:async url=>{
        if(url==="/villagers")return{ok:true,json:async()=>[resident]};
        if(url.startsWith("/events"))return response("",cursor(1));
        throw new Error(url);
      }});
    await runtime.poll(); runtime.connectStream();
    events.forEach((event,index)=>stream.onmessage({lastEventId:cursor(index+2),
      data:JSON.stringify(event)}));
    await stream.listeners.ready({lastEventId:cursor(events.length+1),
      data:JSON.stringify({cursor:cursor(events.length+1)})});
    return summary(runtime);
  }
  for(const independent of [plain,malformed]) {
    const events=[request,journal,independent,...chatter];
    const expected={state:"knocking",message:independent.payload.message,request_id:null,
      malformed:independent===malformed?1:0,requestOrdinal:"1",ordinaryOrdinal:"3"};
    const views=await grouped(events);
    assert.deepEqual(views.bootstrap,expected,
      "bootstrap keeps the ordinary knock that approval selection displaced");
    assert.deepEqual(views.reset,expected,
      "reset rebuild preserves the original full-response append position");
    assert.deepEqual(await incremental(events),expected,
      "one-event delivery and grouped parsing share append-order authority");
  }
});

test("grouped reset and SSE never assign child journal recency through a shared project",async()=>{
  const projectResident={...resident,file:"project.resident.json",match:{project:"life"}};
  for(const prefix of ["claude-code","codex"]) for(const order of ["before","after"]) {
    const child=`${prefix}:visitor`, journal=written({agent_id:child});
    const lineage={v:0,ts:new Date(BASE+(order==="before"?-1:1)).toISOString(),source:prefix,
      agent_id:child,project:"life",type:"tool_called",
      payload:{tool:"Read",parent_agent_id:`${prefix}:root`}};
    const events=order==="before"?[lineage,journal]:[journal,lineage];
    let polls=0,stream;
    class EventSource{constructor(){this.listeners={};stream=this} addEventListener(n,f){this.listeners[n]=f} close(){}}
    const runtime=createBrowserRuntime({now:()=>BASE+1000,EventSource,setTimeout:()=>1,clearTimeout(){},
      fetch:async url=>{
        if(url==="/villagers")return{ok:true,json:async()=>[projectResident]};
        if(url.startsWith("/events")){polls++;return response(events.map(JSON.stringify).join("\n"),
          cursor(polls*10),polls>1)}
        throw new Error(url);
      }});
    await runtime.poll();
    for(const phase of ["bootstrap","reset"]) {
      const visitor=runtime.snapshot().villagers.find(item=>item.id===child);
      assert.equal(visitor&&visitor.residency,"visitor",`${prefix} ${order} ${phase}`);
      assert.equal(visitor&&visitor.journal,null,`${prefix} ${order} ${phase}`);
      assert.match(runtime.snapshot().journalState.ownershipDiagnostics[0].reason,/visitor child/);
      if(phase==="bootstrap") await runtime.poll();
    }
    runtime.connectStream();
    const next=written({agent_id:child,payload:{...journal.payload,day:"2026-08-26",
      path:"/journal/2026-08-26.md"}});
    stream.onmessage({lastEventId:cursor(21),data:JSON.stringify(next)});
    await stream.listeners.ready({lastEventId:cursor(21),data:JSON.stringify({cursor:cursor(21)})});
    const visitor=runtime.snapshot().villagers.find(item=>item.id===child);
    assert.equal(visitor&&visitor.residency,"visitor",`${prefix} ${order} SSE`);
    assert.equal(visitor&&visitor.journal,null,`${prefix} ${order} SSE`);
  }
});
