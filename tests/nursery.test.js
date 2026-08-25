"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const nursery = require("../viewer/nursery.js");
const projection = require("../viewer/projection.js");

const draft = (overrides = {}) => ({ name:"Quill Keeper", char:"Monk", accent:"#4f7ea6",
  role:"note keeper", mission:"Keep notes.", duties:"Tidy notes\nIndex artifacts",
  rules:"Never delete", escalation:"Ask before irreversible work", skills:"research, write-journal",
  runner:"codex", ...overrides });
const event = (agent_id="codex:quill-keeper",source="codex") => ({
  v:0,ts:"2026-08-25T10:00:00.000Z",source,agent_id,
  project:"notes",type:"task_started",payload:{prompt:"Wake"}});
const stewardEvent = (type,agent_id="codex:quill-keeper") => ({
  v:0,ts:"2026-08-25T10:00:00.000Z",source:"steward",agent_id,project:"notes",type,
  payload:type==="routine_started"?{routine:"summary",run_id:"wake",trigger:"manual"}:
    type==="task_claimed"?{task_id:"wake",title:"Wake",claimant:agent_id}:
    type==="needs_human_resolved"?{request_id:"wake",decision:"approve",
      decided_by:"api",action:"wake"}:{}
});
const valid = () => null;
const cursor = offset => `v1:0123456789abcdef0123456789abcdef:1:2:3:${offset}`;
const resetCursor = offset => `v1:fedcba9876543210fedcba9876543210:4:5:6:${offset}`;
const receipt = (overrides = {}) => ({request_id:"req",agent_id:"codex:quill-keeper",
  changed:true,declaration_written:true,register_ok:true,register_problems:[],
  message:"accepted",truncated:false,...overrides});

test("local validation uses the documented Burrow sprite set and safe declaration fields", () => {
  assert.strictEqual(nursery.CHARS, projection.CHARS,
    "nursery and renderer consume the exact same sprite authority");
  assert.equal(Object.isFrozen(nursery.CHARS), true);
  assert.equal(nursery.validate(draft()).ok, true);
  const bad = nursery.validate(draft({ name:" ", char:"Scribe", accent:"red", duties:"", runner:"mock" }));
  assert.equal(bad.ok, false);
  assert.deepEqual(Object.keys(bad.errors).sort(), ["accent","char","duties","name","runner"]);
  const body = nursery.requestBody(draft());
  assert.deepEqual(body, { id:"quill-keeper", name:"Quill Keeper", char:"Monk", accent:"#4f7ea6",
    role:"note keeper", charter:{mission:"Keep notes.",duties:["Tidy notes","Index artifacts"],
      rules:["Never delete"],escalation:"Ask before irreversible work"},
    skills:["research","write-journal"], runner:{kind:"codex"}, deploy:true });
});

test("create posts the exact current Steward contract directly with bearer auth", async () => {
  let call;
  const body = nursery.immutableBody(draft());
  const result = await nursery.createResident({url:"http://steward:8801/",token:"secret"}, body,
    async (url, options) => { call={url,options}; return {status:201,async json(){return {
      status:"accepted",request_id:"req-1",id:"quill-keeper",changed:true,
      message:"accepted",declare:{written:true},register:{ok:true,problems:[]}};}}; });
  assert.equal(call.url, "http://steward:8801/residents");
  assert.equal(call.options.headers.Authorization, "Bearer secret");
  assert.deepEqual(JSON.parse(call.options.body), body);
  assert.equal(result.agent_id, "codex:quill-keeper");
});

test("Steward rejection preserves its message and is safely bounded", async () => {
  const remote = "duplicate resident <script>";
  await assert.rejects(nursery.createResident({url:"http://steward",token:"x"}, nursery.immutableBody(draft()),
    async () => ({status:409,async json(){return {detail:{error:"resident_not_declared",message:remote}};}})),
    error => error.message === remote && error.code === "resident_not_declared" && error.definitive);
  const huge = "x".repeat(nursery.MAX_REMOTE_ERROR + 10);
  await assert.rejects(nursery.createResident({url:"http://steward",token:"x"}, nursery.immutableBody(draft()),
    async () => ({status:422,async json(){return {detail:{message:huge}};}})),
    error => error.message === huge.slice(0,nursery.MAX_REMOTE_ERROR) && error.truncated);
});

