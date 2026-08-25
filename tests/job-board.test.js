"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const jobs = require("../viewer/job-board.js");
const projection = require("../viewer/projection.js");
const { validateEvent } = projection;

const lines = fs.readFileSync("tests/fixtures/jobs.jsonl", "utf8").trim().split("\n");
const event = (type, id, ts, payload = {}) => ({
  v:0, ts, source:"steward", agent_id:type === "task_posted" ? "steward:api" : "claude-code:one",
  project:"burrow", type, payload:{task_id:id,title:`Task ${id}`,
    ...(type === "task_posted" ? {required_skills:[],posted_by:"api"} : {claimant:"claude-code:one"}),
    ...(type === "task_done" ? {artifacts:[]} : {}),
    ...(type === "task_failed" ? {reason:"failed"} : {}), ...payload},
});

test("fixture projection is keyed, newest-first, truthful and diagnostic", () => {
  const state = jobs.createState();
  jobs.fold(state, lines, {validateEvent});
  const rows = jobs.rows(state, Date.parse("2026-08-25T09:10:00.000Z"));
  assert.deepEqual(rows.map(row => [row.id,row.state,row.title,row.required_skills]), [
    ["blank-skills","open","Name the missing skills",["","  ","research"]],
    ["reopened","open","Archive old notes",[]],
    ["newer","claimed","Write the field note",["write-journal"]],
    ["orphan-claim","claimed","Claim without retained post",null],
    ["failed","failed","Unrecoverable errand",[]],
    ["older","done","Index the library",["research"]],
    ["orphan-done","done","Done without retained post",null],
  ]);
  assert.equal(rows[1].claimant, null);
  assert.equal(rows[1].previous_claimant, "codex:archivist");
  assert.equal(rows[1].reason, "lease_expired");
  assert.equal(rows[2].claimant, "claude-code:absent");
  assert.equal(rows[3].claimant, "codex:orphan");
  assert.equal(rows[3].posted_at, null,
    "a transition without retained task_posted evidence has no invented post time");
  assert.equal(rows[4].reason, "session_failed");
  assert.equal(rows[6].posted_at, null);
  assert.equal(state.tasks.size, 7, "duplicates and terminal transitions stay keyed by task");
  assert.equal(state.malformed, 4);
  assert.deepEqual(jobs.rows(state, Date.parse("2026-08-25T09:25:00.001Z")).map(row => row.id),
    ["blank-skills","reopened","newer","orphan-claim"],
    "done and ordinary failures expire while lease-expired and claimed work stays open");
});

test("known post chronology and unknown transition chronology remain distinct", () => {
  const state = jobs.createState();
  jobs.fold(state, [
    event("task_claimed", "unknown-older", "2026-08-25T10:04:00.000Z"),
    event("task_posted", "known-older", "2026-08-25T10:01:00.000Z"),
    event("task_claimed", "unknown-newer", "2026-08-25T10:05:00.000Z"),
    event("task_posted", "known-newer", "2026-08-25T10:02:00.000Z"),
  ], {validateEvent});
  const rows = jobs.rows(state, Date.parse("2026-08-25T10:06:00.000Z"));
  assert.deepEqual(rows.map(row => row.id),
    ["known-newer", "known-older", "unknown-newer", "unknown-older"]);
  assert.deepEqual(rows.map(row => row.posted_at), [
    Date.parse("2026-08-25T10:02:00.000Z"),
    Date.parse("2026-08-25T10:01:00.000Z"), null, null,
  ]);
  assert.deepEqual(rows.slice(2).map(row => row.updated_at), [
    Date.parse("2026-08-25T10:05:00.000Z"),
    Date.parse("2026-08-25T10:04:00.000Z"),
  ]);
});

