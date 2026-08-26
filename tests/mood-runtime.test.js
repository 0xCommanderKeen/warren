"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createBrowserRuntime } = require("../viewer/browser-runtime.js");

const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/mood-rotation.json")));
const futureSufficiency = JSON.parse(fs.readFileSync(path.join(__dirname,
  "fixtures/mood-future-sufficiency.json")));
const lines = fixture.events.map(event => JSON.stringify(event));
const BOOT = "0123456789abcdef0123456789abcdef";
const cursor = offset => `v1:${BOOT}:1:2:3:${offset}`;
function eventResponse(batch, offset, reset = false) {
  return { ok: true, headers: { get(name) {
    if (name === "X-Burrow-Cursor") return cursor(offset);
    if (name === "X-Burrow-Reset") return reset ? "1" : null;
    return null;
  } }, text: async () => batch.join("\n") };
}
const villagersResponse = { ok: true, json: async () => [] };

const regression = JSON.parse(fs.readFileSync(path.join(__dirname,
  "fixtures/mood-rotation-regressions.json")));
function regressionEvents() {
  const base = { v: 0, source: "codex", project: "burrow" };
  const plain = regression.plain;
  const events = [
    { ...base, ts: plain.knock, agent_id: plain.agent_id, type: "needs_human",
      payload: { message: "legacy" } },
    { ...base, source: "steward", ts: plain.failure, agent_id: plain.agent_id,
      type: "routine_failed", payload: { routine: "nightly", run_id: "r", error: "boom" } },
    { ...base, ts: plain.boundary, agent_id: plain.agent_id, type: "idle", payload: {} },
  ];
  const capacity = regression.capacity;
  events.push({ ...base, ts: capacity.oldest, agent_id: capacity.agent_id,
    type: "needs_human", payload: { message: "Old", request_id: "oldest",
      action: "deploy", detail: null, options: ["approve", "deny"] } });
  const newest = Date.parse(regression.now);
  for (let index = 0; index < capacity.recent_count; index++) {
    events.push({ ...base, ts: new Date(newest - (capacity.recent_count - 1 - index) * 60000).toISOString(),
      agent_id: capacity.agent_id, type: "needs_human", payload: { message: "Recent",
        request_id: `recent-${index}`, action: "deploy", detail: null,
        options: ["approve", "deny"] } });
  }
  return events;
}

function runtimeWith(responses) {
  let read = 0;
  return createBrowserRuntime({ now: () => Date.parse(fixture.now), EventSource: null,
    setTimeout: () => 1, clearTimeout() {},
    fetch: async url => url === "/villagers" ? villagersResponse : responses[read++],
  });
}

test("incremental, grouped bootstrap and reset publish byte-identical shared Mood objects", async () => {
  const grouped = runtimeWith([eventResponse(lines, lines.length)]);
  await grouped.poll();
  const groupedMood = grouped.snapshot().villagers[0].mood;

  const split = 4;
  const incremental = runtimeWith([eventResponse(lines.slice(0, split), split),
    eventResponse(lines.slice(split), lines.length)]);
  await incremental.poll(); await incremental.poll();
  const incrementalMood = incremental.snapshot().villagers[0].mood;

  const reset = runtimeWith([eventResponse([lines.at(-1)], 1),
    eventResponse(lines, lines.length, true)]);
  await reset.poll(); await reset.poll();
  const resetMood = reset.snapshot().villagers[0].mood;

  assert.deepEqual(incrementalMood, groupedMood);
  assert.deepEqual(resetMood, groupedMood);
  assert.equal(groupedMood.status, "blocked");
});