test("non-definitive HTTP responses report unknown creation truth and retain exact-body retry", async () => {
  for (const response of [
    {status:503,async json(){return {detail:{error:"overloaded",message:"try later"}};}},
    {status:503,async json(){throw new SyntaxError("broken JSON");}},
    {status:200,async json(){return {status:"unexpected"};}},
  ]) {
    const body=nursery.immutableBody(draft());
    let failure;
    try {
      await nursery.createResident({url:"http://steward",token:"x"},body,async()=>response);
    } catch (error) { failure=error; }
    assert.equal(failure && failure.definitive,false);
    assert.equal(failure && failure.kind,"ambiguous");
    assert.doesNotMatch(failure.message,/Steward rejected/i);
    assert.match(failure.message,/may have been created/);
    assert.match(failure.message,/outcome is unknown/);
    assert.match(failure.message,/exact original declaration/);

    const tracker=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
    const item=tracker.begin(cursor(1),draft()).item;
    tracker.failed(item.key,failure);
    const retry=tracker.retry(item.key,cursor(2));
    assert.equal(retry.ok,true);
    assert.strictEqual(retry.item.body,item.body,
      "an uncertain HTTP response can retry only the frozen original object");
  }
});

test("FastAPI validation arrays and exact conflicts are rendered verbatim within the safety bound", async () => {
  const body=nursery.immutableBody(draft());
  const detail=[{type:"extra_forbidden",loc:["body","api_key"],msg:"Extra inputs are not permitted",input:"<secret>"}];
  await assert.rejects(nursery.createResident({url:"http://steward",token:"x"},body,
    async()=>({status:422,async json(){return {detail};}})), error =>
      error.message === JSON.stringify(detail) && error.kind === "validation" && error.definitive);
  const conflict="resident exists with a different soul and charter";
  await assert.rejects(nursery.createResident({url:"http://steward",token:"x"},body,
    async()=>({status:409,async json(){return {detail:{error:"resident_not_declared",message:conflict}};}})),
    error=>error.message===conflict && error.code==="resident_not_declared" && error.kind==="rejected");
});

test("the actual shared deadline adapter classifies its AbortError as a truthful timeout", async () => {
  const body=nursery.immutableBody(draft());
  await assert.rejects(nursery.createResident({url:"http://steward",token:"x"},body,
    async(_url,{signal})=>new Promise((resolve,reject)=>signal.addEventListener("abort",()=>{
      const error=new Error("fetch aborted");error.name="AbortError";reject(error);
    },{once:true})),{timeoutMs:1}), error=>error.kind==="timeout" &&
      error.message.startsWith("Steward timed out.") && !error.message.includes("unreachable"));
});

test("the actual 201 register failure preserves schedule details without claiming acceptance", async () => {
  const body=nursery.immutableBody(draft());
  const result=await nursery.createResident({url:"http://steward",token:"x"},body,
    async()=>({status:201,async json(){return {status:"accepted",request_id:"req-broken",
      id:"quill-keeper",changed:true,declare:{written:true},message:
      "the container is up, but the schedule check did not pass — see register.problems",
      register:{ok:false,problems:["runner binary not found: codex"]}};}}));
  assert.equal(result.register_ok,false);
  assert.deepEqual(result.register_problems,["runner binary not found: codex"]);
  assert.match(result.message,/register\.problems: \["runner binary not found: codex"\]/);
  const tracker=nursery.createTracker({setTimeout:()=>{throw new Error("no pending timer");},clearTimeout:()=>{}});
  const item=tracker.begin(cursor(1),draft()).item; tracker.accepted(item.key,result);
  assert.equal(item.state,"deployment-failed"); assert.equal(tracker.blocks(),false);
});

test("begin captures immutable runner identity and the exact request body before HTTP", () => {
  const mutable=draft(); const tracker=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
  const item=tracker.begin(cursor(10),mutable).item;
  mutable.name="Edited Later"; mutable.runner="claude"; mutable.rules="Changed";
  assert.equal(item.agent_id,"codex:quill-keeper");
  assert.equal(item.runner_source,"codex");
  assert.equal(item.body.name,"Quill Keeper");
  assert.deepEqual(item.body.charter.rules,["Never delete"]);
  assert.equal(Object.isFrozen(item.body),true);
  assert.equal(Object.isFrozen(item.body.charter.rules),true);
});