test("trusted fold accepts only the shared strict batch while raw fold stays fail closed", () => {
  const valid = event("task_posted", "trusted", "2026-08-25T10:00:00.000Z");
  const raw = jobs.createState();
  jobs.fold(raw, [valid]);
  assert.equal(raw.tasks.size, 0);
  assert.equal(raw.malformed, 1, "unvalidated public input cannot enter the board");

  const batch = projection.parseEvents([valid]);
  const trusted = jobs.createState();
  jobs.foldValidated(trusted, batch, {isValidatedBatch:projection.isValidatedBatch});
  assert.deepEqual(jobs.rows(trusted).map(row => row.id), ["trusted"]);
  assert.throws(() => jobs.foldValidated(jobs.createState(), [valid],
    {isValidatedBatch:projection.isValidatedBatch}), /shared strict validated batch/);
});

test("latest timestamp wins and equal-time semantic order survives exact replay", () => {
  const posted = event("task_posted", "one", "2026-08-25T10:00:00.000Z", {required_skills:["research"]});
  const claimed = event("task_claimed", "one", "2026-08-25T10:01:00.000Z");
  const done = event("task_done", "one", "2026-08-25T10:02:00.000Z");
  const state = jobs.createState();
  jobs.fold(state, [done, posted, claimed, done, posted], {validateEvent});
  assert.equal(jobs.rows(state, Date.parse("2026-08-25T10:03:00.000Z"))[0].state, "done");
  const tied = jobs.createState();
  jobs.fold(tied, [event("task_claimed", "tie", posted.ts),
    event("task_done", "tie", posted.ts), event("task_posted", "tie", posted.ts)], {validateEvent});
  assert.equal(jobs.rows(tied, Date.parse("2026-08-25T10:03:00.000Z"))[0].state, "done",
    "same-ms facts converge by bounded semantic order, not delivery grouping");
  const failed = event("task_failed", "one", "2026-08-25T10:04:00.000Z",
    {reason:"lease_expired"});
  jobs.fold(state, [failed], {validateEvent});
  const reopened = jobs.rows(state, Date.parse("2026-08-25T10:05:00.000Z"))[0];
  assert.equal(reopened.state, "open", "an expired lease returns to Steward's open queue");
  assert.equal(reopened.claimant, null);
  assert.equal(reopened.previous_claimant, "claude-code:one");
  assert.equal(reopened.reason, "lease_expired");
  jobs.fold(state, [event("task_failed", "one", "2026-08-25T10:06:00.000Z",
    {reason:"session_failed"})], {validateEvent});
  assert.equal(jobs.rows(state, Date.parse("2026-08-25T10:07:00.000Z"))[0].state, "failed",
    "ordinary failures remain terminal during their bounded recency window");
});

test("same-millisecond lease expiry then re-claim stays claimed across replay", () => {
  const timestamp = "2026-08-25T10:01:00.000Z";
  const post = event("task_posted", "reclaim", "2026-08-25T10:00:00.000Z");
  const expiry = event("task_failed", "reclaim", timestamp, {reason:"lease_expired"});
  const reclaim = event("task_claimed", "reclaim", timestamp,
    {claimant:"codex:new-holder"});
  reclaim.agent_id = "codex:new-holder";
  const state = jobs.createState();
  jobs.fold(state, [post, expiry, reclaim, expiry], {validateEvent});
  let [row] = jobs.rows(state, Date.parse("2026-08-25T10:02:00.000Z"));
  assert.equal(row.state, "claimed");
  assert.equal(row.claimant, "codex:new-holder");

  jobs.fold(state, [event("task_claimed", "reclaim", "2026-08-25T09:59:59.999Z")],
    {validateEvent});
  [row] = jobs.rows(state, Date.parse("2026-08-25T10:02:00.000Z"));
  assert.equal(row.claimant, "codex:new-holder", "older out-of-order evidence cannot regress state");

  const reset = jobs.createState();
  jobs.fold(reset, [post, expiry, reclaim, expiry], {reset:true, validateEvent});
  assert.equal(jobs.rows(reset, Date.parse("2026-08-25T10:02:00.000Z"))[0].state, "claimed");
});

