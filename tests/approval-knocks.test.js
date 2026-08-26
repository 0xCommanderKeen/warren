"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const crypto = require("node:crypto");
const approvals = require("../viewer/approval-knocks.js");
const projection = require("../viewer/projection.js");

const BOOT = "0123456789abcdef0123456789abcdef";
const RESET_BOOT = "fedcba9876543210fedcba9876543210";
const cursor = (offset, boot = BOOT) => `v1:${boot}:1:2:3:${offset}`;
const knock = (id, ts = "2026-08-25T10:00:00.000Z", overrides = {}) => ({
  v:0,ts,source:"any-emitter",agent_id:"codex:keeper",project:"life",type:"needs_human",
  payload:{message:"May I?",request_id:id,action:"send_email",detail:{to:"a@example.com"},
    options:["approve","deny","edit"],...overrides},
});
const resolved = (id, ts = "2026-08-25T10:01:00.000Z", overrides = {}) => ({
  v:0,ts,source:"steward",agent_id:"codex:keeper",project:"life",type:"needs_human_resolved",
  payload:{request_id:id,decision:"approve",decided_by:"api",action:"send_email",...overrides},
});
const expected = id => { const event=knock(id); return {agent_id:event.agent_id,
  project:event.project,action:event.payload.action,
  lifecycle:approvals.lifecycleIdentity(event,approvals.classify(event))}; };
function fold(events, state = approvals.createState(), reset = false) {
  const batch = projection.parseEvents(events);
  approvals.foldValidated(state, batch, {isValidatedBatch:projection.isValidatedBatch,
    rejections:projection.approvalRejections(batch),reset});
  return state;
}

test("public append ordinals normalize and compare without numeric precision loss", () => {
  assert.equal(approvals.normalizeOrdinal(undefined), "0");
  assert.equal(approvals.normalizeOrdinal(null), "0");
  assert.equal(approvals.normalizeOrdinal(-1n), "0");
  assert.equal(approvals.normalizeOrdinal("01"), "0", "non-canonical decimals use the fixed fallback");
  assert.equal(approvals.normalizeOrdinal(9007199254740993), "0",
    "unsafe Number input never becomes ordering authority");
  assert.equal(approvals.normalizeOrdinal(90071992547409931234567890n),
    "90071992547409931234567890");
  assert.equal(approvals.compareOrdinal("90071992547409931234567891",
    90071992547409931234567890n), 1);
  assert.equal(approvals.compareOrdinal(90071992547409931234567890n,
    "90071992547409931234567891"), -1);
  assert.equal(approvals.compareOrdinal(undefined, "0"), 0);
  assert.equal(approvals.compareOrdinal("not-an-ordinal", undefined), 0);
});

function identityBytes(value) {
  const length = value => { const bytes=Buffer.alloc(8);bytes.writeBigUInt64BE(BigInt(value));return bytes; };
  if (value === null) return Buffer.from("n");
  if (typeof value === "boolean") return Buffer.from(value ? "b1" : "b0");
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return Buffer.from("x");
    const bytes=Buffer.alloc(9);bytes[0]="d".charCodeAt(0);
    bytes.writeDoubleBE(Object.is(value,-0) ? 0 : value,1);return bytes;
  }
  if (typeof value === "string") {
    const bytes=Buffer.from(value,"utf16le");bytes.swap16();
    return Buffer.concat([Buffer.from("s"),length(bytes.length),bytes]);
  }
  if (Array.isArray(value)) return Buffer.concat([
    Buffer.from("a"),length(value.length),...value.map(identityBytes)]);
  if (value && typeof value === "object") {
    const entries=Object.keys(value).sort().map(key =>
      Buffer.concat([identityBytes(key),identityBytes(value[key])]));
    return Buffer.concat([Buffer.from("o"),length(entries.length),...entries]);
  }
  return Buffer.from("x");
}

test("notification v3 identity has a stable cross-language canonical hash", () => {
  const vector=JSON.parse(fs.readFileSync("tests/fixtures/notification-identity.json","utf8"));
  const event=vector.event, payload=event.payload;
  const identity={version:3,event_version:event.v,type:event.type,ts:event.ts,
    source:event.source,agent_id:event.agent_id,project:event.project,approval:{
      request_id:payload.request_id,action:payload.action,message:payload.message,
      detail:payload.detail,options:payload.options,expires_at:{present:true,value:null}}};
  const key="structured-v3-sha256-"+crypto.createHash("sha256")
    .update(identityBytes(identity)).digest("hex");
  assert.equal(key,vector.expected_key);
});