test("actual sufficiency contributors survive incremental runtime retention", async () => {
  const adversarial = JSON.parse(fs.readFileSync(path.join(__dirname,
    "fixtures/mood-rotation-adversarial.json")));
  const adversarialLines = adversarial.events.map(event => JSON.stringify(event));
  const options = responses => {
    let read = 0;
    return createBrowserRuntime({ now: () => Date.parse(adversarial.now), EventSource: null,
      setTimeout: () => 1, clearTimeout() {},
      fetch: async url => url === "/villagers" ? villagersResponse : responses[read++],
    });
  };
  const grouped = options([eventResponse(adversarialLines, adversarialLines.length)]);
  await grouped.poll();
  const groupedSnapshot = grouped.snapshot();
  const groupedMood = groupedSnapshot.villagers.find(villager =>
    villager.id === "codex:mood-adversarial").mood;
  const groupedInterrupted = groupedSnapshot.villagers.find(villager =>
    villager.id === "codex:mood-interrupted").mood;
  const incremental = options(adversarialLines.map((line, index) =>
    eventResponse([line], index + 1)));
  for (let index = 0; index < adversarialLines.length; index++) await incremental.poll();
  const incrementalSnapshot = incremental.snapshot();
  assert.equal(groupedMood.enoughEvidence, true);
  assert.deepEqual(incrementalSnapshot.villagers.find(villager =>
    villager.id === "codex:mood-adversarial").mood, groupedMood);
  assert.deepEqual(incrementalSnapshot.villagers.find(villager =>
    villager.id === "codex:mood-interrupted").mood, groupedInterrupted);
  assert.deepEqual(groupedInterrupted.signals.failure,
    { observed: true, streak: 0, failures: 3, failuresLabel: "3+" });
});

test("grouped-to-incremental plain supersession keeps future-safe sufficiency", async () => {
  const initial = futureSufficiency.initial.map(JSON.stringify);
  const appended = JSON.stringify(futureSufficiency.append);
  const read = runtime => runtime.snapshot().villagers.find(villager =>
    villager.id === futureSufficiency.agent_id).mood;
  const full = runtimeWith([eventResponse([...initial, appended], initial.length + 1)]);
  await full.poll();
  const expected = read(full);
  assert.deepEqual([expected.glyph, expected.status, expected.evidence],
    ["◆", "active", { count: 6, spanMs: 30 * 60 * 1000 }]);

  const staged = runtimeWith([eventResponse(initial, initial.length),
    eventResponse([appended], initial.length + 1)]);
  await staged.poll(); await staged.poll();
  assert.deepEqual(read(staged), expected);
  assert.equal(read(staged).signals.unresolvedNeed.observed, false);
});

test("300 cross-agent collisions stay bounded behind global runtime uncertainty", async () => {
  const at = Date.parse(fixture.now) - 10 * 60 * 1000;
  const collision = (index, agentId, action) => ({
    v: 0, ts: new Date(at + index * 1000).toISOString(), source: "codex",
    agent_id: agentId, project: "burrow", type: "needs_human",
    payload: { message: `collision ${index}`, request_id: `shared-${index}`,
      action, detail: null, options: ["approve"] },
  });
  const events = [];
  for (let index = 0; index < 300; index++) {
    events.push(collision(index, "codex:collision-hub", "deploy"));
    events.push(collision(index, `codex:collision-temp-${index}`, "erase"));
  }
  events.push({ v: 0, ts: fixture.now, source: "codex",
    agent_id: "codex:collision-hub", project: "burrow", type: "heartbeat", payload: {} });
  const lines = events.map(JSON.stringify);
  const grouped = runtimeWith([eventResponse(lines, lines.length)]);
  await grouped.poll();
  const groupedSnapshot = grouped.snapshot();
  assert.deepEqual(groupedSnapshot.moodAuthority,
    { retained: 0, overflow: true, observed: 257 });
  assert.equal(groupedSnapshot.villagers.find(villager =>
    villager.id === "codex:collision-hub").mood.status, "authority history uncertain");

  const staged = runtimeWith([eventResponse(lines.slice(0, 300), 300),
    eventResponse(lines.slice(300), lines.length)]);
  await staged.poll(); await staged.poll();
  assert.deepEqual(staged.snapshot().moodAuthority, groupedSnapshot.moodAuthority);
  assert.equal(staged.snapshot().villagers.find(villager =>
    villager.id === "codex:collision-hub").mood.status, "authority history uncertain");
});

test("grouped and incremental runtime preserve bounded-complete append-order Mood truth", async () => {
  const regressionLines = regressionEvents().map(event => JSON.stringify(event));
  const grouped = runtimeWith([eventResponse(regressionLines, regressionLines.length)]);
  await grouped.poll();
  const incremental = runtimeWith(regressionLines.map((line, index) =>
    eventResponse([line], index + 1)));
  for (let index = 0; index < regressionLines.length; index++) await incremental.poll();
  const byId = snapshot => Object.fromEntries(snapshot.villagers.map(villager =>
    [villager.id, villager.mood]));
  const groupedMoods = byId(grouped.snapshot()), incrementalMoods = byId(incremental.snapshot());
  assert.deepEqual(incrementalMoods, groupedMoods);
  assert.equal(groupedMoods[regression.plain.agent_id].signals.unresolvedNeed.observed, false);
  assert.equal(groupedMoods[regression.capacity.agent_id].signals.unresolvedNeed.request_id, "oldest");
  assert.equal(groupedMoods[regression.capacity.agent_id].status, "blocked");
});