test("invalid task ownership and lineage cannot render or acknowledge", () => {
  const badClaim = event("task_claimed", "forged", "2026-08-25T10:00:00.000Z",
    {claimant:"codex:forged"});
  assert.equal(validateEvent(badClaim), "payload.claimant must match agent_id");
  const badPost = event("task_posted", "bad-parent", "2026-08-25T10:00:00.000Z",
    {parent_task_id:" "});
  assert.equal(validateEvent(badPost), "invalid payload.parent_task_id");
  const state = jobs.createState();
  jobs.fold(state, [badClaim, badPost], {validateEvent});
  assert.deepEqual(jobs.rows(state), []);
  assert.equal(state.malformed, 2);

  const cursor = offset => `v1:0123456789abcdef0123456789abcdef:1:2:3:${offset}`;
  const acks = jobs.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
  const tracking = acks.request(cursor(1));
  acks.accepted(tracking.id, "bad-parent", "request-bad-parent");
  acks.observe({events:[badPost], cursor:cursor(2)}, validateEvent);
  assert.equal(acks.get(tracking.id).state, "pending");
});

test("reset clears ghosts and capacity keeps only newest task identities", () => {
  const state = jobs.createState();
  for (let index = 0; index <= jobs.MAX_TASKS; index++) jobs.fold(state,
    [event("task_posted", `task-${index}`, new Date(Date.parse("2026-08-25T10:00:00.000Z") + index).toISOString())],
    {validateEvent});
  assert.equal(state.tasks.size, jobs.MAX_TASKS);
  assert.equal(state.tasks.has("task-0"), false);
  assert.equal(state.capacityDropped, 1);
  jobs.fold(state, [event("task_posted", "fresh", "2026-08-25T11:00:00.000Z")],
    {reset:true,validateEvent});
  assert.deepEqual(jobs.rows(state).map(row => row.id), ["fresh"]);
  assert.equal(state.capacityDropped, 0);
});

test("capacity diagnostics do not imply that an omitted terminal task is older", async t => {
  const start = Date.parse("2026-08-25T10:00:00.000Z");
  const activePosts = Array.from({length:jobs.MAX_TASKS}, (_, index) =>
    event("task_posted", `active-${index}`, new Date(start + index).toISOString()));
  for (const type of ["task_done", "task_failed"]) await t.test(type, () => {
    const state = jobs.createState();
    const newerTerminal = event(type, `newer-${type}`,
      new Date(start + jobs.MAX_TASKS + 1000).toISOString());
    jobs.fold(state, [...activePosts, newerTerminal], {validateEvent});
    assert.equal(state.tasks.size, jobs.MAX_TASKS);
    assert.equal(state.tasks.has(`newer-${type}`), false,
      "active-first retention may omit a newer terminal-only identity");
    assert.equal(state.capacityDropped, 1,
      "the age-neutral capacity counter reports the valid omission");
    assert.deepEqual(jobs.rows(state, start + jobs.MAX_TASKS + 1001).map(row => row.id),
      [...activePosts].reverse().map(item => item.payload.task_id));
  });
});

test("capacity is event-granular across live, grouped, and reset replay", () => {
  const start = Date.parse("2026-08-25T10:00:00.000Z");
  const posts = Array.from({length:jobs.MAX_TASKS + 1}, (_, index) =>
    event("task_posted", `task-${index}`, new Date(start + index).toISOString(),
      {required_skills:[`skill-${index}`]}));
  const evictedClaim = event("task_claimed", "task-0",
    new Date(start + 1000).toISOString(), {title:"Claimed after eviction"});
  const stream = jobs.createState();
  for (const item of [...posts, evictedClaim]) jobs.fold(stream, [item], {validateEvent});
  const grouped = jobs.createState();
  jobs.fold(grouped, [...posts, evictedClaim], {validateEvent});
  const reset = jobs.createState();
  jobs.fold(reset, [event("task_posted", "ghost", new Date(start - 1).toISOString())],
    {validateEvent});
  jobs.fold(reset, [...posts, evictedClaim], {reset:true,validateEvent});
  const canonical = state => jobs.rows(state, start + 2000).map(row => ({
    id:row.id,title:row.title,required_skills:row.required_skills,
    posted_at:row.posted_at,updated_at:row.updated_at,state:row.state,claimant:row.claimant,
  }));
  assert.deepEqual(canonical(grouped), canonical(stream));
  assert.deepEqual(canonical(reset), canonical(stream));
  const orphan = canonical(stream).find(row => row.id === "task-0");
  assert.deepEqual(orphan, {id:"task-0",title:"Claimed after eviction",required_skills:null,
    posted_at:null,updated_at:start + 1000,state:"claimed",claimant:"claude-code:one"},
  "an evicted post is not reconstructed from earlier evidence in the same batch");
  assert.equal(stream.tasks.has("task-1"), false);
  assert.equal(stream.tasks.size, jobs.MAX_TASKS);
});