test("fixture projects structured, legacy, malformed, orphan, duplicate, and out-of-order evidence", () => {
  const events = fs.readFileSync("tests/fixtures/approvals.jsonl","utf8")
    .trim().split("\n").map(JSON.parse);
  const state = fold(events);
  assert.equal(state.requests.get("req-send").resolution.payload.decision,"edit",
    "the first closing fact wins over a conflicting duplicate");
  assert.equal(state.requests.get("req-late").resolution,null,
    "an orphan decision never binds to a request appended later");
  assert.equal(state.malformed,1);
  assert.match(state.diagnostics.map(item=>item.reason).join("\n"),/unknown request_id/);
  assert.match(state.diagnostics.map(item=>item.reason).join("\n"),/already resolved/);
  const villagers = projection.reduce(events,Date.parse("2026-08-25T10:08:00.000Z"),[],state);
  assert.equal(villagers.find(v=>v.id==="claude-code:plain").state,"knocking");
  assert.equal(villagers.find(v=>v.id==="codex:malformed").state,"knocking");
  assert.equal(villagers.find(v=>v.id==="codex:keeper").state,"resting");
  assert.equal(villagers.find(v=>v.id==="codex:late").state,"knocking");
});

test("shape detection is source-independent and malformed attempts remain plain knocks", () => {
  assert.equal(approvals.classify(knock("r1")).kind,"structured");
  assert.deepEqual(approvals.classify({...knock("r2"),source:"custom"}).options,
    ["approve","deny","edit"]);
  assert.equal(approvals.classify(knock("r3",undefined,{options:[]})).kind,"malformed");
  assert.equal(approvals.classify(knock("null-detail",undefined,{detail:null})).detail,null,
    "wire-level null detail is preserved");
  const brokenDetail=knock("r4",undefined,{detail:["not","an","object"]});
  assert.equal(projection.validateEvent(brokenDetail),null,
    "shape defects remain available for legacy degradation");
  assert.equal(approvals.classify(brokenDetail).kind,"malformed");
  assert.equal(approvals.classify({type:"needs_human",payload:{message:"plain"}}).kind,"plain");
});

test("repeated known options preserve order without malformed diagnostics", () => {
  const event=knock("repeat",undefined,{detail:{subject:"Repeat"},
    options:["approve","approve","deny","approve"]});
  assert.deepEqual(approvals.classify(event).options,
    ["approve","approve","deny","approve"]);
  const state=fold([event]);
  assert.deepEqual(state.requests.get("repeat").shape.options,
    ["approve","approve","deny","approve"]);
  assert.equal(state.malformed,0);
  assert.equal(state.diagnostics.length,0);
});

test("shared structured-shape matrix preserves browser classification parity", () => {
  const cases=JSON.parse(fs.readFileSync("tests/fixtures/approval-shapes.json","utf8"));
  for (const item of cases) {
    const event={v:0,ts:"2026-08-25T10:00:00.000Z",source:"custom",
      agent_id:"custom:keeper",project:"life",type:"needs_human",payload:item.payload};
    assert.equal(approvals.classify(event).kind,item.kind,item.name);
  }
});

test("shared lifecycle fixture preserves viewer/server selection parity", () => {
  const cases=JSON.parse(fs.readFileSync("tests/fixtures/approval-lifecycle.json","utf8"));
  for (const item of cases) {
    const state=fold(item.events);
    const record=state.requests.values().next().value;
    assert.equal(record.knock.ts,item.expected_request_ts,item.name);
    assert.equal(record.resolution?.payload.decision ?? null,item.expected_decision,item.name);
  }
});

test("shared immutable request fixture uses JSON semantic equality", () => {
  const cases=JSON.parse(fs.readFileSync("tests/fixtures/approval-identity.json","utf8"));
  for (const item of cases) {
    const left=knock(item.left.request_id,undefined,item.left);
    const right=knock(item.right.request_id,"2026-08-25T10:00:01.000Z",item.right);
    const state=fold([left,right]);
    assert.equal(state.requests.get(item.left.request_id).collided,!item.compatible,item.name);
  }
});

test("deep public approval identity is iterative and preserves exact lifecycle truth", () => {
  let detail = { leaf: 1.2345678901234567 };
  for (let index = 0; index < 3000; index++) detail = { child: detail };
  const request = knock("deep-public", undefined, { detail });
  const close = resolved("deep-public");
  const state = fold([request, close]);
  assert.equal(state.requests.get("deep-public").resolution, close);
  assert.doesNotThrow(() => approvals.lifecycleIdentity(request, approvals.classify(request)));
  const replay = knock("deep-public", "2026-08-25T10:02:00.000Z", { detail });
  assert.equal(fold([request, replay]).requests.get("deep-public").collided, false);
});