test("wake evidence requires both immutable resident identity and immutable runner source", () => {
  const tracker=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
  const codex=tracker.begin(cursor(10),draft()).item;
  assert.equal(codex.runner_source,"codex");
  tracker.observe({cursor:cursor(11),events:[event(codex.agent_id,"steward")]},valid);
  assert.equal(codex.candidates.size,0,"matching Steward lifecycle evidence is not retained");
  tracker.accepted(codex.key,receipt());
  assert.equal(codex.state,"pending");
  tracker.observe({cursor:cursor(12),events:[event(codex.agent_id,"claude-code")]},valid);
  assert.equal(codex.state,"pending","another runner source cannot impersonate the requested runner");
  tracker.observe({cursor:cursor(13),events:[event(codex.agent_id,"codex ")]},valid);
  assert.equal(codex.state,"pending","runner source matching is exact and never normalized");
  tracker.observe({cursor:cursor(14),events:[event(codex.agent_id,"codex")]},valid);
  assert.equal(codex.state,"alive");

  const claude=tracker.begin(cursor(20),draft({name:"Claude Keeper",runner:"claude"})).item;
  assert.equal(claude.agent_id,"claude-code:claude-keeper");
  assert.equal(claude.runner_source,"claude-code");
  tracker.accepted(claude.key,receipt({agent_id:claude.agent_id}));
  tracker.observe({cursor:cursor(21),events:[event(claude.agent_id,"claude")]},valid);
  assert.equal(claude.state,"pending");
  tracker.observe({cursor:cursor(22),events:[event(claude.agent_id,"claude-code")]},valid);
  assert.equal(claude.state,"alive");
});

test("matching Steward evidence never reconciles any nursery wake state", () => {
  const cases=[["requesting","routine_started"],["pending","task_claimed"],
    ["silent","needs_human_resolved"],["ambiguous","session_ended"],
    ["timeout","routine_started"],["unreachable","task_claimed"],
    ["cancelled","needs_human_resolved"]];
  for(const [state,wrongType] of cases){
    let timer; const tracker=nursery.createTracker({waitMs:1,
      setTimeout:fn=>{timer=fn;return 1;},clearTimeout:()=>{}});
    const item=tracker.begin(cursor(30),draft()).item;
    if(state==="pending" || state==="silent") tracker.accepted(item.key,receipt());
    if(state==="silent") timer();
    if(["ambiguous","timeout","unreachable","cancelled"].includes(state)) {
      tracker.failed(item.key,Object.assign(new Error(state),{kind:state,definitive:false}));
    }
    assert.equal(item.state,state);
    const wrong=stewardEvent(wrongType,item.agent_id);
    assert.equal(projection.validateEvent(wrong),null,
      `${wrongType} is valid protocol evidence, just not runner-authored wake evidence`);
    tracker.observe({cursor:cursor(31),events:[wrong]},valid);
    assert.equal(item.state,state,`${state} ignores a matching Steward-authored record`);
    assert.equal(item.candidates.size,0,`${state} does not retain wrong-source evidence`);
    tracker.observe({cursor:cursor(32),events:[event(item.agent_id,item.runner_source)]},valid);
    if(state==="requesting") {
      assert.equal(item.state,"requesting","requesting retains exact runner evidence until HTTP settles");
      tracker.accepted(item.key,receipt());
    }
    assert.equal(item.state,"alive",`${state} reconciles from the subsequent exact runner event`);
  }
});

test("pending starts only after valid acceptance and clears on exact first post-boundary event", () => {
  let at=1000; const tracker=nursery.createTracker({now:()=>at,setTimeout:()=>1,clearTimeout:()=>{}});
  const tracking=tracker.begin(cursor(10),draft());
  tracker.observe({cursor:cursor(11),events:[event("codex:someone-else")]},valid);
  tracker.accepted(tracking.item.key,receipt());
  assert.equal(tracker.latest().state,"pending");
  tracker.observe({cursor:cursor(12),events:[event()]},valid);
  assert.equal(tracker.latest().state,"alive");
  assert.equal(tracker.latest().event.agent_id,"codex:quill-keeper");
});