test("ten thousand same-ms transitions use constant metadata and converge under replay", () => {
  const timestamp = "2026-08-25T10:01:00.000Z";
  const post = event("task_posted", "stress", "2026-08-25T10:00:00.000Z",
    {required_skills:["research"]});
  const transitions = Array.from({length:10_000}, (_, index) => {
    const claimant = `codex:holder-${String(index).padStart(5, "0")}`;
    const item = event("task_claimed", "stress", timestamp, {claimant});
    item.agent_id = claimant;
    return item;
  });
  const live = jobs.createState();
  jobs.fold(live, [post], {validateEvent});
  for (const item of transitions) jobs.fold(live, [item], {validateEvent});
  const grouped = jobs.createState();
  jobs.fold(grouped, [post, ...transitions], {validateEvent});
  const reversed = jobs.createState();
  jobs.fold(reversed, [post, ...[...transitions].reverse()], {reset:true,validateEvent});
  const beforeReplay = jobs.rows(live, Date.parse(timestamp) + 1);
  jobs.fold(live, [transitions[0], transitions.at(-1), transitions[5000]], {validateEvent});
  assert.deepEqual(jobs.rows(live, Date.parse(timestamp) + 1), beforeReplay,
    "exact current and older replays cannot regress the winner");
  assert.deepEqual(jobs.rows(grouped, Date.parse(timestamp) + 1), beforeReplay);
  assert.deepEqual(jobs.rows(reversed, Date.parse(timestamp) + 1), beforeReplay);
  assert.deepEqual(Object.keys(live.tasks.get("stress")).sort(), ["id","posted","transition"],
    "per-task equal-time metadata remains constant instead of accumulating fingerprints");
  assert.equal(live.tasks.size, 1);
});

test("acknowledgement requires exact id after exact cursor and handles replay, reset, timeout", () => {
  let timeout;
  const acks = jobs.createAcknowledgements({timeoutMs:10,
    setTimeout: fn => { timeout = fn; return 1; }, clearTimeout() {}});
  const cursor = offset => `v1:0123456789abcdef0123456789abcdef:1:2:3:${offset}`;
  const tracking = acks.request(cursor(10));
  assert.equal(tracking.ok, true);
  const matching = event("task_posted", "accepted", "2026-08-25T10:00:00.000Z");
  acks.observe({events:[matching],cursor:cursor(9)}, validateEvent);
  acks.accepted(tracking.id, "accepted", "request-accepted");
  assert.equal(acks.get(tracking.id).state, "pending", "old replay cannot acknowledge");
  acks.observe({events:[event("task_posted", "other", matching.ts)],cursor:cursor(11)}, validateEvent);
  assert.equal(acks.get(tracking.id).state, "pending", "title or proximity cannot acknowledge another id");
  const incomplete = event("task_posted", "accepted", matching.ts);
  delete incomplete.payload.posted_by;
  acks.observe({events:[incomplete],cursor:cursor(11)}, validateEvent);
  assert.equal(acks.get(tracking.id).state, "pending", "an invalid post cannot acknowledge acceptance");
  acks.observe({events:[matching],cursor:cursor(12)}, validateEvent);
  assert.equal(acks.get(tracking.id).state, "acknowledged");

  const reset = acks.request(cursor(12));
  acks.accepted(reset.id, "reset-task", "request-reset");
  acks.observe({events:[event("task_posted", "reset-task", matching.ts)],cursor:cursor(20),reset:true}, validateEvent);
  assert.equal(acks.get(reset.id).state, "ambiguous");
  assert.equal(acks.blocksSubmission(), true);
  assert.equal(acks.request(cursor(20)).ok, false,
    "an ambiguous outcome prevents an unsafe duplicate");

  const timeoutAcks = jobs.createAcknowledgements({timeoutMs:10,
    setTimeout: fn => { timeout = fn; return 1; }, clearTimeout() {}});
  const late = timeoutAcks.request(cursor(20));
  timeoutAcks.accepted(late.id, "late", "request-late"); timeout();
  assert.equal(timeoutAcks.get(late.id).state, "timeout");
  assert.equal(timeoutAcks.blocksSubmission(), true,
    "a known Steward acceptance cannot be retried merely because its event timed out");
});