test("grouped bootstrap and reset select Mood authority before the raw transport cap", async () => {
  const anchor = Date.parse(fixture.now), agentId = "codex:transport-pressure";
  const base = { v: 0, source: "codex", agent_id: agentId, project: "burrow" };
  const pending = { ...base, ts: new Date(anchor - 8 * 60 * 60 * 1000).toISOString(),
    type: "needs_human", payload: { message: "Approve deployment?", request_id: "old-pending",
      action: "deploy", detail: null, options: ["approve", "deny"] } };
  const pressure = Array.from({ length: 4100 }, (_, index) => ({ ...base,
    ts: new Date(anchor - 60 * 60 * 1000 + index).toISOString(), type: "tool_called",
    payload: { tool: `Read-${index}` } }));
  const idle = { ...base, ts: new Date(anchor).toISOString(), type: "idle", payload: {} };
  const complete = [pending, ...pressure, idle].map(JSON.stringify);
  const readMood = runtime => runtime.snapshot().villagers.find(villager =>
    villager.id === agentId).mood;

  const grouped = runtimeWith([eventResponse(complete, complete.length)]);
  await grouped.poll();
  const groupedMood = readMood(grouped);
  assert.equal(groupedMood.status, "blocked");
  assert.equal(groupedMood.signals.unresolvedNeed.request_id, "old-pending");

  const reset = runtimeWith([eventResponse([JSON.stringify(idle)], 1),
    eventResponse(complete, complete.length, true)]);
  await reset.poll(); await reset.poll();
  assert.deepEqual(readMood(reset), groupedMood,
    "X-Burrow-Reset rebuild derives bounded Mood witnesses from the complete response");
});

test("shared adversarial lifecycles have grouped, incremental and reset parity", async () => {
  const adversarial = JSON.parse(fs.readFileSync(path.join(__dirname,
    "fixtures/mood-lifecycle-adversarial.json")));
  const adversarialLines = adversarial.events.map(JSON.stringify);
  const read = runtime => Object.fromEntries(runtime.snapshot().villagers.map(villager =>
    [villager.id, villager.mood]));
  const grouped = runtimeWith([eventResponse(adversarialLines, adversarialLines.length)]);
  await grouped.poll();
  const expected = read(grouped);
  assert.equal(expected["codex:close"].signals.interaction.kind, "approval decision");
  assert.equal(expected["codex:owner"].signals.unresolvedNeed.kind, "structured collision");
  assert.equal(expected["codex:canonical"], undefined,
    "the cross-agent dependency does not invent canonical-owner presence");

  const incremental = runtimeWith(adversarialLines.map((line, index) =>
    eventResponse([line], index + 1)));
  for (let index = 0; index < adversarialLines.length; index++) await incremental.poll();
  assert.deepEqual(read(incremental), expected);

  const reset = runtimeWith([eventResponse([adversarialLines.at(-1)], 1),
    eventResponse(adversarialLines, adversarialLines.length, true)]);
  await reset.poll(); await reset.poll();
  assert.deepEqual(read(reset), expected);
});