test("exact wire identities preserve surrounding whitespace and never cross-close", () => {
  const request={...knock(" request "),agent_id:" codex:keeper ",project:" life "};
  const wrong={...resolved("request"),agent_id:"codex:keeper",project:"life"};
  const exact={...resolved(" request ","2026-08-25T10:02:00.000Z"),
    agent_id:" codex:keeper ",project:" life "};
  const state=fold([request,wrong,exact]);
  assert.equal(state.requests.has(" request "),true);
  assert.equal(state.requests.has("request"),false);
  assert.equal(state.requests.get(" request ").resolution,exact);
  assert.match(state.diagnostics.map(item=>item.reason).join("\n"),/unknown request_id/);
});

test("pending requests queue newest-first and hold the villager at the door across later activity", () => {
  const first=knock("first"), second=knock("second","2026-08-25T10:02:00.000Z");
  const later={v:0,ts:"2026-08-25T10:03:00.000Z",source:"codex",agent_id:"codex:keeper",
    project:"life",type:"tool_called",payload:{tool:"Read"}};
  const state=fold([first,second,later]);
  assert.deepEqual(approvals.pendingForAgent(state,"codex:keeper").map(item=>item.id),
    ["second","first"]);
  let [villager]=projection.reduce([first,second,later],Date.parse("2026-08-25T10:04:00.000Z"),[],state);
  assert.equal(villager.state,"knocking");
  assert.equal(villager.knock.request_id,"second");
  fold([resolved("second","2026-08-25T10:05:00.000Z")],state);
  [villager]=projection.reduce([first,second,later],Date.parse("2026-08-25T10:06:00.000Z"),[],state);
  assert.equal(villager.state,"knocking");
  assert.equal(villager.knock.request_id,"first");
  fold([resolved("first","2026-08-25T10:07:00.000Z")],state);
  [villager]=projection.reduce([first,second,later],Date.parse("2026-08-25T10:08:00.000Z"),[],state);
  assert.equal(villager.state,"resting",
    "the later closing fact wins over activity emitted while approval was pending");
});

test("append order, never producer time, selects queues confirmations and resident state", () => {
  const futureActivity={v:0,ts:"2026-08-25T23:00:00.000Z",source:"codex",
    agent_id:"codex:keeper",project:"life",type:"tool_called",payload:{tool:"Read"}};
  const olderClose=resolved("append-close","2026-08-25T09:00:00.000Z");
  let events=[knock("append-close","2026-08-25T10:00:00.000Z"),futureActivity,olderClose];
  let state=fold(events);
  let [villager]=projection.reduce(events,Date.parse("2026-08-25T12:00:00.000Z"),[],state);
  assert.equal(villager.state,"resting",
    "a later close append wins even when its producer timestamp is older");

  const futureClose=resolved("later-work","2026-08-25T23:00:00.000Z");
  const olderActivity={...futureActivity,ts:"2026-08-25T11:59:00.000Z"};
  events=[knock("later-work"),futureClose,olderActivity];
  state=fold(events);
  [villager]=projection.reduce(events,Date.parse("2026-08-25T12:00:00.000Z"),[],state);
  assert.equal(villager.state,"working",
    "a later work append wins even when the earlier close claims a future time");

  const first=knock("pending-first","2026-08-25T23:00:00.000Z");
  const second=knock("pending-second","2026-08-25T08:00:00.000Z");
  state=fold([first,second]);
  assert.deepEqual(approvals.pendingForAgent(state,"codex:keeper").map(item=>item.id),
    ["pending-second","pending-first"]);

  events=[knock("confirmed-first"),resolved("confirmed-first","2026-08-25T23:00:00.000Z"),
    knock("confirmed-second"),resolved("confirmed-second","2026-08-25T08:00:00.000Z")];
  state=fold(events);
  [villager]=projection.reduce(events,Date.parse("2026-08-25T12:00:00.000Z"),[],state);
  assert.deepEqual(villager.approvals.map(item=>item.request_id),
    ["confirmed-second","confirmed-first"]);
  assert.equal(typeof state.sequence,"string");
  assert.equal(state.ordinals instanceof WeakMap,true,
    "append ordinals do not retain evicted transport events");
  assert.doesNotThrow(()=>JSON.stringify(villager),
    "projected append authority remains safe for JSON snapshots");
});