test("timeout stays blocked until a late definitive refusal makes retry safe", () => {
  let timeout;
  const cursor = "v1:0123456789abcdef0123456789abcdef:1:2:3:20";
  const acks = jobs.createAcknowledgements({timeoutMs:10,
    setTimeout:fn=>{timeout=fn;return 1;},clearTimeout(){}});
  const tracking = acks.request(cursor);
  timeout();
  assert.equal(acks.get(tracking.id).state,"timeout");
  assert.equal(acks.blocksSubmission(),true);
  acks.failed(tracking.id,"late definitive response",true);
  assert.equal(acks.get(tracking.id).state,"failed");
  assert.equal(acks.blocksSubmission(),false);
});

test("pre-response candidate overflow becomes indeterminate instead of false timeout", () => {
  const cursor = offset => `v1:0123456789abcdef0123456789abcdef:1:2:3:${offset}`;
  for (const matchingFirst of [true, false]) {
    let timeout;
    const acks = jobs.createAcknowledgements({timeoutMs:10,
      setTimeout:fn=>{timeout=fn;return 1;},clearTimeout(){}});
    const tracking = acks.request(cursor(10));
    const candidates = [];
    if (matchingFirst) candidates.push(event("task_posted", "requested",
      "2026-08-25T10:00:00.000Z"));
    for (let index=0; index<jobs.MAX_TASKS + (matchingFirst ? 0 : 1); index++) {
      candidates.push(event("task_posted", `other-${index}`,
        new Date(Date.parse("2026-08-25T10:01:00.000Z") + index).toISOString()));
    }
    acks.observe({events:candidates,cursor:cursor(40)},validateEvent);
    assert.equal(acks.get(tracking.id).state,"indeterminate");
    assert.ok(acks.get(tracking.id).candidates.size <= jobs.MAX_TASKS);
    acks.accepted(tracking.id,"requested", "request-overflow");
    timeout();
    assert.equal(acks.get(tracking.id).state,"indeterminate",
      "late response and deadline cannot rewrite explicit bounded evidence loss");
    assert.equal(acks.blocksSubmission(),true);
  }
});

test("known accepted task ignores unrelated candidate volume and keeps exact proof", () => {
  const cursor = offset => `v1:0123456789abcdef0123456789abcdef:1:2:3:${offset}`;
  const acks = jobs.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
  const tracking = acks.request(cursor(10));
  acks.accepted(tracking.id,"requested", "request-known");
  const unrelated = Array.from({length:jobs.MAX_TASKS + 20},(_,index)=>
    event("task_posted",`other-${index}`,
      new Date(Date.parse("2026-08-25T10:01:00.000Z") + index).toISOString()));
  acks.observe({events:unrelated,cursor:cursor(80)},validateEvent);
  assert.equal(acks.get(tracking.id).state,"pending");
  assert.equal(acks.get(tracking.id).candidates.size,0);
  acks.observe({events:[event("task_posted","requested","2026-08-25T10:02:00.000Z")],
    cursor:cursor(81)},validateEvent);
  assert.equal(acks.get(tracking.id).state,"acknowledged");
});

