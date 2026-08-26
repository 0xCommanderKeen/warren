"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const journals = require("../viewer/journal-observations.js");
const projection = require("../viewer/projection.js");
const approvals = require("../viewer/approval-knocks.js");

const BASE = Date.parse("2026-08-25T20:31:00.000Z");
const fixture = (agent="codex:life-agent", day="2026-08-25", offset=0) => ({
  v:0, ts:new Date(BASE + offset).toISOString(), source:"steward", agent_id:agent,
  project:"life-agent", type:"journal_written",
  payload:{routine:"close-of-day",day,path:`/journal/${day}.md`},
});
const soul = (agent="codex:life-agent") => ({valid:true,manifest_version:1,home:1,
  match:{agent_id:agent},meta:{name:"Hob",char:"Monk",accent:"#a68a4f"}});
function fold(events) {
  const parsed = projection.parseEvents(events), state = journals.createState();
  journals.foldValidated(state, parsed, {isValidatedBatch:projection.isValidatedBatch,
    rejections:projection.journalRejections(parsed)});
  return {parsed,state};
}
function fixtureCases() {
  return JSON.parse(fs.readFileSync("tests/fixtures/journal-observations.json")).map(source => {
    const item=structuredClone(source), prefix=item.path_prefix;
    delete item.path_prefix;
    if(prefix) item.event.payload.path=`/${prefix.scalar.repeat(prefix.count)}/${item.event.payload.path}`;
    return item;
  });
}

test("Python and browser share the journal validation matrix", () => {
  const cases=fixtureCases();
  for (const item of cases) {
    assert.equal(projection.validateEvent(item.event) === null,item.valid,item.name);
    if(item.error) assert.equal(projection.validateEvent(item.event),item.error,item.name);
  }
});

test("slug, path, and Gregorian boundaries are exact", () => {
  const event=fixture("codex:edge","0001-01-01");
  event.payload.routine="a".repeat(128);
  const filename="0001-01-01.md";
  event.payload.path="/"+"a".repeat(2048-filename.length-2)+"/"+filename;
  assert.equal(event.payload.path.length,2048); assert.equal(projection.validateEvent(event),null);
  event.payload.path="x"+event.payload.path;
  assert.equal(projection.validateEvent(event),"invalid payload.path");
  assert.equal(projection.validateEvent(fixture("codex:max","9999-12-31")),null);
});

test("first append owns a day, replay cannot refresh it, and one collision is retained", () => {
  const first=fixture(), replay={...fixture(undefined,undefined,9000)}, conflict=fixture(undefined,undefined,10_000);
  conflict.payload={...conflict.payload,routine:"nightly"};
  const secondConflict=fixture(undefined,undefined,11_000);
  secondConflict.payload={...secondConflict.payload,path:"/else/2026-08-25.md"};
  const {state}=fold([first,replay,conflict,secondConflict]);
  const [record]=journals.records(state);
  assert.equal(record.event,first); assert.equal(record.ordinal,"1");
  assert.equal(record.conflict.event,conflict);
  assert.equal(state.diagnostics.length,1,"only the first collision consumes diagnostic capacity");
  assert.equal(journals.activityEntries(state)[0].ts,BASE);
});