test("a structured close cannot suppress an independent plain or malformed knock", () => {
  const request=knock("exact-only");
  const plain={v:0,ts:"2026-08-25T09:00:00.000Z",source:"custom",
    agent_id:"codex:keeper",project:"life",type:"needs_human",payload:{message:"Plain later"}};
  const malformed={...plain,payload:{message:"Malformed later",request_id:"broken",
    action:"SEND",detail:null,options:["approve"]}};
  const close=resolved("exact-only","2026-08-25T23:00:00.000Z");
  for (const independent of [plain,malformed]) {
    for (const events of [[request,close,independent],[request,independent,close]]) {
      const state=fold(events);
      const [villager]=projection.reduce(events,Date.parse("2026-08-25T12:00:00.000Z"),[],state);
      assert.equal(villager.state,"knocking");
      assert.equal(villager.knock.message,independent.payload.message);
      assert.equal(villager.knock.structured,null);
      assert.equal(villager.approval,null);
    }
  }

  const other={...plain,agent_id:"codex:other",payload:{message:"Other agent"}};
  const state=fold([request,other,close]);
  const village=projection.reduce([request,other],Date.parse("2026-08-25T12:00:00.000Z"),[],state);
  assert.equal(village.find(item=>item.id==="codex:keeper").state,"resting");
  assert.equal(village.find(item=>item.id==="codex:other").state,"knocking");

  const plainFirst=fold([plain,knock("newer-structured"),resolved("newer-structured")]);
  const [resolvedLatest]=projection.reduce([plain,knock("newer-structured")],
    Date.parse("2026-08-25T12:00:00.000Z"),[],plainFirst);
  assert.equal(resolvedLatest.state,"resting",
    "a later structured lifecycle supersedes an earlier ordinary knock by append order");
});

test("approval identity collisions are quarantined without overwriting or inheriting a close", () => {
  const requestA=knock("reused");
  const closeA=resolved("reused","2026-08-25T10:01:00.000Z");
  const requestB={...knock("reused","2026-08-25T10:02:00.000Z",
    {action:"publish_note"}),agent_id:"codex:other"};
  const closeB={...resolved("reused","2026-08-25T10:03:00.000Z",
    {action:"publish_note",decision:"deny"}),agent_id:"codex:other"};
  const state=fold([requestA,closeA,requestB,closeB]);
  const record=state.requests.get("reused");
  assert.equal(record.knock.agent_id,"codex:keeper");
  assert.equal(record.resolution,closeA);
  assert.equal(record.collided,true);
  assert.deepEqual(approvals.pendingForAgent(state,"codex:keeper"),[]);
  assert.deepEqual(approvals.pendingForAgent(state,"codex:other"),[]);
  assert.match(state.diagnostics.map(item=>item.reason).join("\n"),/collision/);
  const village=projection.reduce([requestA,requestB],Date.parse("2026-08-25T10:04:00.000Z"),[],state);
  assert.equal(village.some(item=>item.state==="knocking"),false,
    "neither incompatible identity becomes an actionable villager");

  const sameAgentAction=fold([knock("same-agent"),
    knock("same-agent","2026-08-25T10:02:00.000Z",{action:"delete_file"})]);
  assert.equal(sameAgentAction.requests.get("same-agent").collided,true);
  assert.deepEqual(approvals.pendingForAgent(sameAgentAction,"codex:keeper"),[]);

  for (const field of ["detail","options","message","expires_at"]) {
    const first=knock(`changed-${field}`,undefined,{expires_at:null});
    const payload={...first.payload};
    if (field==="detail") payload.detail={to:"other@example.com"};
    if (field==="options") payload.options=["deny"];
    if (field==="message") payload.message="A different question";
    if (field==="expires_at") payload.expires_at="2026-08-26T10:00:00.000Z";
    const changed={...first,ts:"2026-08-25T10:02:00.000Z",payload};
    const projected=fold([first,changed]);
    assert.equal(projected.requests.get(`changed-${field}`).collided,true,field);
    assert.deepEqual(approvals.pendingForAgent(projected,"codex:keeper"),[],field);
  }
});

test("first matching decision in append order wins regardless of producer timestamp", () => {
  const request=knock("first-wins","2026-08-25T10:00:00.000Z");
  const approve=resolved("first-wins","2026-08-25T10:02:00.000Z",{decision:"approve"});
  const earlierTimestamp=resolved("first-wins","2026-08-25T10:01:00.000Z",{decision:"deny"});
  const state=fold([request,approve,earlierTimestamp]);
  assert.equal(state.requests.get("first-wins").resolution,approve);
  assert.equal(state.diagnostics.filter(item=>/already resolved/.test(item.reason)).length,1);
  const replayed=fold([request,approve,
    ...Array.from({length:approvals.MAX_DIAGNOSTICS+20},()=>({...approve,
      payload:{...approve.payload}}))]);
  assert.equal(replayed.diagnostics.filter(item=>/exact decision replay/.test(item.reason)).length,1,
    "exact replay is idempotent, bounded diagnostic evidence rather than a second close");
  assert.ok(replayed.diagnostics.length<=approvals.MAX_DIAGNOSTICS);
  assert.equal(replayed.requests.get("first-wins").resolution.payload.decision,"approve");
});