test("agent evidence uses exact wire identity without trimming", () => {
  const tracker=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
  const item=tracker.begin(cursor(10),draft()).item; tracker.accepted(item.key,receipt());
  tracker.observe({cursor:cursor(11),events:[event(" codex:quill-keeper ")]},valid);
  assert.equal(item.state,"pending");
  tracker.observe({cursor:cursor(12),events:[event()]},valid);
  assert.equal(item.state,"alive");
});

test("definitive failure permits safe resubmit and never creates pending", () => {
  const tracker=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
  const first=tracker.begin(cursor(10),draft()).item;
  tracker.failed(first.key,Object.assign(new Error("resident already exists"),{kind:"rejected",definitive:true}));
  assert.equal(tracker.latest().state,"rejected");
  assert.equal(tracker.blocks(),false);
  assert.equal(tracker.begin(cursor(10),draft({name:"Another"})).ok,true);
});

test("unreachable and timeout outcomes stay ambiguous and block phantom resubmission", () => {
  const tracker=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
  const first=tracker.begin(cursor(10),draft()).item;
  tracker.failed(first.key,Object.assign(new Error("unreachable"),{kind:"unreachable",definitive:false}));
  assert.equal(tracker.latest().state,"unreachable");
  assert.equal(tracker.begin(cursor(11),draft()).ok,false);
});

test("exact post-boundary evidence reconciles ambiguous transport but never a definitive refusal", () => {
  for (const kind of ["unreachable","timeout","cancelled","ambiguous"]) {
    const tracker=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
    const first=tracker.begin(cursor(10),draft()).item;
    tracker.observe({cursor:cursor(11),events:[event()]},valid);
    tracker.failed(first.key,Object.assign(new Error("lost response"),{kind,definitive:false}));
    assert.equal(first.state,"alive",`${kind} reconciles from retained exact evidence`);
  }
  const tracker=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
  const second=tracker.begin(cursor(20),draft({name:"Other",runner:"claude"})).item;
  const other=event("claude-code:other");
  tracker.observe({cursor:cursor(21),events:[other]},valid);
  tracker.failed(second.key,Object.assign(new Error("conflict"),{kind:"rejected",definitive:true}));
  assert.equal(second.state,"rejected");
  tracker.observe({cursor:cursor(22),events:[other]},valid);
  assert.equal(second.state,"rejected");
});

test("converged acceptance and failed deployment never create a pending or phantom villager", () => {
  const tracker=nursery.createTracker({setTimeout:()=>{throw new Error("no timer expected");},clearTimeout:()=>{}});
  const converged=tracker.begin(cursor(1),draft()).item;
  tracker.accepted(converged.key,receipt({changed:false,declaration_written:false}));
  assert.equal(converged.state,"converged"); assert.equal(tracker.blocks(),false);
  const provisioned=tracker.begin(cursor(2),draft({name:"Provisioned Keeper"})).item;
  tracker.accepted(provisioned.key,receipt({changed:true,declaration_written:false}));
  assert.equal(provisioned.state,"converged",
    "declare.written false is authoritative even when provisioning changed aggregate state");
  assert.equal(tracker.blocks(),false);
  const failed=tracker.begin(cursor(3),draft({name:"Deploy Failure"})).item;
  tracker.accepted(failed.key,receipt({agent_id:"codex:deploy-failure",register_ok:false,
    message:'container is up; register.problems: ["runner binary not found: codex"]',
    register_problems:["runner binary not found: codex"]}));
  assert.equal(failed.state,"deployment-failed"); assert.match(failed.message,/runner binary not found/);
  assert.equal(tracker.blocks(),false);
});

test("created-but-never-seen remains reconcilable only by exact post-boundary evidence", () => {
  let timer,at=0; const tracker=nursery.createTracker({now:()=>at,waitMs:600000,
    setTimeout:fn=>{timer=fn;return 1;},clearTimeout:()=>{}});
  const item=tracker.begin(cursor(1),draft()).item;
  tracker.accepted(item.key,receipt());
  at=600000; timer();
  assert.equal(tracker.latest().state,"silent");
  assert.match(tracker.latest().message,/created but never seen within 10 minutes/);
  tracker.observe({cursor:cursor(1),events:[event()]},valid);
  assert.equal(item.state,"silent","evidence at the request boundary is retained replay");
  tracker.observe({cursor:cursor(2),events:[event("codex:someone-else")]},valid);
  assert.equal(item.state,"silent","another resident cannot reconcile this declaration");
  tracker.observe({cursor:cursor(3),events:[event()]},valid);
  assert.equal(item.state,"alive");
});