test("retained collisions survive malformed pressure and track eviction/reset exactly", () => {
  const valid=[];
  for(let index=0;index<journals.MAX_DAYS;index++) {
    const agent=`codex:collision-${String(index).padStart(2,"0")}`;
    const canonical=fixture(agent), conflict=fixture(agent,undefined,index+1);
    conflict.payload={...conflict.payload,routine:"nightly"};
    valid.push(canonical,conflict);
  }
  const malformed=Array.from({length:journals.MAX_MALFORMED_DIAGNOSTICS+5},(_,index)=>
    fixture(`codex:malformed-${index}`,undefined,100+index));
  for(const event of malformed) event.source="codex";
  const raw=[...valid,...malformed];
  const grouped=journals.createState(), groupedBatch=projection.parseEvents(raw);
  journals.foldValidated(grouped,groupedBatch,{isValidatedBatch:projection.isValidatedBatch,
    rejections:projection.journalRejections(groupedBatch)});

  const incremental=journals.createState();
  for(let start=0;start<raw.length;start+=7) {
    const batch=projection.parseEvents(raw.slice(start,start+7));
    journals.foldValidated(incremental,batch,{isValidatedBatch:projection.isValidatedBatch,
      rejections:projection.journalRejections(batch)});
  }
  for(const state of [grouped,incremental]) {
    assert.equal(state.records.size,journals.MAX_DAYS);
    assert.equal(state.collisionDiagnostics.length,journals.MAX_DAYS,
      "every retained conflicted key keeps authoritative diagnostic evidence");
    assert.equal(state.malformed,journals.MAX_MALFORMED_DIAGNOSTICS+5);
    assert.equal(state.malformedDiagnostics.length,journals.MAX_MALFORMED_DIAGNOSTICS,
      "malformed details have an independent newest-forty bound");
    assert.equal(state.diagnostics.length,
      journals.MAX_DAYS+journals.MAX_MALFORMED_DIAGNOSTICS);
    assert.equal(new Set(state.collisionDiagnostics.map(item=>item.key)).size,journals.MAX_DAYS,
      "one diagnostic is exposed per retained collision key");
  }
  assert.deepEqual(grouped.diagnostics,incremental.diagnostics,
    "grouped and incremental delivery expose the same deterministic diagnostic view");

  const evictedKey=`codex:collision-00\0${"2026-08-25"}`;
  const replacement=fixture("codex:replacement","2026-08-26",1000);
  const replacementConflict=fixture("codex:replacement","2026-08-26",1001);
  replacementConflict.payload={...replacementConflict.payload,path:"/else/2026-08-26.md"};
  let batch=projection.parseEvents([replacement,replacementConflict]);
  journals.foldValidated(grouped,batch,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  assert.equal(grouped.records.has(evictedKey),false);
  assert.equal(grouped.collisionDiagnostics.some(item=>item.key===`collision\0${evictedKey}`),false,
    "top-forty eviction removes the evicted record's collision diagnostic");
  assert.equal(grouped.collisionDiagnostics.length,journals.MAX_DAYS);

  const resetCanonical=fixture("codex:after-reset","2026-08-27",2000);
  const resetConflict=fixture("codex:after-reset","2026-08-27",2001);
  resetConflict.payload={...resetConflict.payload,routine:"nightly"};
  const resetMalformed=fixture("codex:reset-malformed","2026-08-27",2002);
  resetMalformed.source="codex";
  batch=projection.parseEvents([resetCanonical,resetConflict,resetMalformed]);
  journals.foldValidated(grouped,batch,{reset:true,isValidatedBatch:projection.isValidatedBatch,
    rejections:projection.journalRejections(batch)});
  assert.equal(grouped.records.size,1);
  assert.deepEqual(grouped.collisionDiagnostics.map(item=>item.key),
    [`collision\0${journals.keyFor(resetCanonical)}`]);
  assert.equal(grouped.malformed,1);
  assert.equal(grouped.malformedDiagnostics.length,1);
  assert.equal(grouped.diagnostics.length,2,
    "reset clears prior diagnostics and rebuilds only the new authority");
});

test("highest forty canonical day keys and diagnostics are bounded; reset rebuilds from empty", () => {
  const state=journals.createState();
  const days=[];
  for(let i=0;i<45;i++) {
    const day=`2026-${i<31?"07":"08"}-${String(i<31?i+1:i-30).padStart(2,"0")}`;
    days.push(fixture(`codex:${i}`,day,i));
  }
  let parsed=projection.parseEvents(days);
  journals.foldValidated(state,parsed,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  assert.equal(state.records.size,journals.MAX_DAYS); assert.equal(state.capacityDropped,5);
  assert.equal(journals.records(state)[0].event.agent_id,"codex:5");
  parsed=projection.parseEvents([fixture("claude-code:life-agent","2026-08-25",99)]);
  journals.foldValidated(state,parsed,{isValidatedBatch:projection.isValidatedBatch,rejections:[],reset:true});
  assert.equal(state.records.size,1); assert.equal(state.capacityDropped,0);
});

test("ten thousand incremental journal agents leave only bounded retained projection authority", () => {
  const state=journals.createState(), agents=new Map();
  const started=Date.now();
  for(let index=0;index<10_000;index++) {
    const day=new Date(Date.UTC(2000,0,1)+index*86_400_000).toISOString().slice(0,10);
    const event=fixture(`codex:stress-${String(index).padStart(5,"0")}`,day,index);
    const batch=projection.parseEvents([event]);
    journals.foldValidated(state,batch,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
    projection.foldEvents(agents,batch,state);
    assert.ok(state.evictedAgentIds.length<=1,"one append displaces at most one retained agent");
  }
  const elapsed=Date.now()-started;
  assert.equal(state.records.size,journals.MAX_DAYS);
  assert.equal(agents.size,journals.MAX_DAYS,
    "journal-only agent cache tracks retained authority, not total stream history");
  const cachedEvents=[];
  for(const agent of agents.values()) {
    if(agent.lastAny) cachedEvents.push(agent.lastAny);
    if(agent.lastOrdinaryAny) cachedEvents.push(agent.lastOrdinaryAny);
    cachedEvents.push(...agent.events);
  }
  assert.equal(cachedEvents.length,journals.MAX_DAYS*2,
    "each retained journal has only its lastAny and bounded visible-event references");
  assert.equal(cachedEvents.every(event=>state.recordForEvent(event)!==null),true,
    "no projection cache references an evicted journal object");
  assert.ok(elapsed<10_000,`bounded 10,000-agent fold took ${elapsed}ms`);
});

test("eviction restores ordinary state and selects a same-agent retained journal", () => {
  const state=journals.createState(), agents=new Map();
  const ordinary={v:0,ts:new Date(BASE-1).toISOString(),source:"codex",
    agent_id:"codex:keeper",project:"life-agent",type:"tool_called",payload:{tool:"Read"}};
  const low=fixture("codex:keeper","2000-01-01",0);
  const retained=fixture("codex:keeper","2099-12-31",1);
  let batch=projection.parseEvents([ordinary,low,retained]);
  journals.foldValidated(state,batch,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  projection.foldEvents(agents,batch,state);
  for(let index=0;index<journals.MAX_DAYS-1;index++) {
    const day=new Date(Date.UTC(2050,0,1)+index*86_400_000).toISOString().slice(0,10);
    const event=fixture(`codex:filler-${index}`,day,index+2);
    batch=projection.parseEvents([event]);
    journals.foldValidated(state,batch,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
    projection.foldEvents(agents,batch,state);
  }
  const keeper=agents.get("codex:keeper");
  assert.equal(state.recordForEvent(low),null);
  assert.equal(state.recordForEvent(retained).event,retained);
  assert.equal(keeper.lastAny,retained,"newer retained same-agent authority replaces the evicted journal");
  assert.equal(keeper.lastOrdinaryAny,ordinary);
  assert.deepEqual(keeper.events,[ordinary,retained]);

  for(let index=0;index<journals.MAX_DAYS;index++) {
    const day=new Date(Date.UTC(2100,0,1)+index*86_400_000).toISOString().slice(0,10);
    const finalReplacement=fixture(`codex:final-${index}`,day,100+index);
    batch=projection.parseEvents([finalReplacement]);
    journals.foldValidated(state,batch,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
    projection.foldEvents(agents,batch,state);
  }
  assert.equal(state.recordForEvent(retained),null);
  assert.equal(agents.get("codex:keeper").lastAny,ordinary,
    "evicting the final journal restores the exact ordinary state");
  assert.deepEqual(agents.get("codex:keeper").events,[ordinary]);
  const village=projection.reduce(agents,BASE+1000,[soul("codex:keeper")],null,state);
  const projected=village.find(item=>item.id==="codex:keeper");
  assert.equal(projected.lastLine,"reading");
  assert.equal(projected.journal,null);
});

test("journal reset reconciliation removes old journal-only agents without touching lineage", () => {
  const state=journals.createState(), agents=new Map();
  const old=fixture("codex:old","2026-08-25");
  const child=fixture("codex:child","2026-08-24");
  const lineage={v:0,ts:new Date(BASE-1).toISOString(),source:"codex",agent_id:"codex:child",
    project:"life-agent",type:"tool_called",payload:{tool:"Read",parent_agent_id:"codex:parent"}};
  let batch=projection.parseEvents([old,lineage,child]);
  journals.foldValidated(state,batch,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  projection.foldEvents(agents,batch,state);

  const replacement=fixture("codex:new","2026-08-26",1);
  batch=projection.parseEvents([replacement]);
  journals.foldValidated(state,batch,{reset:true,isValidatedBatch:projection.isValidatedBatch,
    rejections:[]});
  projection.foldEvents(agents,batch,state);
  assert.equal(agents.has("codex:old"),false,"reset drops an old journal-only cache entry");
  assert.equal(agents.has("codex:child"),true,"explicit child lineage survives journal reset");
  assert.equal(agents.get("codex:child").lastAny,lineage);
  assert.deepEqual(agents.get("codex:child").events,[lineage]);
  assert.equal(agents.has("codex:new"),true);
});

test("lineage carried only by an evicted journal cannot retain a ghost agent", () => {
  const state=journals.createState(),agents=new Map();
  const journal=fixture("codex:journal-child","2000-01-01");
  journal.payload={...journal.payload,parent_agent_id:"codex:parent"};
  let batch=projection.parseEvents([journal]);
  journals.foldValidated(state,batch,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  projection.foldEvents(agents,batch,state);
  assert.equal(agents.get("codex:journal-child").parentAgentId,null,
    "journal lineage stays on its bounded record rather than a permanent agent cache");
  const pressure=Array.from({length:journals.MAX_DAYS},(_,index)=>{
    const day=new Date(Date.UTC(2050,0,1)+index*86_400_000).toISOString().slice(0,10);
    return fixture(`codex:lineage-pressure-${index}`,day,index+1);
  });
  batch=projection.parseEvents(pressure);
  journals.foldValidated(state,batch,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  projection.foldEvents(agents,batch,state);
  assert.equal(agents.has("codex:journal-child"),false);
});

test("journal eviction preserves approval, session, and visitor authority exactly", () => {
  const state=journals.createState(),agents=new Map();
  const ordinary=[
    {v:0,ts:new Date(BASE-10).toISOString(),source:"codex",agent_id:"codex:approval",
      project:"life-agent",type:"needs_human",payload:{message:"May I?",request_id:"bounded",
        action:"publish",detail:null,options:["approve","deny"]}},
    {v:0,ts:new Date(BASE-9).toISOString(),source:"codex",agent_id:"codex:session",
      project:"life-agent",type:"session_ended",payload:{}},
    {v:0,ts:new Date(BASE-8).toISOString(),source:"codex",agent_id:"codex:child",
      project:"life-agent",type:"tool_called",payload:{tool:"Read",parent_agent_id:"codex:root"}},
  ];
  const journalsForOrdinary=ordinary.map((event,index)=>
    fixture(event.agent_id,`2000-01-0${index+1}`,index));
  let batch=projection.parseEvents(ordinary.flatMap((event,index)=>[event,journalsForOrdinary[index]]));
  journals.foldValidated(state,batch,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  projection.foldEvents(agents,batch,state);
  const pressure=Array.from({length:journals.MAX_DAYS},(_,index)=>{
    const day=new Date(Date.UTC(2050,0,1)+index*86_400_000).toISOString().slice(0,10);
    return fixture(`codex:authority-pressure-${index}`,day,20+index);
  });
  batch=projection.parseEvents(pressure);
  journals.foldValidated(state,batch,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  projection.foldEvents(agents,batch,state);
  for(const [index,event] of ordinary.entries()) {
    const agent=agents.get(event.agent_id);
    assert.ok(agent,`${event.type} cache survives`);
    assert.equal(agent.lastAny,event);
    assert.equal(agent.lastOrdinaryAny,event);
    assert.deepEqual(agent.events,[event]);
    assert.equal(state.recordForEvent(journalsForOrdinary[index]),null);
  }
  assert.equal(agents.get("codex:child").parentAgentId,"codex:root");
});

test("evicted and below-frontier keys cannot re-enter or refresh capacity", () => {
  const state=journals.createState();
  const initial=Array.from({length:journals.MAX_DAYS},(_,i)=>
    fixture(`codex:${String(i).padStart(2,"0")}`,"2026-08-24",i));
  let parsed=projection.parseEvents(initial);
  journals.foldValidated(state,parsed,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  parsed=projection.parseEvents([fixture("codex:new","2026-08-25",100)]);
  journals.foldValidated(state,parsed,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  assert.equal(state.records.has(`codex:00\0${"2026-08-24"}`),false);
  assert.equal(state.capacityDropped,1);

  const replay=fixture("codex:00","2026-08-24",101);
  const conflict=fixture("codex:00","2026-08-24",102);
  conflict.payload={...conflict.payload,routine:"nightly"};
  parsed=projection.parseEvents([replay,conflict,replay,conflict]);
  journals.foldValidated(state,parsed,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  assert.equal(state.records.has(`codex:00\0${"2026-08-24"}`),false);
  assert.equal(state.records.has(`codex:01\0${"2026-08-24"}`),true,
    "ignored old evidence cannot force a second eviction");
  assert.equal(state.capacityDropped,1);
  assert.equal(state.diagnostics.length,0);
});

test("day then Unicode-scalar agent rank is stable across grouping and reset", () => {
  const older=Array.from({length:39},(_,i)=>fixture(`codex:${String(i).padStart(2,"0")}`,"2026-08-24",i));
  const privateUse=fixture("codex:\uE000","2026-08-25",40);
  const astral=fixture("codex:😀","2026-08-25",41);
  const all=[...older,privateUse,astral];
  const expectedKeys=new Set(all.slice(1).map(journals.keyFor));
  const state=journals.createState();
  for(const group of [all.slice(0,7),all.slice(7,23),all.slice(23)]) {
    const parsed=projection.parseEvents(group);
    journals.foldValidated(state,parsed,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  }
  assert.deepEqual(new Set(journals.records(state).map(record=>record.key)),expectedKeys);
  assert.equal(state.records.has(journals.keyFor(astral)),true,
    "U+1F600 ranks after U+E000 by scalar value, not UTF-16 code unit");

  const resetBatch=projection.parseEvents(all);
  journals.foldValidated(state,resetBatch,{reset:true,isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  assert.deepEqual(new Set(journals.records(state).map(record=>record.key)),expectedKeys);
});

test("capacity eviction also removes stale village/history authority", () => {
  const state=journals.createState(),agents=new Map();
  for(let i=0;i<=journals.MAX_DAYS;i++) {
    const parsed=projection.parseEvents([fixture(`codex:capacity-${i}`,"2026-08-25",i)]);
    journals.foldValidated(state,parsed,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
    projection.foldEvents(agents,parsed,state);
  }
  assert.equal(state.records.has(`codex:capacity-0\0${"2026-08-25"}`),false);
  assert.deepEqual(projection.reduce(agents,BASE+1000,[soul("codex:capacity-0")],null,state),[],
    "an evicted observation cannot keep a desk animation or retained detail row");
  assert.equal(agents.has("codex:capacity-0"),false,
    "an evicted journal-only agent cannot remain in the projection cache");
});

test("a retained same-agent day replaces an evicted cached journal in every fold shape", () => {
  const ordinary={v:0,ts:new Date(BASE-1000).toISOString(),source:"codex",
    agent_id:"codex:life-agent",project:"life-agent",type:"tool_called",payload:{tool:"Read"}};
  const retained=fixture("codex:life-agent","2026-08-24",0);
  const laterButEvicted=fixture("codex:life-agent","2026-07-01",1);
  const pressure=Array.from({length:39},(_,index)=>
    fixture(`codex:pressure-${String(index).padStart(2,"0")}`,"2026-08-25",index+2));
  const all=[ordinary,retained,laterButEvicted,...pressure];

  for(const mode of ["incremental","grouped"]) {
    const state=journals.createState(),agents=new Map();
    const groups=mode==="incremental" ? [[ordinary,retained,laterButEvicted],...pressure.map(item=>[item])] : [all];
    for(const group of groups) {
      const parsed=projection.parseEvents(group);
      journals.foldValidated(state,parsed,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
      projection.foldEvents(agents,parsed,state);
    }
    assert.equal(state.records.has(journals.keyFor(laterButEvicted)),false,mode);
    const resident=projection.reduce(agents,BASE+1000,[soul()],null,state)
      .find(item=>item.id==="codex:life-agent");
    assert.equal(resident.doing,"writing the journal",mode);
    assert.equal(resident.journal.event,retained,mode);

    const later={...ordinary,ts:new Date(BASE+2000).toISOString(),payload:{tool:"Bash"}};
    const parsed=projection.parseEvents([later]);
    journals.foldValidated(state,parsed,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
    projection.foldEvents(agents,parsed,state);
    assert.equal(projection.reduce(agents,BASE+2001,[soul()],null,state)
      .find(item=>item.id==="codex:life-agent").doing,"tinkering",
    `${mode}: later ordinary append remains authoritative`);
  }

  const resetState=journals.createState(),resetAgents=new Map();
  const resetBatch=projection.parseEvents(all);
  journals.foldValidated(resetState,resetBatch,{reset:true,
    isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  projection.foldEvents(resetAgents,resetBatch,resetState);
  assert.equal(projection.reduce(resetAgents,BASE+1000,[soul()],null,resetState)
    .find(item=>item.id==="codex:life-agent").journal.event,retained,
  "a grouped reset derives the same retained day");
});

test("latest effective journal searches retained active authority, not only latest append", () => {
  const active=fixture("codex:life-agent","2026-08-24",0);
  const expired=fixture("codex:life-agent","2026-08-25",-journals.ACTIVE_MS);
  const ordinary={v:0,ts:new Date(BASE-2000).toISOString(),source:"codex",
    agent_id:"codex:life-agent",project:"life-agent",type:"tool_called",payload:{tool:"Read"}};
  const parsed=projection.parseEvents([ordinary,active,expired]);
  const state=journals.createState(),agents=new Map();
  journals.foldValidated(state,parsed,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  projection.foldEvents(agents,parsed,state);
  const resident=projection.reduce(agents,BASE+1000,[soul()],null,state)[0];
  assert.equal(resident.journal.event,active);
  assert.equal(resident.doing,"writing the journal");
});

test("matched Claude and Codex residents animate at home, visitors and unmatched facts never do", () => {
  for(const agent of ["claude-code:life-agent","codex:life-agent"]) {
    const event=fixture(agent), {parsed,state}=fold([event]), agents=new Map();
    projection.foldEvents(agents,parsed,state);
    const [resident]=projection.reduce(agents,BASE+30_000,[soul(agent)],null,state);
    assert.equal(resident.residency,"resident"); assert.equal(resident.place,null);
    assert.equal(resident.doing,"writing the journal"); assert.equal(resident.journal.event,event);
    assert.deepEqual(projection.reduce(agents,BASE+30_000,[],null,state),[],
      "unmatched observations stay out of the village");
    assert.deepEqual(projection.reduce(agents,BASE+30_000,[{...soul(agent),valid:false}],null,state),[],
      "invalid declarations cannot grant a desk or house");
  }
});

test("one ownership resolver keeps Claude and Codex child journals Fleet-only across lineage order", () => {
  const projectResident={...soul("unused"),file:"project.resident.json",
    match:{project:"life-agent"}};
  const invalid={...projectResident,file:"invalid.resident.json",valid:false};
  for(const prefix of ["claude-code","codex"]) for(const order of ["before","after"]) {
    const agent=`${prefix}:child`, journal=fixture(agent);
    const lineage={v:0,ts:new Date(BASE+(order==="before"?-1:1)).toISOString(),source:prefix,
      agent_id:agent,project:"life-agent",type:"tool_called",
      payload:{tool:"Read",parent_agent_id:`${prefix}:root`}};
    const events=order==="before"?[lineage,journal]:[journal,lineage];
    const {parsed,state}=fold(events),agents=new Map();
    projection.foldEvents(agents,parsed,state);
    const [visitor]=projection.reduce(agents,BASE+1000,[projectResident],null,state);
    assert.equal(visitor.id,agent,`${prefix} ${order}`);
    assert.equal(visitor.residency,"visitor",`${prefix} ${order}`);
    assert.equal(visitor.journal,null,`${prefix} ${order}`);
    assert.equal(journals.ownerFor(state,journals.records(state)[0]),null,`${prefix} ${order}`);
    assert.match(state.ownershipDiagnostics[0].reason,/visitor child/,`${prefix} ${order}`);
  }

  const direct=fixture("codex:payload-child");
  direct.payload={...direct.payload,parent_agent_id:"codex:root"};
  let {parsed,state}=fold([direct]); let agents=new Map();
  projection.foldEvents(agents,parsed,state);
  assert.deepEqual(projection.reduce(agents,BASE+1,[projectResident],null,state),[]);
  assert.match(state.ownershipDiagnostics[0].reason,/visitor child/);

  ({parsed,state}=fold([fixture("codex:root")])); agents=new Map();
  projection.foldEvents(agents,parsed,state);
  assert.equal(projection.reduce(agents,BASE+1,[projectResident],null,state)[0].home,1,
    "a lineage-free project root owns its declared project journal");

  ({parsed,state}=fold([fixture("codex:root")])); agents=new Map();
  projection.foldEvents(agents,parsed,state);
  assert.deepEqual(projection.reduce(agents,BASE+1,[invalid],null,state),[]);
  assert.match(state.ownershipDiagnostics[0].reason,/unmatched resident/);

  const collision={...projectResident,file:"project-two.resident.json",home:2};
  ({parsed,state}=fold([fixture("codex:root")])); agents=new Map();
  projection.foldEvents(agents,parsed,state);
  assert.deepEqual(projection.reduce(agents,BASE+1,[projectResident,collision],null,state),[]);
  assert.match(state.ownershipDiagnostics[0].reason,/ambiguous project/);
});

test("expiry restores prior ordinary timestamp and later ordinary/session evidence wins", () => {
  const ordinary={v:0,ts:new Date(BASE-1000).toISOString(),source:"codex",agent_id:"codex:life-agent",
    project:"life-agent",type:"tool_called",payload:{tool:"Read"}};
  const written=fixture(), later={...ordinary,ts:new Date(BASE+1).toISOString(),payload:{tool:"Bash"}};
  let {parsed,state}=fold([ordinary,written]); const agents=new Map();
  projection.foldEvents(agents,parsed,state);
  assert.equal(projection.reduce(agents,BASE+59_999,[soul()],null,state)[0].doing,"writing the journal");
  const restored=projection.reduce(agents,BASE+60_000,[soul()],null,state)[0];
  assert.equal(restored.doing,"reading"); assert.equal(restored.lastTs,BASE-1000);
  ({parsed}=fold([later])); projection.foldEvents(agents,parsed,state);
  assert.equal(projection.reduce(agents,BASE+2,[soul()],null,state)[0].doing,"tinkering");
  const ended={...later,ts:new Date(BASE+3).toISOString(),type:"session_ended",payload:{}};
  ({parsed}=fold([ended])); projection.foldEvents(agents,parsed,state);
  assert.deepEqual(projection.reduce(agents,BASE+4,[soul()],null,state),[]);
});

test("pending structured approval retains precedence over a later journal observation", () => {
  const knock={v:0,ts:new Date(BASE-1).toISOString(),source:"codex",agent_id:"codex:life-agent",
    project:"life-agent",type:"needs_human",payload:{message:"May I?",request_id:"r1",
      action:"publish",detail:null,options:["approve","deny"]}};
  const written=fixture(), parsed=projection.parseEvents([knock,written]);
  const approvalState=approvals.createState(), journalState=journals.createState(), agents=new Map();
  approvals.foldValidated(approvalState,parsed,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  journals.foldValidated(journalState,parsed,{isValidatedBatch:projection.isValidatedBatch,rejections:[]});
  projection.foldEvents(agents,parsed,journalState);
  const [resident]=projection.reduce(agents,BASE+1,[soul()],approvalState,journalState);
  assert.equal(resident.state,"knocking"); assert.equal(resident.journal,null);
});