test("shared ambiguous-close authority has grouped, incremental and reset parity", async () => {
  const fixtureData = JSON.parse(fs.readFileSync(path.join(__dirname,
    "fixtures/mood-lifecycle-ambiguity.json")));
  const post = fixtureData.scenarios.post_close_collision;
  const pendingStart = Date.parse(post.pending_start);
  const pending = Array.from({ length: post.pending_count }, (_, index) => ({
    ...post.anchor, ts: new Date(pendingStart + index * 60000).toISOString(),
    type: "needs_human", payload: { message: `Pending ${index}`,
      request_id: `pressure-${index}`, action: "deploy", detail: null,
      options: ["approve", "deny"] },
  }));
  const scenarios = [fixtureData.scenarios.ambiguous_close,
    [...post.events, ...pending, post.anchor]];
  const targetIds = ["codex:ambiguous", "codex:post-collision"];
  for (let scenarioIndex = 0; scenarioIndex < scenarios.length; scenarioIndex++) {
    const scenarioLines = scenarios[scenarioIndex].map(JSON.stringify);
    const read = runtime => Object.fromEntries(runtime.snapshot().villagers.map(villager =>
      [villager.id, villager.mood]));
    const grouped = runtimeWith([eventResponse(scenarioLines, scenarioLines.length)]);
    await grouped.poll();
    const expected = read(grouped);
    const targetId = targetIds[scenarioIndex];
    assert.equal(expected[targetId].status, "blocked");

    const incremental = runtimeWith(scenarioLines.map((line, index) =>
      eventResponse([line], index + 1)));
    for (let index = 0; index < scenarioLines.length; index++) await incremental.poll();
    assert.deepEqual(read(incremental)[targetId], expected[targetId]);

    const reset = runtimeWith([eventResponse([scenarioLines.at(-1)], 1),
      eventResponse(scenarioLines, scenarioLines.length, true)]);
    await reset.poll(); await reset.poll();
    assert.deepEqual(read(reset)[targetId], expected[targetId]);
  }
});

test("capsule/raw append interleaving is byte-identical for orphan, plain and multiple-root authority", async () => {
  const orderFixture = JSON.parse(fs.readFileSync(path.join(__dirname,
    "fixtures/mood-authority-order.json")));
  const orderLines = orderFixture.events.map(JSON.stringify);
  const read = runtime => Object.fromEntries(runtime.snapshot().villagers.map(villager =>
    [villager.id, villager.mood]));
  const grouped = runtimeWith([eventResponse(orderLines, orderLines.length)]);
  await grouped.poll();
  const expected = read(grouped);
  assert.equal(expected["codex:orphan-first"].signals.unresolvedNeed.request_id, "late");
  assert.equal(expected["codex:plain-root"].signals.unresolvedNeed.observed, false);
  assert.equal(expected["codex:multiple-roots"].signals.interaction.level, "aging");

  const incremental = runtimeWith(orderLines.map((line, index) =>
    eventResponse([line], index + 1)));
  for (let index = 0; index < orderLines.length; index++) await incremental.poll();
  assert.deepEqual(read(incremental), expected);
  const reset = runtimeWith([eventResponse([orderLines.at(-1)], 1),
    eventResponse(orderLines, orderLines.length, true)]);
  await reset.poll(); await reset.poll();
  assert.deepEqual(read(reset), expected);
});

test("incremental Mood authority precedes ordinary liveness beyond the transport cap", async () => {
  const anchor = Date.parse(fixture.now), agentId = "codex:late-visible";
  const base = { v: 0, source: "steward", agent_id: agentId, project: "burrow" };
  const specialized = Array.from({ length: 4100 }, (_, index) => ({ ...base,
    ts: new Date(anchor - index * 1000).toISOString(), type: "routine_finished",
    payload: { routine: "watch", run_id: `run-${index}`, outcome: "ok",
      artifacts: [], duration_s: 1 } }));
  const idle = { ...base, source: "codex", ts: new Date(anchor - 500).toISOString(),
    type: "idle", payload: {} };
  const complete = [...specialized, idle].map(JSON.stringify);
  const read = runtime => runtime.snapshot().villagers.find(villager =>
    villager.id === agentId).mood;

  const grouped = runtimeWith([eventResponse(complete, complete.length)]);
  await grouped.poll();
  const expected = read(grouped);
  assert.equal(expected.anchor, new Date(anchor).toISOString());
  assert.equal(expected.status, "authority history uncertain");
  assert.equal(expected.authority.complete, false);
  assert.deepEqual(expected.evidence, { count: 0, spanMs: 0 });
  assert.equal(expected.signals.failure.observed, false,
    "overflow cannot expose a partial terminal signal as exact");

  const batches = [eventResponse([], 0)];
  for (let start = 0; start < specialized.length; start += 100) {
    const slice = complete.slice(start, start + 100);
    batches.push(eventResponse(slice, Math.min(start + 100, specialized.length)));
  }
  batches.push(eventResponse([complete.at(-1)], complete.length));
  const fresh = JSON.stringify({ ...base, source: "codex",
    ts: new Date(anchor + 60 * 60 * 1000).toISOString(), type: "tool_called",
    payload: { tool: "Read" } });
  batches.push(eventResponse([fresh], complete.length + 1, true));
  const incremental = runtimeWith(batches);
  for (let index = 0; index < batches.length - 1; index++) await incremental.poll();
  assert.deepEqual(read(incremental), expected);
  await incremental.poll();
  assert.equal(read(incremental).status, "not enough observed",
    "a genuinely new complete reset epoch recovers from durable overflow");

  const reset = runtimeWith([eventResponse([complete.at(-1)], 1),
    eventResponse(complete, complete.length, true)]);
  await reset.poll(); await reset.poll();
  assert.deepEqual(read(reset), expected);
});