test("equal timestamp conflicts use first append order and semantic replay is idempotent", () => {
  const request=knock("equal-time");
  const approve=resolved("equal-time","2026-08-25T10:01:00.000Z",
    {decision:"approve",extension:{count:1,nested:{b:2,a:[true,null]}}});
  const replay={...approve,payload:{extension:{nested:{a:[true,null],b:2.0},count:1.0},
    action:"send_email",decided_by:"api",decision:"approve",request_id:"equal-time"}};
  const deny=resolved("equal-time","2026-08-25T10:01:00.000Z",{decision:"deny"});
  for (const [events,expected,replays] of [[[request,approve,replay,deny],"approve",1],
    [[request,deny,approve],"deny",0]]) {
    const state=fold(events), record=state.requests.get("equal-time");
    assert.equal(record.resolution.payload.decision,expected);
    assert.equal(state.diagnostics.filter(item=>/exact decision replay/.test(item.reason)).length,replays);
  }
});

test("orphan close is diagnostic-only and never binds to a later knock", () => {
  const close=resolved("orphan-first","2026-08-25T10:09:00.000Z",{decision:"deny"});
  const request=knock("orphan-first","2026-08-25T10:08:00.000Z");
  const state=fold([close,request]);
  assert.equal(state.requests.get("orphan-first").resolution,null);
  assert.match(state.diagnostics.map(item=>item.reason).join("\n"),/unknown request_id/);
  fold([resolved("orphan-first","2026-08-25T10:07:00.000Z",{decision:"approve"})],state);
  assert.equal(state.requests.get("orphan-first").resolution.payload.decision,"approve",
    "a later append closes even when its producer timestamp is earlier");
});

test("bounded unknown diagnostics and request eviction never invent a resolution", () => {
  const state=fold(Array.from({length:approvals.MAX_DIAGNOSTICS+20},(_,index)=>
    resolved(`orphan-${index}`,undefined,{decision:"deny"})));
  assert.ok(state.diagnostics.length<=approvals.MAX_DIAGNOSTICS);
  fold(Array.from({length:approvals.MAX_REQUESTS+1},(_,index)=>
    knock(`capacity-${index}`,new Date(Date.parse("2026-08-25T10:00:00.000Z")+index).toISOString())),state);
  assert.equal(state.requests.size,approvals.MAX_REQUESTS);
  assert.equal([...state.requests.values()].some(record=>record.resolution),false);
});

test("many later conflicts preserve the first append at an equal timestamp", () => {
  const request=knock("cutoff-order","2026-08-25T10:10:00.000Z");
  const first=resolved("cutoff-order","2026-08-25T10:08:00.000Z",{decision:"approve"});
  const second=resolved("cutoff-order","2026-08-25T10:08:00.000Z",{decision:"deny"});
  const closer=Array.from({length:7},(_,index)=>resolved("cutoff-order",
    `2026-08-25T10:09:0${index}.000Z`,{decision:"edit",extension:{index}}));
  const state=fold([request,first,second,...closer]);
  fold([knock("cutoff-order","2026-08-25T10:08:00.000Z")],state);
  assert.equal(state.requests.get("cutoff-order").resolution.payload.decision,"approve");
});

test("confirmed decision remains visible after activity that intervened before the close", () => {
  const request=knock("persistent");
  const activity={v:0,ts:"2026-08-25T10:01:00.000Z",source:"codex",
    agent_id:"codex:keeper",project:"life",type:"tool_called",payload:{tool:"Read"}};
  const close=resolved("persistent","2026-08-25T10:02:00.000Z",{decision:"edit"});
  const state=fold([request,activity,close]);
  const [villager]=projection.reduce([request,activity],Date.parse("2026-08-25T10:03:00.000Z"),[],state);
  assert.equal(villager.state,"resting");
  assert.equal(villager.approval.request_id,"persistent");
  assert.equal(villager.approval.resolution.payload.decision,"edit");
});

test("recent confirmations remain alongside a newer actionable queue and are bounded", () => {
  const events=[];
  for(let index=0;index<approvals.MAX_CONFIRMATIONS+2;index++) {
    const at=`2026-08-25T10:${String(index).padStart(2,"0")}:00.000Z`;
    events.push(knock(`closed-${index}`,at));
    events.push(resolved(`closed-${index}`,`2026-08-25T10:${String(index).padStart(2,"0")}:30.000Z`,
      {decision:index%2?"deny":"approve"}));
  }
  const pending=knock("still-pending","2026-08-25T10:20:00.000Z");
  events.push(pending);
  const state=fold(events);
  const [villager]=projection.reduce(events,Date.parse("2026-08-25T10:21:00.000Z"),[],state);
  assert.equal(villager.state,"knocking");
  assert.equal(villager.knock.request_id,"still-pending");
  assert.equal(villager.approvals.length,approvals.MAX_CONFIRMATIONS);
  assert.equal(villager.approvals[0].request_id,`closed-${approvals.MAX_CONFIRMATIONS+1}`);
  assert.deepEqual(approvals.recentConfirmations(state).map(item=>item.request_id),
    Array.from({length:approvals.MAX_CONFIRMATIONS},(_,offset)=>
      `closed-${approvals.MAX_CONFIRMATIONS+1-offset}`),
    "the shared global selector uses the same append-authoritative retention cap");
});