test("late valid acceptance retains identities and reconciles already-staged exact evidence", () => {
  let now = 1_000, scheduledDelay;
  const changes = [];
  const acks = jobs.createAcknowledgements({timeoutMs:15_000, now:()=>now,
    setTimeout:(fn, delay)=>{ scheduledDelay=delay; return 1; }, clearTimeout(){},
    onChange:item=>changes.push(item.state)});
  const cursor = "v1:0123456789abcdef0123456789abcdef:1:2:3:10";
  const tracking = acks.request(cursor);
  assert.equal(scheduledDelay, 15_000, "deadline is scheduled from request start");
  acks.observe({events:[event("task_posted", "late-acceptance",
    "2026-08-25T10:00:00.000Z")],cursor:"v1:0123456789abcdef0123456789abcdef:1:2:3:11"},
  validateEvent);
  assert.equal(acks.get(tracking.id).state, "requesting",
    "event evidence is staged until Steward identifies the request");
  now = 16_001;
  acks.accepted(tracking.id, "late-acceptance", "late-request");
  const item = acks.get(tracking.id);
  assert.equal(item.state, "acknowledged");
  assert.equal(item.task_id, "late-acceptance");
  assert.equal(item.request_id, "late-request");
  assert.equal(item.deadlineElapsedAt, 16_001);
  assert.match(item.message, /after the acknowledgement deadline had elapsed/);
  assert.ok(changes.includes("timeout"), "deadline transition is observable to the browser renderer");
  assert.equal(acks.blocksSubmission(), false, "only exact evidence releases duplicate blocking");
});

test("late valid acceptance stays timed out until future exact evidence", () => {
  let now = 1_000;
  const cursor = offset => `v1:0123456789abcdef0123456789abcdef:1:2:3:${offset}`;
  const acks = jobs.createAcknowledgements({timeoutMs:10,now:()=>now,
    setTimeout:()=>1,clearTimeout(){}});
  const tracking = acks.request(cursor(10));
  now = 1_011;
  acks.accepted(tracking.id,"future-exact","future-request");
  const timedOut = acks.get(tracking.id);
  assert.equal(timedOut.state,"timeout");
  assert.equal(timedOut.task_id,"future-exact");
  assert.equal(timedOut.request_id,"future-request");
  assert.equal(timedOut.deadlineElapsedAt,1_011);
  assert.equal(acks.blocksSubmission(),true);
  acks.observe({events:[event("task_posted","unrelated","2026-08-25T10:00:00.000Z")],
    cursor:cursor(11)},validateEvent);
  assert.equal(acks.get(tracking.id).state,"timeout","unrelated evidence cannot resolve it");
  acks.observe({events:[event("task_posted","future-exact","2026-08-25T10:00:01.000Z")],
    cursor:cursor(12)},validateEvent);
  assert.equal(acks.get(tracking.id).state,"acknowledged");
  assert.match(acks.get(tracking.id).message,/after the acknowledgement deadline had elapsed/);
  assert.equal(acks.blocksSubmission(),false);
});

test("reset discards staged evidence before a late response supplies its identity", () => {
  let now = 1_000;
  const cursor = offset => `v1:0123456789abcdef0123456789abcdef:1:2:3:${offset}`;
  const resetCursor = offset => `v1:fedcba9876543210fedcba9876543210:4:5:6:${offset}`;
  const acks = jobs.createAcknowledgements({timeoutMs:10,now:()=>now,
    setTimeout:()=>1,clearTimeout(){}});
  const tracking = acks.request(cursor(10));
  acks.observe({events:[event("task_posted","stale-candidate","2026-08-25T10:00:00.000Z")],
    cursor:cursor(11)},validateEvent);
  now = 1_011;
  acks.observe({events:[event("task_posted","stale-candidate","2026-08-25T10:00:01.000Z")],
    cursor:resetCursor(20),reset:true},validateEvent);
  assert.equal(acks.get(tracking.id).candidates.size,0);
  acks.accepted(tracking.id,"stale-candidate","late-after-reset");
  assert.equal(acks.get(tracking.id).state,"timeout");
  assert.equal(acks.blocksSubmission(),true);
});