test("grouped browser ingestion bounds a 150,000-heartbeat source epoch", async () => {
  const count = 150_000, anchor = Date.parse(fixture.now), agentId = "codex:large-grouped";
  const base = { v: 0, source: "codex", agent_id: agentId, project: "burrow" };
  const heartbeats = Array.from({ length: count }, (_, index) => JSON.stringify({ ...base,
    ts: new Date(anchor - count + index).toISOString(), type: "heartbeat",
    payload: { sequence: index } }));
  const oldAppendLastRoot = JSON.stringify({ ...base,
    ts: new Date(anchor - 3 * 60 * 60 * 1000).toISOString(), type: "task_started",
    payload: { prompt: "old append-last root" } });
  const grouped = runtimeWith([eventResponse([...heartbeats, oldAppendLastRoot], count + 1)]);
  const started = performance.now();
  await grouped.poll();
  const villager = grouped.snapshot().villagers.find(item => item.id === agentId);
  assert.ok(villager, "append-last root remains ordinary liveness evidence");
  assert.equal(villager.mood.anchor, new Date(anchor - 1).toISOString());
  assert.equal(villager.mood.status, "authority history uncertain");
  assert.deepEqual(grouped.snapshot().moodAuthority,
    { retained: 0, overflow: true, observed: 257 });
  assert.ok(performance.now() - started < 5000, "grouped overflow stays bounded and responsive");
});

test("grouped then incremental future anchor preserves an exposed terminal streak", async () => {
  const anchor = Date.parse(fixture.now), agentId = "codex:future-frontier";
  const observed = (hours, type, payload = {}) => JSON.stringify({ v: 0,
    ts: new Date(anchor + hours * 60 * 60 * 1000).toISOString(), source: "codex",
    agent_id: agentId, project: "burrow", type, payload });
  const initial = [observed(-10, "tool_failed", { tool: "early" }),
    observed(-23.5, "heartbeat"),
    ...[-23, -22.5, -22, -21.5].map((hours, index) =>
      observed(hours, "tool_failed", { tool: `frontier-${index}` })),
    observed(-1, "tool_called", { tool: "Read" }), observed(0, "idle")];
  const append = observed(2, "idle");
  const read = runtime => runtime.snapshot().villagers.find(villager =>
    villager.id === agentId).mood;

  const full = runtimeWith([eventResponse([...initial, append], initial.length + 1)]);
  await full.poll();
  const expected = read(full);
  assert.deepEqual([expected.signals.failure.streak, expected.signals.failure.failures,
    expected.enoughEvidence, expected.status], [2, 2, true, "watchful"]);

  const groupedThenIncremental = runtimeWith([
    eventResponse(initial, initial.length),
    eventResponse([append], initial.length + 1),
  ]);
  await groupedThenIncremental.poll(); await groupedThenIncremental.poll();
  assert.deepEqual(read(groupedThenIncremental), expected);
});