test("session terminal remains authoritative after an approval closes", () => {
  const request=knock("parked");
  const activity={v:0,ts:"2026-08-25T10:01:00.000Z",source:"codex",
    agent_id:"codex:keeper",project:"life",type:"tool_called",payload:{tool:"Read"}};
  const ended={v:0,ts:"2026-08-25T10:02:00.000Z",source:"codex",
    agent_id:"codex:keeper",project:"life",type:"session_ended",payload:{}};
  const close=resolved("parked","2026-08-25T10:03:00.000Z");
  let state=fold([request,activity,ended]);
  assert.equal(projection.reduce([request,activity,ended],Date.parse("2026-08-25T10:04:00.000Z"),[],state)[0].state,
    "knocking","documented pending doorstep survives a parked session");
  fold([close],state);
  assert.deepEqual(projection.reduce([request,activity,ended],
    Date.parse("2026-08-25T10:04:00.000Z"),[],state),[],
    "the close does not resurrect a session-ended visitor");
  assert.deepEqual(approvals.recentConfirmations(state).map(item=>({
    request_id:item.request_id,decision:item.resolution.payload.decision,
    agent_id:item.record.knock.agent_id})),[
    {request_id:"parked",decision:"approve",agent_id:"codex:keeper"}],
  "bounded global confirmation evidence survives without projecting a villager");
  const resident={valid:true,manifest_version:1,home:0,match:{agent_id:"codex:keeper"},
    meta:{name:"Keeper"}};
  assert.deepEqual(projection.reduce([request,activity,ended],
    Date.parse("2026-08-25T10:04:00.000Z"),[resident],state),[],
    "a static resident home does not override session terminal telemetry");
});

test("capacity omits whole old approvals instead of degrading them into unresolvable plain knocks", () => {
  const events=Array.from({length:approvals.MAX_REQUESTS+1},(_,index)=>({
    ...knock(`capacity-${index}`,new Date(Date.parse("2026-08-25T10:00:00.000Z")+index).toISOString(),
      {message:`Question ${index}`}),agent_id:`codex:capacity-${index}`}));
  const state=fold(events);
  const village=projection.reduce(events,Date.parse("2026-08-25T10:01:00.000Z"),[],state);
  assert.equal(state.requests.size,approvals.MAX_REQUESTS);
  assert.equal(state.capacityDropped,1);
  assert.equal(village.some(v=>v.knock && v.knock.request_id==="capacity-0"),false);
  assert.equal(village.some(v=>v.id==="codex:capacity-0"),false);
});

test("grouped lifecycle window retains bounded journal-predecessor truth in append order", () => {
  const request=knock("retained");
  const activity={...request,type:"tool_called",payload:{tool:"Read"}};
  const journal={...request,source:"steward",type:"journal_written",
    payload:{routine:"close-of-day",day:"2026-08-25",path:"/journal/2026-08-25.md"}};
  const chatter=Array.from({length:4000},(_,index)=>({...activity,
    agent_id:`codex:other-${index}`,payload:{tool:"Read",index}}));
  for (const suffix of [[], [resolved("retained")],
    [{...request,payload:{...request.payload,message:"Different immutable question"}}]]) {
    const raw=[request,activity,journal,...suffix,...chatter];
    const full=projection.parseEvents(raw);
    const selected=approvals.lifecycleWindow(full,4000,[{event:full[2]}],
      {isValidatedBatch:projection.isValidatedBatch,
        validatedSelection:projection.validatedSelection});
    assert.equal(selected.length,4000);
    assert.equal(new Set(selected).size,selected.length);
    assert.equal(selected.includes(request),true,"canonical request is retained");
    assert.deepEqual(selected,full.filter(event=>selected.includes(event)),
      "selection never changes authoritative append order");
    for(const event of suffix) assert.equal(selected.includes(event),true,
      "closing or collision evidence is retained with its request");
  }
});