test("post uses actual Steward contract without leaking token into body or persistence", async () => {
  let call;
  const result = await jobs.postJob({url:"https://steward.test/",token:"top-secret"},
    {title:" Research X ",detail:"long",required_skills:"research, writing, research"},
    async (url, options) => {
      call = {url,options};
      return {status:202,json:async()=>({status:"accepted",task_id:"task-1",request_id:"request-1"})};
    }, {setTimeout:()=>1,clearTimeout(){}});
  assert.equal(result.task_id, "task-1");
  assert.equal(call.url, "https://steward.test/jobs");
  assert.equal(call.options.headers.Authorization, "Bearer top-secret");
  assert.deepEqual(JSON.parse(call.options.body), {title:"Research X",detail:"long",
    required_skills:["research","writing"]});
  assert.doesNotMatch(call.options.body, /top-secret/);
  assert.equal(call.options.credentials, "omit");
});

test("only contractually pre-mutation HTTP refusals are definitive", async () => {
  for (const status of [401, 422]) {
    await assert.rejects(jobs.postJob({url:"https://steward.test",token:"x"},{title:"No"},
      async()=>({status,json:async()=>({})}), {setTimeout:()=>1,clearTimeout(){}}),
    error => error.definitive === true);
  }
  for (const status of [400, 404, 409, 429, 500, 502, 503]) {
    await assert.rejects(jobs.postJob({url:"https://steward.test",token:"x"},{title:"Maybe"},
      async()=>({status,json:async()=>({})}), {setTimeout:()=>1,clearTimeout(){}}),
    error => error.definitive === false && /ambiguous/.test(error.message));
  }
  await assert.rejects(jobs.postJob({url:"https://steward.test",token:"x"},{title:"Maybe"},
    async()=>{throw new Error("network lost");}, {setTimeout:()=>1,clearTimeout(){}}),
  error => error.definitive !== true);
});

test("ambiguous HTTP outcome reconciles only its exact task event", async () => {
  let error;
  try {
    await jobs.postJob({url:"https://steward.test",token:"x"},{title:"Maybe"},
      async()=>({status:503,json:async()=>({task_id:"task-exact"})}),
      {setTimeout:()=>1,clearTimeout(){}});
  } catch (caught) { error = caught; }
  assert.equal(error.definitive, false);
  assert.equal(error.taskId, "task-exact");

  const cursor = offset => `v1:0123456789abcdef0123456789abcdef:1:2:3:${offset}`;
  const acks = jobs.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
  const tracking = acks.request(cursor(10));
  acks.failed(tracking.id, error.message, error.definitive, error.taskId);
  assert.equal(acks.get(tracking.id).state, "ambiguous");
  assert.equal(acks.blocksSubmission(), true);
  acks.observe({events:[event("task_posted", "other", "2026-08-25T10:00:00.000Z")],
    cursor:cursor(11)}, validateEvent);
  assert.equal(acks.get(tracking.id).state, "ambiguous");
  acks.observe({events:[event("task_posted", "task-exact", "2026-08-25T10:00:01.000Z")],
    cursor:cursor(12)}, validateEvent);
  assert.equal(acks.get(tracking.id).state, "acknowledged");
  assert.equal(acks.blocksSubmission(), false);
});

test("accepted response requires exact non-empty task and request identities", async () => {
  for (const payload of [
    null,
    [],
    {status:"accepted",task_id:"task-1"},
    {status:"accepted",task_id:"task-1",request_id:"   "},
    {status:"accepted",task_id:"task-1",request_id:42},
    {status:"accepted",task_id:"   ",request_id:"request-1"},
  ]) {
    await assert.rejects(jobs.postJob({url:"https://steward.test",token:"x"},{title:"Maybe"},
      async()=>({status:202,json:async()=>payload}), {setTimeout:()=>1,clearTimeout(){}}),
    error => error.definitive !== true && /ambiguous/.test(error.message));
  }
  await assert.rejects(jobs.postJob({url:"https://steward.test",token:"x"},{title:"Maybe"},
    async()=>({status:202,json:async()=>{throw new SyntaxError("bad json");}}),
    {setTimeout:()=>1,clearTimeout(){}}),
  error => error.definitive !== true && /invalid JSON.*ambiguous/.test(error.message));
});