test("two-stage runtime keeps a bounded authoritative-decision predecessor", async () => {
  const at = Date.parse(fixture.now), agentId = "codex:capsule-runtime";
  const base = { v: 0, source: "codex", agent_id: agentId, project: "burrow" };
  const knock = (index, id, action = "deploy") => ({ ...base,
    ts: new Date(at - (500 - index) * 60000).toISOString(), type: "needs_human",
    payload: { message: id, request_id: id, action, detail: null,
      options: ["approve", "deny"] } });
  const close = (index, id) => ({ ...base, source: "steward",
    ts: new Date(at - (499 - index) * 60000).toISOString(), type: "needs_human_resolved",
    payload: { request_id: id, decision: "approve", decided_by: "human",
      action: "deploy" } });
  const first = [];
  for (let index = 0; index < 81; index++) {
    first.push(knock(index * 2, `q${index}`), close(index * 2, `q${index}`));
  }
  const later = Array.from({ length: 80 }, (_, offset) =>
    knock(340 + offset, `q${offset + 1}`, "erase"));
  const idle = { ...base, ts: new Date(at).toISOString(), type: "idle", payload: {} };
  later.push(idle);
  const read = runtime => runtime.snapshot().villagers.find(villager =>
    villager.id === agentId).mood;
  const grouped = runtimeWith([eventResponse([...first, ...later].map(JSON.stringify), 1)]);
  await grouped.poll();
  const staged = runtimeWith([eventResponse(first.map(JSON.stringify), 1),
    eventResponse(later.map(JSON.stringify), 2)]);
  await staged.poll(); await staged.poll();
  assert.deepEqual(read(staged), read(grouped));
  assert.equal(read(staged).status, "authority history uncertain");
  assert.equal(read(staged).signals.interaction.kind, null,
    "overflow never presents a retained partial decision as exact");
});

test("grouped Mood order survives a departed unrelated raw prefix", async () => {
  const at = Date.parse(fixture.now);
  const unrelated = { v: 0, ts: new Date(at - 20_000).toISOString(), source: "codex",
    agent_id: "codex:departed", project: "burrow", type: "session_ended", payload: {} };
  const oldRoot = { ...unrelated, ts: new Date(at - 10_000).toISOString(),
    agent_id: "codex:target", type: "task_started", payload: { prompt: "old" } };
  const latestRoot = { ...oldRoot, ts: new Date(at - 5_000).toISOString(),
    payload: { prompt: "latest" } };
  const idle = { ...latestRoot, ts: new Date(at).toISOString(), type: "idle", payload: {} };
  const capsule = JSON.stringify({ _burrow_internal: "mood-authority-v1",
    events: [latestRoot], ordinals: ["12"], copies: ["12"],
    raw_ordinals: ["10", "11", "12", "13"],
    raw_indexes: ["0000000000000000", "0000000000000001",
      "0000000000000002", "0000000000000003"],
    raw_count: "0000000000000004", overflow: false, observed: 1 });
  const grouped = runtimeWith([eventResponse([capsule, ...[unrelated, oldRoot, latestRoot, idle]
    .map(JSON.stringify)], 4)]);
  await grouped.poll();
  const snapshot = grouped.snapshot();
  assert.equal(snapshot.villagers.some(item => item.id === "codex:departed"), false);
  const mood = snapshot.villagers.find(item => item.id === "codex:target").mood;
  assert.equal(mood.signals.interaction.kind, "root prompt");
  assert.equal(mood.signals.interaction.logAgeMs, 5000,
    "projecting away the prefix preserves the newest copied interaction ordinal");
});

test("grouped authority remains globally owned across unrelated incremental appends and reset", async () => {
  const data = JSON.parse(fs.readFileSync(path.join(__dirname,
    "fixtures/mood-grouped-unrelated.json")));
  const initial = data.initial.map(JSON.stringify), unrelated = data.unrelated.map(JSON.stringify);
  const mood = runtime => runtime.snapshot().villagers.find(item =>
    item.id === "codex:resident-a").mood;
  const groupedThenIncremental = runtimeWith([
    eventResponse(initial, initial.length),
    eventResponse([unrelated[0]], initial.length + 1),
    eventResponse([unrelated[1]], initial.length + 2),
  ]);
  await groupedThenIncremental.poll();
  const expected = mood(groupedThenIncremental);
  assert.equal(expected.signals.interaction.kind, "root prompt");
  assert.equal(expected.signals.interaction.logAgeMs, 0);
  await groupedThenIncremental.poll();
  assert.deepEqual(mood(groupedThenIncremental), expected,
    "an unrelated changed agent cannot consume global capsule coordinates");
  await groupedThenIncremental.poll();
  assert.deepEqual(mood(groupedThenIncremental), expected,
    "repeated unrelated agents cannot reorder retained resident authority");

  const reset = runtimeWith([
    eventResponse([unrelated[0]], 1),
    eventResponse([...initial, ...unrelated], initial.length + unrelated.length, true),
  ]);
  await reset.poll(); await reset.poll();
  assert.deepEqual(mood(reset), expected);
});