test("acknowledgement requires an exact post-boundary closing event and reset is ambiguous", () => {
  let now=1000, timer;
  const acks=approvals.createAcknowledgements({timeoutMs:10,now:()=>now,
    setTimeout:fn=>{timer=fn;return 1;},clearTimeout(){}});
  const tracking=acks.request("r1","approve",null,cursor(10),
    expected("r1"));
  assert.equal(tracking.ok,true); acks.accepted("r1",{replay:false});
  acks.observe({cursor:cursor(10),events:[resolved("r1")]});
  assert.equal(acks.get("r1").state,"pending","boundary evidence is not later evidence");
  acks.observe({cursor:cursor(11),events:[resolved("other")]});
  assert.equal(acks.get("r1").state,"pending");
  acks.observe({cursor:cursor(11),events:[{...resolved("r1"),agent_id:"codex:other"}]});
  assert.equal(acks.get("r1").state,"pending","same id under another resident is not exact");
  const exact=resolved("r1");
  acks.observe({cursor:cursor(12),events:[exact],approvalState:fold([knock("r1"),exact])});
  assert.equal(acks.get("r1").state,"acknowledged");
  assert.ok(acks.get("r1").generationCandidates.size<=approvals.MAX_ACK_EVIDENCE);

  const second=acks.request("r2","deny",null,cursor(12),expected("r2"));
  assert.equal(second.ok,true); acks.accepted("r2",{replay:false});
  acks.observe({cursor:cursor(13),events:[],reset:true});
  assert.equal(acks.get("r2").state,"ambiguous");
  assert.equal(acks.request("r2","deny",null,cursor(13),expected("r2")).ok,false);
  assert.equal(typeof timer,"function");
});

test("acknowledgement requires the clicked immutable request shape to remain authoritative", () => {
  const acks=approvals.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
  acks.request("shape-ack","approve",null,cursor(1),expected("shape-ack"));
  acks.accepted("shape-ack",{replay:false});
  const original=knock("shape-ack"), rewritten=knock("shape-ack",
    "2026-08-25T10:00:01.000Z",{detail:{to:"attacker@example.com"},options:["approve"]});
  const close=resolved("shape-ack","2026-08-25T10:02:00.000Z");
  const state=fold([original,rewritten,close]);
  acks.observe({cursor:cursor(2),events:[close],approvalState:state});
  assert.equal(state.requests.get("shape-ack").collided,true);
  assert.equal(state.requests.get("shape-ack").resolution,null);
  assert.equal(acks.get("shape-ack").state,"pending",
    "a close cannot acknowledge a rewritten question or unoffered answer set");
});

test("timeouts and replay receipts prevent duplicate delivery; definitive refusal permits correction", () => {
  let now=10, callback;
  const acks=approvals.createAcknowledgements({timeoutMs:5,now:()=>now,
    setTimeout:fn=>{callback=fn;return 1;},clearTimeout(){}});
  acks.request("timeout","approve",null,cursor(1),expected("timeout")); acks.accepted("timeout",{replay:false});
  now=15; callback();
  assert.equal(acks.get("timeout").state,"timeout");
  assert.equal(acks.request("timeout","approve",null,cursor(2),expected("timeout")).ok,false);
  acks.request("replay","deny",null,cursor(2),expected("replay")); acks.accepted("replay",{replay:true});
  assert.equal(acks.get("replay").state,"indeterminate");
  assert.equal(acks.request("replay","deny",null,cursor(3),expected("replay")).ok,false);
  acks.request("auth","approve",null,cursor(3),expected("auth"));
  acks.failed("auth","Steward refused (401).",true);
  assert.equal(acks.get("auth").state,"failed");
  assert.equal(acks.request("auth","approve",null,cursor(3),expected("auth")).ok,true);
});

test("exact evidence cannot be downgraded by a later HTTP replay and tracking is bounded", () => {
  const acks=approvals.createAcknowledgements({maxEntries:2,setTimeout:()=>1,clearTimeout(){}});
  acks.request("closed","approve",null,cursor(1),expected("closed"));
  const close=resolved("closed");
  acks.observe({cursor:cursor(2),events:[close],approvalState:fold([knock("closed"),close])});
  assert.equal(acks.get("closed").state,"acknowledged");
  acks.accepted("closed",{replay:true});
  assert.equal(acks.get("closed").state,"acknowledged");
  acks.request("uncertain-1","approve",null,cursor(2),expected("uncertain-1"));
  acks.accepted("uncertain-1",{replay:true});
  // The acknowledged entry yields to another request, but two unresolved
  // entries cannot be silently evicted to send a third decision.
  assert.equal(acks.request("uncertain-2","deny",null,cursor(3),expected("uncertain-2")).ok,true);
  acks.accepted("uncertain-2",{replay:true});
  assert.equal(acks.request("overflow","approve",null,cursor(4),expected("overflow")).ok,false);
});