test("malformed 202 retains only a safe task identity for exact event reconciliation", async () => {
  const invalidEnvelopes = [
    {task_id:"malformed-staged",status:"accepted"},
    {task_id:"malformed-future",request_id:"request-but-wrong-status",status:"queued"},
  ];
  const errors = [];
  for (const payload of invalidEnvelopes) {
    try {
      await jobs.postJob({url:"https://steward.test",token:"x"},{title:"Maybe"},
        async()=>({status:202,json:async()=>payload}),{setTimeout:()=>1,clearTimeout(){}});
    } catch (error) { errors.push(error); }
  }
  assert.deepEqual(errors.map(error=>[error.definitive,error.taskId]),
    [[false,"malformed-staged"],[false,"malformed-future"]]);

  const cursor = offset => `v1:0123456789abcdef0123456789abcdef:1:2:3:${offset}`;
  const staged = jobs.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
  const stagedRequest = staged.request(cursor(10));
  staged.observe({events:[event("task_posted","malformed-staged","2026-08-25T10:00:00.000Z")],
    cursor:cursor(11)},validateEvent);
  staged.failed(stagedRequest.id,errors[0].message,errors[0].definitive,errors[0].taskId);
  assert.equal(staged.get(stagedRequest.id).state,"acknowledged",
    "the exact staged event, not the malformed envelope, is acknowledgement");

  const future = jobs.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
  const futureRequest = future.request(cursor(10));
  future.failed(futureRequest.id,errors[1].message,errors[1].definitive,errors[1].taskId);
  assert.equal(future.get(futureRequest.id).state,"ambiguous");
  future.observe({events:[event("task_posted","not-it","2026-08-25T10:00:00.000Z")],
    cursor:cursor(11)},validateEvent);
  assert.equal(future.get(futureRequest.id).state,"ambiguous");
  future.observe({events:[event("task_posted","malformed-future","2026-08-25T10:00:01.000Z")],
    cursor:cursor(12)},validateEvent);
  assert.equal(future.get(futureRequest.id).state,"acknowledged");
});

test("malformed 202 with invalid task identity remains unidentified and blocked", async () => {
  for (const task_id of ["   ",42,null]) {
    let error;
    try {
      await jobs.postJob({url:"https://steward.test",token:"x"},{title:"Maybe"},
        async()=>({status:202,json:async()=>({status:"queued",task_id})}),
        {setTimeout:()=>1,clearTimeout(){}});
    } catch (caught) { error = caught; }
    assert.equal(error.taskId,undefined);
    const cursor = offset => `v1:0123456789abcdef0123456789abcdef:1:2:3:${offset}`;
    const acks = jobs.createAcknowledgements({setTimeout:()=>1,clearTimeout(){}});
    const tracking = acks.request(cursor(10));
    acks.failed(tracking.id,error.message,error.definitive,error.taskId);
    acks.observe({events:[event("task_posted","unrelated","2026-08-25T10:00:00.000Z")],
      cursor:cursor(11)},validateEvent);
    assert.equal(acks.get(tracking.id).state,"ambiguous");
    assert.equal(acks.get(tracking.id).task_id,null);
    assert.equal(acks.blocksSubmission(),true);
  }
});

test("401 is a definitive, identifiable credential rejection", async () => {
  await assert.rejects(jobs.postJob({url:"https://steward.test",token:"wrong"},{title:"Retry me"},
    async()=>({status:401,json:async()=>({})}), {setTimeout:()=>1,clearTimeout(){}}),
  error => error.definitive === true && error.authRejected === true && !error.message.includes("wrong"));
});