test("4,001 sequential authority facts stay fixed-space and conservatively match grouped overflow", async () => {
  const count = 4001, at = Date.parse(fixture.now), agentId = "codex:authority-pressure";
  const base = { v: 0, source: "codex", agent_id: agentId, project: "burrow" };
  const pressure = Array.from({ length: count }, (_, index) => ({ ...base,
    ts: new Date(at - count + index).toISOString(), type: "needs_human",
    payload: { message: `Request ${index}`, request_id: `bounded-${index}`,
      action: "deploy", detail: null, options: ["approve", "deny"] } }));
  const pressureLines = pressure.map(JSON.stringify);
  const grouped = runtimeWith([eventResponse(pressureLines, count)]);
  await grouped.poll();

  const incremental = runtimeWith(pressureLines.map((line, index) =>
    eventResponse([line], index + 1)));
  const started = performance.now();
  for (let index = 0; index < pressureLines.length; index++) await incremental.poll();
  const elapsed = performance.now() - started;
  assert.ok(elapsed < 10_000, `bounded sequential fold took ${elapsed.toFixed(0)}ms`);
  assert.deepEqual(incremental.snapshot().villagers[0].mood,
    grouped.snapshot().villagers[0].mood);
  assert.equal(incremental.snapshot().villagers[0].mood.status,
    "authority history uncertain");
  assert.deepEqual(incremental.snapshot().moodAuthority,
    { retained: 0, overflow: true, observed: 257 });
});

test("grouped fifty-thousand structured facts saturate browser authority in bounded time", async () => {
  const count = 50_000, at = Date.parse(fixture.now), agentId = "codex:grouped-pressure";
  const base = { v: 0, source: "codex", agent_id: agentId, project: "burrow" };
  const pressure = Array.from({ length: count }, (_, index) => JSON.stringify({ ...base,
    ts: new Date(at - count + index).toISOString(), type: "needs_human",
    payload: { message: `Request ${index}`, request_id: `grouped-${index}`,
      action: "deploy", detail: null, options: ["approve", "deny"] } }));
  const appended = JSON.stringify({ ...base, ts: new Date(at + 1).toISOString(),
    type: "idle", payload: {} });
  const grouped = runtimeWith([eventResponse(pressure, count),
    eventResponse([appended], count + 1)]);
  const started = performance.now();
  await grouped.poll();
  const elapsed = performance.now() - started;
  assert.ok(elapsed < 5000, `grouped structured fold took ${elapsed.toFixed(0)}ms`);
  assert.equal(grouped.snapshot().villagers[0].mood.status, "authority history uncertain");
  assert.deepEqual(grouped.snapshot().moodAuthority,
    { retained: 0, overflow: true, observed: 257 });
  await grouped.poll();
  assert.equal(grouped.snapshot().villagers[0].mood.status, "authority history uncertain",
    "ordinary grouped continuation cannot clear durable uncertainty");
  assert.deepEqual(grouped.snapshot().moodAuthority,
    { retained: 0, overflow: true, observed: 257 });
});

test("discarded repeated roots occupy one compact grouped/incremental authority slot", async () => {
  const at = Date.parse(fixture.now), count = 257, agentId = "codex:root-pressure";
  const roots = Array.from({ length: count }, (_, index) => JSON.stringify({
    v: 0, ts: new Date(at - count + index).toISOString(), source: "codex",
    agent_id: agentId, project: "burrow", type: "task_started",
    payload: { prompt: `Root ${index}` },
  }));
  const grouped = runtimeWith([eventResponse(roots, count)]);
  await grouped.poll();
  const incremental = runtimeWith(roots.map((line, index) =>
    eventResponse([line], index + 1)));
  for (let index = 0; index < roots.length; index++) await incremental.poll();
  assert.deepEqual(incremental.snapshot().villagers[0].mood,
    grouped.snapshot().villagers[0].mood);
  assert.equal(incremental.snapshot().villagers[0].mood.status,
    "not enough observed");
  assert.deepEqual(incremental.snapshot().moodAuthority,
    { retained: 1, overflow: false, observed: 1 });
});