test("reset revalidates exact lifecycle and close fingerprint before retaining acknowledgement", () => {
  const acks=approvals.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
  const request=knock("mirror"), close=resolved("mirror");
  acks.request("mirror","approve",null,cursor(1),expected("mirror"));
  acks.accepted("mirror",{replay:false});
  acks.observe({cursor:cursor(2),events:[close],approvalState:fold([request,close])});
  assert.equal(acks.get("mirror").state,"acknowledged");

  acks.observe({cursor:cursor(20,RESET_BOOT),events:[],reset:true,
    approvalState:fold([request,close])});
  assert.equal(acks.get("mirror").state,"acknowledged","same exact close survives replay");

  for (const [name,replay] of [
    ["pending",fold([request])], ["missing",fold([])],
    ["collided",fold([request,knock("mirror",undefined,{detail:{to:"other"}}),close])],
    ["different",fold([request,resolved("mirror",undefined,{decision:"deny"})])],
  ]) {
    acks.observe({cursor:cursor(30,RESET_BOOT),events:[],reset:true,approvalState:replay});
    assert.equal(acks.get("mirror").state,"indeterminate",name);
    assert.equal(acks.request("mirror","approve",null,cursor(30,RESET_BOOT),expected("mirror")).ok,
      false,`${name} replay blocks a duplicate`);
  }

  const recovered=fold([request,close]);
  acks.observe({cursor:cursor(31,RESET_BOOT),events:[close],approvalState:recovered});
  assert.equal(acks.get("mirror").state,"acknowledged","later exact close recovers truth");
  assert.equal(acks.get("mirror").event,close);
});

test("direct decision POST encodes identity, bearer auth, edit free text, and receipt semantics", async () => {
  const calls=[];
  const fetchImpl=async (url,options)=>{calls.push({url,options});return {status:202,
    json:async()=>({request_id:"a/b",status:"recorded",decision:"edit"})};};
  const receipt=await approvals.decide({url:"http://steward.local/",token:"secret"},
    "a/b","edit","shorter subject",fetchImpl,{setTimeout:()=>1,clearTimeout(){},timeoutMs:10});
  assert.deepEqual(receipt,{request_id:"a/b",decision:"edit",replay:false});
  assert.equal(calls[0].url,"http://steward.local/approvals/a%2Fb");
  assert.equal(calls[0].options.headers.Authorization,"Bearer secret");
  assert.deepEqual(JSON.parse(calls[0].options.body),
    {decision:"edit",edit:{note:"shorter subject"}});
});

test("HTTP refusal classification distinguishes no-write correction from ambiguous delivery", async () => {
  for (const [status,definitive] of [[401,true],[404,true],[422,true],[400,false],[409,false],[500,false]]) {
    await assert.rejects(approvals.decide({url:"http://steward",token:"x"},"r","approve","",
      async()=>({status,json:async()=>({})}),{setTimeout:()=>1,clearTimeout(){},timeoutMs:10}),
    error=>error.definitive===definitive && (status!==401 || error.authRejected===true));
  }
});

test("only Steward's exact parsed approval_expired envelope makes a 409 retry-safe", async () => {
  const expired={detail:{error:"approval_expired",message:"expired and denies by default"}};
  await assert.rejects(approvals.decide({url:"http://steward",token:"x"},"r","approve","",
    async()=>({status:409,json:async()=>expired}),{setTimeout:()=>1,clearTimeout(){},timeoutMs:10}),
  error=>error.definitive===true && /expired/.test(error.message));
  for (const body of [{error:"approval_expired"},{detail:{error:"other",message:"no"}},
    {detail:{error:"approval_expired",message:"expired",extra:true}},
    {detail:{error:"approval_expired"}},null]) {
    await assert.rejects(approvals.decide({url:"http://steward",token:"x"},"r","approve","",
      async()=>({status:409,json:async()=>body}),{setTimeout:()=>1,clearTimeout(){},timeoutMs:10}),
    error=>error.definitive===false);
  }
  await assert.rejects(approvals.decide({url:"http://steward",token:"x"},"r","approve","",
    async()=>({status:409,json:async()=>{throw new Error("proxy html");}}),
    {setTimeout:()=>1,clearTimeout(){},timeoutMs:10}),error=>error.definitive===false);
});

test("a 200 replay truthfully returns the first decision even when this click differed", async () => {
  const receipt=await approvals.decide({url:"http://steward",token:"x"},"r","deny","",
    async()=>({status:200,json:async()=>({request_id:"r",status:"recorded",decision:"approve"})}),
    {setTimeout:()=>1,clearTimeout(){},timeoutMs:10});
  assert.deepEqual(receipt,{request_id:"r",decision:"approve",replay:true});
});