test("created-but-never-seen reconciles after rotation without accepting reset replay", () => {
  let timer; const tracker=nursery.createTracker({waitMs:1,
    setTimeout:fn=>{timer=fn;return 1;},clearTimeout:()=>{}});
  const item=tracker.begin(cursor(10),draft()).item;
  tracker.accepted(item.key,receipt()); timer();
  assert.equal(item.state,"silent");
  tracker.observe({reset:true,cursor:resetCursor(20),events:[event()]},valid);
  assert.equal(item.state,"silent","history replayed by reset is not new wake evidence");
  tracker.observe({cursor:resetCursor(21),events:[event("codex:someone-else")]},valid);
  assert.equal(item.state,"silent");
  tracker.observe({cursor:resetCursor(22),events:[event()]},valid);
  assert.equal(item.state,"alive");
});

test("reset does not turn replayed history into a first event and request-time reset is ambiguous", () => {
  const pending=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
  const accepted=pending.begin(cursor(4),draft()).item;
  pending.accepted(accepted.key,receipt());
  pending.observe({reset:true,cursor:resetCursor(7),events:[event()]},valid);
  assert.equal(pending.latest().state,"pending");
  pending.observe({cursor:resetCursor(8),events:[event()]},valid);
  assert.equal(pending.latest().state,"alive","only evidence after the reset-ending cursor wakes it");
  const requesting=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
  const inFlight=requesting.begin(cursor(4),draft()).item;
  assert.equal(requesting.busy(),true);
  requesting.observe({reset:true,cursor:resetCursor(7),events:[event()]},valid);
  assert.equal(requesting.latest().state,"ambiguous");
  assert.equal(requesting.busy(),true,"HTTP remains busy after reset changes lifecycle prose");
  assert.equal(requesting.retry(inFlight.key,resetCursor(8)).ok,false,
    "reset cannot overlap the still-running HTTP attempt");
  requesting.accepted(inFlight.key,receipt());
  assert.equal(inFlight.state,"ambiguous","a late acceptance cannot restore the stale boundary");
  assert.equal(requesting.busy(),false);
  requesting.observe({cursor:resetCursor(8),events:[event(inFlight.agent_id,"steward")]},valid);
  assert.equal(inFlight.state,"ambiguous","matching Steward evidence after reset is not a wake");
  requesting.observe({cursor:resetCursor(9),events:[event()]},valid);
  assert.equal(inFlight.state,"alive");

  const invalid=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
  const blinded=invalid.begin(cursor(1),draft()).item; invalid.accepted(blinded.key,receipt());
  invalid.observe({reset:true,cursor:"not-a-cursor",events:[event()]},valid);
  assert.equal(blinded.state,"ambiguous","an invalid reset authority is explicit, never stale pending");
});

test("ambiguous retry uses a fresh cursor and only the immutable original body", () => {
  const tracker=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}}), mutable=draft();
  const item=tracker.begin(cursor(3),mutable).item;
  tracker.failed(item.key,Object.assign(new Error("lost response"),{kind:"timeout",definitive:false}));
  mutable.name="Malicious Edit"; mutable.role="changed";
  const retry=tracker.retry(item.key,resetCursor(20));
  assert.equal(retry.ok,true); assert.equal(item.attempt,2);
  assert.equal(item.body.name,"Quill Keeper"); assert.equal(item.body.role,"note keeper");
  tracker.observe({reset:true,cursor:resetCursor(21),events:[event()]},valid);
  assert.equal(item.state,"ambiguous");
  tracker.accepted(item.key,receipt({changed:false,declaration_written:false}));
  assert.equal(item.state,"converged","a lost response followed by idempotent convergence is not pending");
  assert.equal(tracker.blocks(),false);

  const conflict=tracker.begin(resetCursor(30),draft({name:"Conflict Keeper"})).item;
  tracker.failed(conflict.key,Object.assign(new Error("lost response"),{kind:"unreachable",definitive:false}));
  assert.equal(tracker.retry(conflict.key,resetCursor(31)).ok,true);
  tracker.failed(conflict.key,Object.assign(new Error("resident differs exactly"),{
    kind:"rejected",code:"resident_not_declared",definitive:true,remote:true}));
  assert.equal(conflict.state,"rejected"); assert.equal(conflict.message,"resident differs exactly");
  assert.equal(tracker.blocks(),false,"a retry conflict is definitive and never becomes pending");
});

test("capacity evicts the oldest terminal entry but retains an older silent declaration", () => {
  let firstTimer;
  const tracker=nursery.createTracker({waitMs:1,
    setTimeout:fn=>{firstTimer ||= fn;return 1;},clearTimeout:()=>{}});
  const silent=tracker.begin(cursor(1),draft({name:"Silent Keeper"})).item;
  tracker.accepted(silent.key,receipt({agent_id:silent.agent_id})); firstTimer();
  assert.equal(silent.state,"silent");
  const terminal=[];
  for(let index=1;index<nursery.MAX_TRACKED;index++){
    const item=tracker.begin(cursor(index+1),draft({name:`Rejected Keeper ${index}`})).item;
    tracker.failed(item.key,Object.assign(new Error(`refused ${index}`),
      {kind:"rejected",definitive:true}));
    terminal.push(item);
  }
  const replacement=tracker.begin(cursor(100),draft({name:"Replacement Keeper"}));
  assert.equal(replacement.ok,true);
  assert.equal(tracker.entries.size,nursery.MAX_TRACKED);
  assert.equal(tracker.entries.has(silent.key),true,
    "the oldest record survives because exact late evidence can still reconcile it");
  assert.equal(tracker.entries.has(terminal[0].key),false,
    "the oldest truly terminal record frees capacity deterministically");
  assert.equal(tracker.entries.has(terminal[1].key),true);
});

test("all-reconcilable capacity refuses a ninth declaration without mutation and frees after evidence", () => {
  const timers=[];
  const tracker=nursery.createTracker({waitMs:1,
    setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{}});
  const silent=[];
  for(let index=0;index<nursery.MAX_TRACKED;index++){
    const item=tracker.begin(cursor(index+1),draft({name:`Silent Keeper ${index}`})).item;
    tracker.accepted(item.key,receipt({agent_id:item.agent_id})); timers[index]();
    assert.equal(item.state,"silent"); silent.push(item);
  }
  const keys=[...tracker.entries.keys()];
  const refused=tracker.begin(cursor(100),draft({name:"Ninth Keeper"}));
  assert.deepEqual(refused,{ok:false,reason:"capacity",
    message:`Nursery history is full: all ${nursery.MAX_TRACKED} tracked declarations still await exact wake evidence; no request was sent.`});
  assert.deepEqual([...tracker.entries.keys()],keys,"capacity refusal creates no phantom tracker item");

  tracker.observe({cursor:cursor(101),events:[event(silent[0].agent_id,silent[0].runner_source)]},valid);
  assert.equal(silent[0].state,"alive","immutable late evidence still reconciles the retained oldest item");
  const admitted=tracker.begin(cursor(102),draft({name:"Ninth Keeper"}));
  assert.equal(admitted.ok,true);
  assert.equal(admitted.item.key,`nursery-${nursery.MAX_TRACKED+1}`,
    "a refused begin does not consume tracker sequence state");
  assert.equal(tracker.entries.has(silent[0].key),false,
    "the newly terminal oldest item is the deterministic capacity victim");
  assert.deepEqual([...tracker.entries.keys()].slice(0,2),[silent[1].key,silent[2].key]);
  assert.equal(tracker.entries.size,nursery.MAX_TRACKED);
});

test("tracking stays bounded without evicting an unresolved request", () => {
  const tracker=nursery.createTracker({setTimeout:()=>1,clearTimeout:()=>{}});
  for(let index=0;index<nursery.MAX_TRACKED+4;index++){
    const item=tracker.begin(cursor(index+1),draft({name:`Keeper ${index}`})).item;
    tracker.failed(item.key,Object.assign(new Error(`refused ${index}`),{kind:"rejected",definitive:true}));
  }
  assert.equal(tracker.entries.size,nursery.MAX_TRACKED);
  const active=tracker.begin(cursor(100),draft({name:"Active Keeper"})).item;
  assert.equal(tracker.begin(cursor(101),draft({name:"Unsafe Duplicate"})).ok,false);
  assert.equal(tracker.entries.has(active.key),true);
});
