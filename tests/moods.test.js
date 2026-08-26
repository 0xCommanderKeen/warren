"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const projection = require("../viewer/projection.js");
const moods = require("../viewer/moods.js");
const glyph = require("../viewer/mood-glyph.js");
const typedJSON = require("../viewer/typed-json.js");
const lifecycleAmbiguity = require("./fixtures/mood-lifecycle-ambiguity.json");
const capsuleParity = require("./fixtures/mood-capsule-parity.json");
const futureSufficiency = require("./fixtures/mood-future-sufficiency.json");

const BASE = Date.parse("2026-08-25T12:00:00.000Z");
function event(minute, type, payload = {}, extra = {}) {
  return { v: 0, ts: new Date(BASE + minute * 60000).toISOString(), source: "codex",
    agent_id: "codex:a", project: "burrow", type, payload, ...extra };
}
function eventMs(milliseconds, type, payload = {}, extra = {}) {
  return { v: 0, ts: new Date(BASE + milliseconds).toISOString(), source: "codex",
    agent_id: "codex:a", project: "burrow", type, payload, ...extra };
}
const validated = events => projection.parseEvents(events);
const one = events => moods.deriveMoods(validated(events)).get("codex:a");
const work = (minute, type = "tool_called", payload = { tool: "Read" }) => event(minute, type, payload);
const knock = (minute, id = "r", overrides = {}) => event(minute, "needs_human", {
  message: "Choose", request_id: id, action: "deploy", detail: null,
  options: ["approve", "deny"], ...overrides });
const close = (minute, id = "r", overrides = {}) => event(minute, "needs_human_resolved", {
  request_id: id, decision: "approve", decided_by: "human", action: "deploy", ...overrides },
  { source: "steward" });

test("anchor, rolling failures, boundaries, buckets, points and precedence are exact", () => {
  const history = [
    event(-1440, "tool_failed", { tool: "old" }), // excluded at open 24h edge
    event(-120, "task_started", { prompt: "root" }),
    work(-119), work(-90), work(-60), work(-30),
    event(-20, "tool_failed", { tool: "A" }),
    event(-10, "tool_failed", { tool: "B" }),
    event(0, "tool_failed", { tool: "C" }),
  ];
  const mood = one(history);
  assert.equal(mood.anchor, "2026-08-25T12:00:00.000Z");
  assert.deepEqual(mood.signals.failure, { observed: true, streak: 3, failures: 3,
    failuresLabel: "3+" });
  assert.equal(mood.glyph, "×");
  assert.equal(mood.status, "repeated failures");
  assert.equal(mood.signals.interaction.level, "recent");
  assert.equal(mood.signals.workload.level, "light");

  const saturated = one(Array.from({ length: 8 }, (_, index) =>
    event(-105 + index * 15, "task_started", { prompt: `p${index}` })));
  assert.equal(saturated.signals.workload.density, 24);
  assert.equal(saturated.signals.workload.level, "saturated");
  assert.equal(saturated.glyph, "▲");

  const interrupted = one([event(-50, "tool_failed", { tool: "A" }),
    event(-40, "heartbeat"), event(-30, "tool_failed", { tool: "B" }),
    event(-20, "heartbeat"), event(0, "tool_failed", { tool: "C" })]);
  assert.deepEqual(interrupted.signals.failure, { observed: true, streak: 1,
    failures: 3, failuresLabel: "3+" });
});

test("evidence threshold, score ranges, equality boundaries and deterministic wall time", () => {
  const enough = [event(-75, "task_started", { prompt: "root" }), work(-60),
    work(-45), work(-30), work(-15), event(0, "heartbeat")];
  const mood = one(enough);
  assert.equal(mood.enoughEvidence, true);
  assert.equal(mood.signals.interaction.level, "recent");
  assert.equal(mood.glyph, "●");
  assert.deepEqual(moods.deriveMoods(validated(enough)), moods.deriveMoods(validated(enough)));
  assert.equal(one([work(0)]).status, "not enough observed");

  const boundary = one([event(-1440, "task_started", { prompt: "root" }), knock(-360, "edge"), work(0)]);
  assert.equal(boundary.signals.interaction.level, "aging", "exact 24h is aging");
  assert.equal(boundary.signals.unresolvedNeed.state, ">1–6h", "exact 6h stays in lower-risk range");
});

test("all age and workload bucket boundaries stay in their specified lower-risk range", () => {
  const anchor = () => event(0, "idle");
  for (const [minutes, level] of [[-360, "recent"], [-361, "aging"],
    [-1440, "aging"], [-1441, "old"]]) {
    assert.equal(one([event(minutes, "task_started", { prompt: "human" }), anchor()])
      .signals.interaction.level, level);
  }
  for (const [minutes, state] of [[-60, "≤1h"], [-61, ">1–6h"],
    [-360, ">1–6h"], [-361, ">6h"]]) {
    assert.equal(one([knock(minutes, `need-${minutes}`), anchor()])
      .signals.unresolvedNeed.state, state);
  }
  function weighted(minute, weight, index) {
    if (weight === 3) return event(minute, "task_started", { prompt: `custom-${index}` }, { source: "fixture" });
    if (weight === 2) return event(minute, "artifact_produced", { artifact: `a-${index}` });
    return work(minute);
  }
  function density(weights) {
    const start = -(weights.length - 1) * 15;
    return one(weights.map((weight, index) => weighted(start + index * 15, weight, index)))
      .signals.workload;
  }
  for (const [weights, score, level] of [
    [[1,1,1,1,1,1], 6, "light"], [[1,1,1,1,1,2], 7, "active"],
    [[3,3,3,3,2], 14, "active"], [[3,3,3,3,3], 15, "heavy"],
    [[3,3,3,3,3,3,2], 20, "heavy"], [[3,3,3,3,3,3,3], 21, "saturated"],
  ]) assert.deepEqual([density(weights).density, density(weights).level], [score, level]);
});

test("precedence and total score select every public result", () => {
  const steady = [event(-75, "task_started", { prompt: "root" }), work(-60),
    work(-45), work(-30), work(-15), event(0, "heartbeat")];
  assert.deepEqual([one(steady).glyph, one(steady).score], ["●", 5]);
  const active = [event(-90, "tool_failed", { tool: "old" }),
    ...[-75,-60,-45,-30,-15].map(minute => work(minute))];
  active.push(event(0, "artifact_produced", { artifact: "sixth" }));
  assert.deepEqual([one(active).glyph, one(active).score], ["◆", 1]);
  assert.equal(one([knock(0)]).glyph, "◇");
  assert.equal(one([work(0)]).glyph, "?");

  const failures = [event(-20, "tool_failed", { tool: "A" }),
    event(-10, "tool_failed", { tool: "B" }), event(0, "tool_failed", { tool: "C" })];
  const saturated = Array.from({length:8}, (_, index) =>
    event(-105 + index * 15, "task_started", {prompt:`p${index}`}, {source:"fixture"}));
  assert.equal(one([...saturated, ...failures]).status, "repeated failures");
  assert.equal(one([...saturated, ...failures, knock(-500)]).status, "blocked");
});

test("approval authority covers exact close, collisions, malformed fallback, plain supersession and orphans", () => {
  const pending = one([knock(-361), work(0)]);
  assert.equal(pending.glyph, "!");
  assert.equal(pending.signals.unresolvedNeed.kind, "structured request");
  const resolved = one([knock(-500), close(-400), work(0)]);
  assert.equal(resolved.signals.unresolvedNeed.observed, false);
  assert.equal(resolved.signals.interaction.kind, "approval decision");

  const collision = one([knock(-500, "same"), knock(-400, "same", { action: "erase" }),
    close(-300, "same"), work(0)]);
  assert.equal(collision.signals.unresolvedNeed.kind, "structured collision");
  assert.equal(collision.glyph, "!");
  const tied = one([knock(-400, "first"), knock(-400, "second"), work(0)]);
  assert.equal(tied.signals.unresolvedNeed.request_id, "first", "equal age chooses earlier append");
  const orphan = one([close(-10, "unknown"), work(0)]);
  assert.equal(orphan.signals.interaction.observed, false);

  const malformed = event(-20, "needs_human", { message: "fallback", request_id: "bad" });
  assert.equal(one([malformed]).signals.unresolvedNeed.kind, "fallback knock");
  assert.equal(one([malformed, work(0)]).signals.unresolvedNeed.observed, false);
  const plain = event(-20, "needs_human", { message: "legacy" });
  assert.equal(one([plain]).signals.unresolvedNeed.kind, "plain knock");
  assert.equal(one([plain, event(-10, "routine_finished", { routine: "x", run_id: "1",
    outcome: "ok", artifacts: [], duration_s: 1 }, { source: "steward" })])
    .signals.unresolvedNeed.observed, true, "specialized lifecycle does not supersede plain knock");
});

test("prompt provenance, visitors and strict-invalid records use the same pure rule", () => {
  const child = event(-60, "task_started", { prompt: "child", parent_agent_id: "codex:p" });
  const custom = event(-50, "task_started", { prompt: "custom" }, { source: "fixture" });
  const root = event(-40, "task_started", { prompt: "human" }, { source: "claude-code" });
  assert.equal(one([child, custom]).signals.interaction.observed, false);
  assert.equal(one([child, custom, root]).signals.interaction.kind, "root prompt");
  const invalid = { ...work(0), v: 99 };
  assert.equal(moods.deriveMoods([invalid]).size, 0);
  assert.equal(one([work(0)]).agent_id, "codex:a", "derivation has no residency branch");
});

test("typed authority preserves random finite binary64 patterns and normalizes exceptional numbers", () => {
  for (const bits of capsuleParity.finite_binary64_bits) {
    const bytes = Uint8Array.from(bits.match(/../g), byte => Number.parseInt(byte, 16));
    const value = new DataView(bytes.buffer).getFloat64(0, false);
    assert.equal(typedJSON.numberBits(value), bits);
    assert.equal(typedJSON.decodeGraph(typedJSON.typedGraph(value)), value);
  }
  assert.equal(typedJSON.numberBits(-0), "0000000000000000");
  assert.equal(Object.is(typedJSON.decodeGraph(typedJSON.typedGraph(-0)), -0), false);
  for (const value of [Infinity, -Infinity, NaN, JSON.parse("1e400")]) {
    assert.equal(typedJSON.numberBits(value), "nonfinite");
    assert.equal(typedJSON.decodeGraph(typedJSON.typedGraph(value)), Infinity);
  }
  for (const value of [1.2345678901234567, Number.MIN_VALUE, Number.MAX_VALUE,
    Number.MIN_NORMAL || 2.2250738585072014e-308]) {
    assert.equal(typedJSON.decodeGraph(typedJSON.typedGraph(value)), value);
  }
});

test("sparse capsule manifests exclude other projections but admit later appends", () => {
  const root = event(-60, "task_started", { prompt: "human" });
  const unrelatedPlain = knock(-120, "not-structured", { request_id: undefined,
    action: undefined, options: undefined });
  delete unrelatedPlain.payload.request_id;
  delete unrelatedPlain.payload.action;
  delete unrelatedPlain.payload.options;
  const anchor = event(0, "idle");
  const capsule = JSON.stringify({
    _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: [root], ordinals: ["1"], copies: ["1"],
    raw_ordinals: ["1", "3"],
    raw_indexes: ["0000000000000000", "0000000000000002"],
    raw_count: "0000000000000003", overflow: false, observed: 1,
  });
  const retained = projection.parseEvents([capsule, root, unrelatedPlain, anchor]);
  assert.equal(moods.deriveMoods(retained).get("codex:a").signals.unresolvedNeed.observed,
    false, "unmanifested raw from the rotated epoch belongs only to other projections");
  const laterPlain = event(1, "needs_human", { message: "new append" });
  const appended = projection.parseEvents([capsule, root, unrelatedPlain, anchor, laterPlain]);
  assert.equal(moods.deriveMoods(appended).get("codex:a").signals.unresolvedNeed.observed,
    true, "records appended after raw_count enter Mood normally");
});

test("sparse capsule completeness rejects omitted or substituted Mood witnesses", () => {
  const authority = { ...event(-200, "task_started", { prompt: "authority" }),
    agent_id: "codex:authority" };
  const cases = [
    [event(-60, "tool_failed", { tool: "Bash" }), event(0, "routine_finished",
      { routine: "r", run_id: "r1", outcome: "ok", artifacts: [], duration_s: 1 },
      { source: "steward" })],
    [event(-60, "task_started", { prompt: "new root" }), event(0, "routine_finished",
      { routine: "r", run_id: "r2", outcome: "ok", artifacts: [], duration_s: 1 },
      { source: "steward" })],
    [knock(-60, "pending"), event(0, "routine_finished",
      { routine: "r", run_id: "r3", outcome: "ok", artifacts: [], duration_s: 1 },
      { source: "steward" })],
    [event(-60, "task_claimed", { task_id: "t", title: "T", claimant: "codex:a" }),
      event(0, "routine_finished", { routine: "r", run_id: "r4", outcome: "ok",
        artifacts: [], duration_s: 1 }, { source: "steward" })],
  ];
  for (const raw of cases) {
    const capsule = JSON.stringify({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
      events: [authority], ordinals: ["0"], copies: [], raw_ordinals: ["2"],
      raw_indexes: ["0000000000000001"], raw_count: "0000000000000002",
      overflow: false, observed: 1 });
    const attacked = projection.parseEvents([capsule, ...raw]);
    assert.deepEqual(Object.fromEntries(moods.deriveMoods(attacked)),
      Object.fromEntries(moods.deriveMoods(projection.parseEvents(raw))),
      `${raw[0].type} cannot be suppressed by an incomplete sparse list`);
  }

  const oldIdle = event(-120, "idle"), anchor = event(0, "heartbeat");
  const complete = JSON.stringify({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: [authority], ordinals: ["0"], copies: [], raw_ordinals: ["2"],
    raw_indexes: ["0000000000000001"], raw_count: "0000000000000002",
    overflow: false, observed: 1 });
  const sparse = projection.parseEvents([complete, oldIdle, anchor]);
  assert.equal(moods.deriveMoods(sparse).get("codex:a").anchor, anchor.ts,
    "a co-retained older record not selected by Mood remains legitimately sparse");
});

test("sparse capsule completeness rejects surplus raw indexes exactly", () => {
  const authority = { ...event(-240, "task_started", { prompt: "unrelated authority" }),
    agent_id: "codex:authority" };
  const plain = knock(-120, "plain", { request_id: undefined, action: undefined,
    options: undefined });
  delete plain.payload.request_id;
  delete plain.payload.action;
  delete plain.payload.options;
  const idle = event(-60, "idle");
  const terminal = event(0, "routine_finished", { routine: "r", run_id: "r-surplus",
    outcome: "ok", artifacts: [], duration_s: 1 }, { source: "steward" });
  // Canonical selection requires only terminal (index 2). Manifesting the old
  // plain knock as well while omitting its later superseder would resurrect a
  // false unresolved need if a merely-subset manifest were trusted.
  const capsule = JSON.stringify({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: [authority], ordinals: ["0"], copies: [], raw_ordinals: ["1", "3"],
    raw_indexes: ["0000000000000000", "0000000000000002"],
    raw_count: "0000000000000003", overflow: false, observed: 1 });
  const raw = projection.parseEvents([plain, idle, terminal]);
  const attacked = projection.parseEvents([capsule, plain, idle, terminal]);
  assert.equal(projection.moodAuthority(attacked).length, 1,
    "wire parser accepts the structurally valid capsule before semantic proof");
  assert.deepEqual(Object.fromEntries(moods.deriveMoods(attacked)),
    Object.fromEntries(moods.deriveMoods(raw)),
    "derive rejects the whole surplus manifest and preserves all raw evidence");
  assert.equal(moods.deriveMoods(attacked).get("codex:a").signals.unresolvedNeed.observed,
    false);
});

test("capsule authority must equal the exact canonical source-epoch fold", () => {
  const request = knock(-30, "exact-fold");
  const resolution = close(-20, "exact-fold");
  const encoded = changes => JSON.stringify({
    _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: [request], ordinals: ["5"], copies: [], raw_ordinals: ["10"],
    raw_indexes: ["0000000000000000"], raw_count: "0000000000000001",
    overflow: false, observed: 1, ...changes,
  });
  const injectedClose = projection.parseEvents([encoded({}), resolution]);
  assert.equal(projection.moodAuthority(injectedClose).length, 1,
    "structural parsing alone does not grant semantic authority");
  assert.deepEqual(Object.fromEntries(moods.deriveMoods(injectedClose)),
    Object.fromEntries(moods.deriveMoods(projection.parseEvents([resolution]))),
    "an append-interleaved close omitted from the claimed fold rejects the capsule");

  const omittedCopy = projection.parseEvents([encoded({ raw_ordinals: [], raw_indexes: [] }),
    request]);
  assert.deepEqual(Object.fromEntries(moods.deriveMoods(omittedCopy)),
    Object.fromEntries(moods.deriveMoods(projection.parseEvents([request]))),
    "raw irreducible authority omitted from copies/manifest rejects atomically");

  const plain = event(-40, "needs_human", { message: "legacy plain knock" });
  const orphan = close(-10, "orphan");
  for (const [name, authorityEvent] of [["plain/fallback knock", plain],
    ["proven orphan close", orphan]]) {
    const line = encoded({ events: [authorityEvent], ordinals: ["5"], copies: [],
      raw_ordinals: [], raw_indexes: [], raw_count: "0000000000000000" });
    const parsed = projection.parseEvents([line]);
    assert.equal(projection.moodAuthority(parsed).length, 1);
    assert.equal(moods.deriveMoods(parsed).size, 0,
      `${name} is not irreducible capsule authority`);
  }

  const reordered = projection.parseEvents([encoded({ events: [orphan, request],
    ordinals: ["4", "5"], observed: 2, raw_ordinals: [], raw_indexes: [],
    raw_count: "0000000000000000" })]);
  assert.equal(moods.deriveMoods(reordered).size, 0,
    "SAFE ordinal reconstruction rejects a fold containing an earlier orphan");
});

test("safe ordinal exhaustion overflows before allocation but capsule-only rotations remain exact", () => {
  const request = knock(-20, "max-safe"), resolution = close(-10, "max-safe");
  const capsule = JSON.stringify({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: [request, resolution],
    ordinals: [String(Number.MAX_SAFE_INTEGER - 1), String(Number.MAX_SAFE_INTEGER)],
    copies: [], raw_ordinals: [], raw_indexes: [], raw_count: "0000000000000000",
    overflow: false, observed: 2 });
  const once = moods.retainMoodWitnesses(projection.parseEvents([capsule]));
  const twice = moods.retainMoodWitnesses(once);
  assert.equal(projection.moodAuthorityState(once).overflow, false);
  assert.equal(projection.moodAuthorityState(twice).overflow, false,
    "capsule-only resolved authority allocates no coordinate on either rotation");
  const appended = projection.parseEvents([capsule, event(0, "heartbeat")]);
  assert.equal(moods.deriveMoods(appended).get("codex:a").status,
    "authority history uncertain");
  assert.equal(projection.moodAuthorityState(moods.retainMoodWitnesses(appended)).overflow, true);
});

test("retained witnesses are bounded, append ordered and byte-equivalent", () => {
  const history = [];
  for (let index = 0; index < 180; index++) history.push(work(index - 180));
  history.push(knock(-400, "kept"), event(0, "heartbeat"));
  const parsed = validated(history);
  const before = one(history);
  const retained = moods.retainMoodWitnesses(parsed);
  const after = moods.deriveMoods(retained).get("codex:a");
  assert.deepEqual(after, before);
  assert.ok(retained.length <= moods.MAX_RETAINED_PER_AGENT);
  assert.deepEqual(retained, parsed.filter(event => retained.includes(event)), "append order retained");

  const interrupted = [event(-100, "tool_failed", { tool: "first" }),
    event(-90, "heartbeat"), event(-80, "tool_failed", { tool: "second" }),
    event(-70, "heartbeat"), event(-60, "tool_failed", { tool: "third" }),
    event(-50, "heartbeat"), event(-40, "task_done", {
      task_id: "t", title: "T", claimant: "codex:a", summary: "done" }),
    event(0, "heartbeat")];
  assert.deepEqual(one(moods.retainMoodWitnesses(validated(interrupted))), one(interrupted),
    "rotation retains all failures needed for the independent rolling count");

  const finishes = Array.from({ length: 6 }, (_, index) => event(-100 + index * 10,
    "routine_finished", { routine: "r", run_id: `run-${index}`, outcome: "ok",
      artifacts: [], duration_s: 1 }, { source: "steward" }));
  const orphans = Array.from({ length: 12 }, (_, index) => close(-30 + index, `orphan-${index}`));
  const adversarial = [event(-110, "task_started", { prompt: "root" }), ...finishes,
    ...orphans, event(0, "idle")];
  assert.equal(one(adversarial).enoughEvidence, true);
  assert.deepEqual(one(moods.retainMoodWitnesses(validated(adversarial))), one(adversarial),
    "orphan resolution types cannot displace actual sufficiency contributors");
});

test("future supersession retains a six-witness proof without unresolved needs", () => {
  const initial = validated(futureSufficiency.initial);
  const grouped = moods.retainMoodWitnesses(initial);
  const incremental = projection.parseEvents([...grouped, futureSufficiency.append]);
  const full = validated([...futureSufficiency.initial, futureSufficiency.append]);
  const expected = moods.deriveMoods(full).get(futureSufficiency.agent_id);
  assert.deepEqual([expected.glyph, expected.status, expected.evidence],
    ["◆", "active", { count: 6, spanMs: 30 * 60 * 1000 }]);
  assert.equal(expected.signals.unresolvedNeed.observed, false);
  assert.deepEqual(moods.deriveMoods(incremental).get(futureSufficiency.agent_id), expected,
    "the append-later idle cannot consume the only retained sufficiency witness");
  const once = moods.retainMoodWitnesses(incremental);
  const twice = moods.retainMoodWitnesses(once);
  assert.deepEqual(moods.deriveMoods(once).get(futureSufficiency.agent_id), expected);
  assert.deepEqual(moods.deriveMoods(twice).get(futureSufficiency.agent_id), expected);
  assert.equal(JSON.stringify([...twice]), JSON.stringify([...once]),
    "repeated bounded rotations retain the exact same public witness bytes");
});

test("timestamp-disordered future anchors preserve the complete terminal frontier", () => {
  const initial = [
    event(-10 * 60, "tool_failed", { tool: "early" }),
    event(-23.5 * 60, "heartbeat"),
    ...[-23, -22.5, -22, -21.5].map((hours, index) =>
      event(hours * 60, "tool_failed", { tool: `frontier-${index}` })),
    work(-60), event(0, "idle"),
  ];
  const append = event(2 * 60, "idle");
  const compacted = moods.retainMoodWitnesses(validated(initial));
  const incremental = projection.parseEvents([...compacted, append]);
  const full = validated([...initial, append]);
  const expected = moods.deriveMoods(full).get("codex:a");
  assert.deepEqual([expected.signals.failure.streak, expected.signals.failure.failures,
    expected.enoughEvidence, expected.status], [2, 2, true, "watchful"]);
  assert.deepEqual(moods.deriveMoods(incremental).get("codex:a"), expected);
  assert.deepEqual(moods.deriveMoods(moods.retainMoodWitnesses(incremental)).get("codex:a"),
    expected, "a repeated rotation cannot lose an outcome exposed by the new anchor");
});

test("terminal witness union is exact at 160 and durably uncertain at 161", () => {
  const frontier = count => [eventMs(-24 * moods.HOUR, "task_done", {
    task_id: "lower-open", title: "excluded", claimant: "codex:a", summary: "old" }),
  ...Array.from({ length: count }, (_, index) => eventMs(
    -24 * moods.HOUR + (index + 1) * 24 * moods.HOUR / count,
    "heartbeat", { sequence: index }))];
  const exact = validated(frontier(160));
  const exactRetained = moods.retainMoodWitnesses(exact);
  assert.equal(projection.moodAuthorityState(exactRetained).overflow, false);
  assert.equal(exactRetained.length, 160, "the exact lower boundary is excluded");
  assert.deepEqual(moods.deriveMoods(exactRetained), moods.deriveMoods(exact));

  const excess = validated(frontier(161));
  const excessRetained = moods.retainMoodWitnesses(excess);
  assert.equal(projection.moodAuthorityState(excessRetained).overflow, true);
  assert.equal(moods.deriveMoods(excess).get("codex:a").status,
    "authority history uncertain", "full derivation uses the retention ceiling");
  const repeated = moods.retainMoodWitnesses(excessRetained);
  assert.equal(projection.moodAuthorityState(repeated).overflow, true);
  assert.equal(JSON.stringify([...repeated]), JSON.stringify([...excessRetained]));

  const freshEpoch = moods.retainMoodWitnesses(validated([event(0, "heartbeat")]));
  assert.equal(projection.moodAuthorityState(freshEpoch).overflow, false,
    "only a new complete source epoch recovers from witness overflow");
  assert.equal(moods.deriveMoods(freshEpoch).get("codex:a").status, "not enough observed");
});

test("a forged non-overflow capsule cannot truncate a 161-witness source epoch", () => {
  const heartbeats = Array.from({ length: 161 }, (_, index) => eventMs(
    -160_000 + index * 1000, "heartbeat", { sequence: index }));
  const oldAppendLastRoot = event(-180, "task_started", { prompt: "old root" });
  const raw = [...heartbeats, oldAppendLastRoot];
  // This is the tempting truncated proof: the authority copy plus only 159 of
  // the 161 terminal witnesses. It is structurally valid transport metadata,
  // but semantically cannot claim a complete source epoch.
  const indexes = [...Array.from({ length: 159 }, (_, index) => index), 161];
  const capsule = JSON.stringify({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: [oldAppendLastRoot], ordinals: ["161"], copies: ["161"],
    raw_ordinals: indexes.map(String),
    raw_indexes: indexes.map(index => String(index).padStart(16, "0")),
    raw_count: "0000000000000162", overflow: false, observed: 1 });
  const proof = moods.requiredMoodRawIndexes(validated(raw), [oldAppendLastRoot]);
  assert.equal(proof.witnessOverflow, true);

  const attacked = projection.parseEvents([capsule, ...raw]);
  assert.equal(projection.moodAuthorityState(attacked).overflow, false,
    "the encoded attack is structurally valid and reaches semantic validation");
  const full = moods.deriveMoods(validated(raw)).get("codex:a");
  const parsed = moods.deriveMoods(attacked).get("codex:a");
  assert.equal(full.status, "authority history uncertain");
  assert.deepEqual(parsed, full, "atomic rejection preserves every raw source record");
  const rotated = moods.retainMoodWitnesses(attacked);
  assert.equal(projection.moodAuthorityState(rotated).overflow, true,
    "the truthful producer emits durable overflow after rejecting the forgery");
  assert.deepEqual(moods.deriveMoods(rotated).get("codex:a"), full);
  const appended = moods.combineMoodEvidence(rotated, validated([event(1, "idle")]));
  assert.equal(moods.deriveMoods(appended).get("codex:a").status,
    "authority history uncertain", "ordinary append cannot clear the source epoch overflow");
  assert.deepEqual(moods.retainMoodWitnesses(rotated), rotated,
    "reset/rotation preserves the canonical overflow capsule");
});

test("four-thousand terminal events overflow in bounded time without partial signals", () => {
  const history = validated(Array.from({ length: 4001 }, (_, index) => eventMs(
    -23 * moods.HOUR + index * 1000, index % 5 ? "heartbeat" : "tool_failed",
    index % 5 ? { sequence: index } : { tool: `tool-${index}` })));
  const started = performance.now();
  const mood = moods.deriveMoods(history).get("codex:a");
  assert.ok(performance.now() - started < 2500, "frontier overflow remains bounded and fast");
  assert.equal(mood.status, "authority history uncertain");
  assert.deepEqual(mood.signals.failure, { observed: false, streak: null, failures: null,
    failuresLabel: "authority history uncertain" });
});

test("one-hundred-fifty-thousand heartbeats overflow without spread or retained growth", () => {
  const history = validated(Array.from({ length: 150_000 }, (_, index) => eventMs(
    -23 * moods.HOUR + index * 500, "heartbeat", { sequence: index })));
  const started = performance.now();
  const mood = moods.deriveMoods(history).get("codex:a");
  const retained = moods.retainMoodWitnesses(history);
  assert.equal(mood.status, "authority history uncertain");
  assert.equal(projection.moodAuthorityState(retained).overflow, true);
  assert.ok(retained.length <= moods.MAX_RETAINED_PER_AGENT,
    `overflow attached ${retained.length} records`);
  assert.ok(performance.now() - started < 5000, "large frontier remains iterative and bounded");
});

test("global overflow bounds cross-agent collision attachments per owner", () => {
  const collision = (index, agentId, action) => ({
    ...knock(index / 60, `shared-${index}`, {
      action, message: `collision ${index}` }),
    agent_id: agentId,
  });
  const history = [];
  for (let index = 0; index < 300; index++) {
    history.push(collision(index, "codex:hub", "deploy"));
    history.push(collision(index, `codex:temporary-${index}`, "erase"));
  }
  const retained = moods.retainMoodWitnesses(validated(history));
  const counts = new Map();
  for (const item of retained) {
    counts.set(item.agent_id, (counts.get(item.agent_id) || 0) + 1);
  }
  assert.ok(Math.max(...counts.values()) <= moods.MAX_RETAINED_PER_AGENT);
  assert.equal(projection.moodAuthorityState(retained).overflow, true);
  assert.equal(moods.deriveMoods(retained).get("codex:hub").status,
    "authority history uncertain");
  const freshEpoch = moods.retainMoodWitnesses(validated([
    event(0, "heartbeat", {}, { agent_id: "codex:hub" })]));
  assert.equal(projection.moodAuthorityState(freshEpoch).overflow, false,
    "a genuinely new complete source epoch remains reclaimable");
});

test("rotation preserves append-later plain supersession despite timestamp order", () => {
  const history = [event(399, "needs_human", { message: "legacy" }),
    event(-279, "routine_failed", { routine: "nightly", run_id: "r", error: "boom" },
      { source: "steward" }), event(300, "idle")];
  const before = one(history);
  assert.equal(before.anchor, "2026-08-25T18:39:00.000Z");
  assert.equal(before.signals.unresolvedNeed.observed, false);
  const retained = moods.retainMoodWitnesses(validated(history));
  assert.ok(retained.some(item => item.type === "idle"),
    "append-later ordinary boundary accompanies the retained timestamp anchor");
  assert.deepEqual(one(retained), before);

  const chained = [event(399, "needs_human", { message: "first" }),
    event(350, "needs_human", { message: "second" }), event(300, "idle")];
  assert.deepEqual(one(moods.retainMoodWitnesses(validated(chained))), one(chained),
    "the final append-order boundary closes every retained plain knock in a chain");
});

test("bounded-complete Mood authority keeps the oldest pending lifecycle", () => {
  const history = [knock(-480, "oldest")];
  for (let index = 0; index < 40; index++) history.push(knock(-39 + index, `recent-${index}`));
  const before = one(history);
  assert.equal(before.signals.unresolvedNeed.request_id, "oldest");
  assert.equal(before.signals.unresolvedNeed.state, ">6h");
  assert.equal(before.status, "blocked");
  const retained = moods.retainMoodWitnesses(validated(history));
  assert.ok(retained.some(item => item.payload.request_id === "oldest"));
  assert.deepEqual(one(retained), before);
});

test("rotation completes anchored closes and preserves every collision owner's need", () => {
  const anchoredClosed = [knock(20, "r1"), close(10, "r1"),
    knock(11, "r2"), close(12, "r2")];
  const anchoredRetained = moods.retainMoodWitnesses(validated(anchoredClosed));
  assert.ok(anchoredRetained.includes(anchoredClosed[1]),
    "a structured timestamp anchor carries its append-authoritative exact close");
  assert.deepEqual(one(anchoredRetained), one(anchoredClosed));
  assert.equal(one(anchoredRetained).signals.unresolvedNeed.observed, false);

  const canonical = knock(-20, "shared");
  const olderCollision = { ...knock(-500, "shared", {
    message: "Older incompatible question" }), agent_id: "codex:a" };
  const sameAgent = [canonical, olderCollision, work(0)];
  const sameMood = one(sameAgent);
  assert.equal(sameMood.signals.unresolvedNeed.kind, "structured collision");
  assert.equal(sameMood.signals.unresolvedNeed.logAgeMs, 500 * 60 * 1000,
    "the older incompatible canonical knock wins instead of collapsing into the first request");

  const crossCanonical = { ...knock(-10, "cross"), agent_id: "codex:source" };
  const crossCollision = { ...knock(-500, "cross", {
    message: "Visitor's incompatible question" }), agent_id: "codex:owner" };
  const crossWork = event(0, "idle", {}, { agent_id: "codex:owner" });
  const cross = [crossCanonical, crossCollision, crossWork];
  const before = moods.deriveMoods(validated(cross)).get("codex:owner");
  assert.equal(before.status, "blocked");
  const retained = moods.retainMoodWitnesses(validated(cross));
  assert.ok(retained.includes(crossCanonical), "cross-agent canonical collision authority is retained");
  assert.ok(retained.includes(crossCollision), "the projected collision owner keeps its own knock");
  assert.deepEqual(moods.deriveMoods(retained).get("codex:owner"), before);

  const pressure = Array.from({ length: 200 }, (_, index) => knock(-300 + index,
    "bounded-collision", { message: `Incompatible question ${index}` }));
  pressure.push(work(0));
  const pressureRetained = moods.retainMoodWitnesses(validated(pressure));
  assert.ok(pressureRetained.length <= moods.MAX_RETAINED_PER_AGENT,
    "collision completion remains bounded instead of retaining the whole ID group");
  assert.deepEqual(one(pressureRetained), one(pressure));
});

test("a retained authoritative close completes its displaced canonical lifecycle", () => {
  const history = [knock(-480, "q1"), close(-470, "q1"),
    knock(-460, "q2"), knock(-450, "q2", { action: "erase" }),
    close(-440, "q2"), event(0, "idle")];
  const before = one(history);
  assert.equal(before.signals.interaction.kind, "approval decision");
  const retained = moods.retainMoodWitnesses(validated(history));
  assert.ok(retained.includes(history[0]),
    "the exact human-interaction close carries its canonical knock");
  assert.deepEqual(one(retained), before);
});

test("retention preserves ambiguous-close dependencies and post-close collision authority", () => {
  const ambiguous = lifecycleAmbiguity.scenarios.ambiguous_close;
  const beforeAmbiguous = moods.deriveMoods(validated(ambiguous)).get("codex:ambiguous");
  const retainedAmbiguous = moods.retainMoodWitnesses(validated(ambiguous));
  assert.deepEqual(moods.deriveMoods(retainedAmbiguous).get("codex:ambiguous"), beforeAmbiguous,
    "a surviving ambiguous resolution carries two matching request lifecycles");
  assert.equal(beforeAmbiguous.status, "blocked");

  const scenario = lifecycleAmbiguity.scenarios.post_close_collision;
  const pendingStart = Date.parse(scenario.pending_start);
  const pending = Array.from({ length: scenario.pending_count }, (_, index) => ({
    ...scenario.anchor, ts: new Date(pendingStart + index * 60000).toISOString(),
    type: "needs_human", payload: { message: `Pending ${index}`,
      request_id: `pressure-${index}`, action: "deploy", detail: null,
      options: ["approve", "deny"] },
  }));
  const collided = [...scenario.events, ...pending, scenario.anchor];
  const beforeCollided = moods.deriveMoods(validated(collided)).get("codex:post-collision");
  const retainedCollided = moods.retainMoodWitnesses(validated(collided));
  assert.deepEqual(moods.deriveMoods(retainedCollided).get("codex:post-collision"), beforeCollided);
  assert.equal(beforeCollided.signals.interaction.kind, "approval decision");
});

test("authority capsule survives predecessor invalidation beyond every raw fallback cap", () => {
  const first = [];
  for (let index = 0; index < 81; index++) {
    first.push(knock(-400 + index * 2, `capsule-${index}`),
      close(-399 + index * 2, `capsule-${index}`));
  }
  const compacted = moods.retainMoodWitnesses(validated(first));
  assert.ok(compacted.length <= moods.MAX_RETAINED_PER_AGENT,
    "structured authority does not consume the bounded raw witness window");
  const later = [];
  for (let index = 1; index < 81; index++) {
    later.push(knock(-100 + index, `capsule-${index}`, { action: "erase" }));
  }
  later.push(event(0, "idle"));
  const full = [...first, ...later];
  const twice = moods.retainMoodWitnesses(moods.combineMoodEvidence(
    compacted, validated(later)));
  assert.deepEqual(one(twice), one(full));
  assert.equal(one(twice).status, "authority history uncertain");
  assert.equal(one(twice).signals.interaction.kind, null);

  const crossReuse = { ...knock(1, "capsule-0", { action: "erase" }),
    agent_id: "codex:other" };
  const otherIdle = event(2, "idle", {}, { agent_id: "codex:other" });
  const crossFull = validated([...full, crossReuse, otherIdle]);
  const crossCompacted = moods.retainMoodWitnesses(moods.combineMoodEvidence(
    twice, validated([crossReuse, otherIdle])));
  assert.deepEqual(moods.deriveMoods(crossCompacted).get("codex:other"),
    moods.deriveMoods(crossFull).get("codex:other"));
  assert.equal(moods.deriveMoods(crossCompacted).get("codex:other").status,
    "authority history uncertain");
});

test("collision quarantine reopens every candidate and ignores all later closes", () => {
  const canonical = knock(-500, "durable");
  const decided = close(-490, "durable");
  const sameAgentReuse = knock(-480, "durable", { action: "erase" });
  const ignoredClose = close(-470, "durable", { action: "erase" });
  const same = one([canonical, decided, sameAgentReuse, ignoredClose, work(0)]);
  assert.equal(same.signals.unresolvedNeed.kind, "structured collision");
  assert.equal(same.signals.unresolvedNeed.logAgeMs, 500 * 60000);
  assert.equal(same.signals.interaction.observed, false);

  const crossReuse = { ...sameAgentReuse, agent_id: "codex:other" };
  const otherIdle = event(0, "idle", {}, { agent_id: "codex:other" });
  const cross = moods.deriveMoods(validated([canonical, decided, crossReuse,
    ignoredClose, otherIdle]));
  assert.equal(cross.get("codex:a").signals.unresolvedNeed.observed, true);
  assert.equal(cross.get("codex:other").signals.unresolvedNeed.observed, true);
});

test("capsule byte ceiling chooses complete near-boundary authority or empty overflow", () => {
  function lifecycle(index) {
    const detail = { pad: "x".repeat(50) };
    return [knock(-500 + index * 2, `byte-${index}`, { detail }),
      close(-499 + index * 2, `byte-${index}`, { detail })];
  }
  let boundary = 1;
  while (!projection.moodAuthorityState(moods.retainMoodWitnesses(validated(
    Array.from({ length: boundary }, (_, index) => lifecycle(index)).flat()))).overflow) boundary++;
  const below = validated(Array.from({ length: boundary - 1 }, (_, index) => lifecycle(index)).flat());
  const retainedBelow = moods.retainMoodWitnesses(below);
  const stateBelow = projection.moodAuthorityState(retainedBelow);
  assert.equal(stateBelow.overflow, false);
  assert.ok(projection.moodAuthorityCapsuleByteLength(stateBelow.events, stateBelow.copies,
    stateBelow) <= moods.MAX_AUTHORITY_BYTES);
  assert.deepEqual(moods.deriveMoods(retainedBelow), moods.deriveMoods(below));

  const above = validated(Array.from({ length: boundary }, (_, index) => lifecycle(index)).flat());
  const retainedAbove = moods.retainMoodWitnesses(above);
  const stateAbove = projection.moodAuthorityState(retainedAbove);
  assert.equal(stateAbove.overflow, true);
  assert.deepEqual([stateAbove.events, stateAbove.ordinals, stateAbove.copies,
    stateAbove.rawOrdinals], [[], [], [], []]);
  assert.deepEqual(moods.deriveMoods(retainedAbove), moods.deriveMoods(above));
  const uncertain = moods.deriveMoods(retainedAbove).get("codex:a");
  const html = glyph.render("Overflow", uncertain);
  assert.match(html, /Authority history overflow; retained authority is incomplete/);
  assert.doesNotMatch(html, /not enough observed evidence|score null/);
});

test("fifty-thousand structured facts saturate before constructing unbounded authority", () => {
  const history = validated(Array.from({ length: 50_000 }, (_, index) =>
    knock(index / 1000, `pressure-${index}`, { message: `Request ${index}` })));
  const started = performance.now();
  const mood = moods.deriveMoods(history).get("codex:a");
  const retained = moods.retainMoodWitnesses(history);
  const elapsed = performance.now() - started;
  assert.ok(elapsed < 5000, `structured authority saturation took ${elapsed.toFixed(0)}ms`);
  assert.equal(mood.status, "authority history uncertain");
  assert.equal(mood.authority.complete, false);
  assert.ok(retained.length <= 2, `overflow attached ${retained.length} raw records`);
  assert.deepEqual(projection.moodAuthorityState(retained), {
    events: [], ordinals: [], copies: [], rawOrdinals: [], rawIndexes: [],
    rawCount: "0000000000000000", overflow: true, observed: 257,
  });
  const appended = moods.combineMoodEvidence(retained, validated([event(51, "idle")]));
  assert.equal(moods.deriveMoods(appended).get("codex:a").status,
    "authority history uncertain", "an ordinary append cannot recover overflowed authority");
});

test("large plain and malformed fallback logs retain in linear time", () => {
  const history = [];
  for (let index = 0; index < 12_000; index++) {
    history.push(event(index / 1000, "needs_human", index % 2 ?
      { message: `plain ${index}` } : { message: `fallback ${index}`, request_id: `bad-${index}` }));
  }
  const started = performance.now();
  const retained = moods.retainMoodWitnesses(validated(history));
  assert.ok(performance.now() - started < 2500, "plain/fallback compaction stays linear");
  assert.deepEqual(one(retained), one(history));
});

test("shared glyph renderer exposes all signals, stale-only alpha hook and no animation", () => {
  const mood = one([knock(-400), work(0)]);
  const html = glyph.render("Maren & Co", mood, { stale: true });
  assert.match(html, /<details class="mood mood-stale" data-mood>/);
  assert.match(html, /Maren &amp; Co: mood blocked; open observed-signal breakdown/);
  for (const label of ["Log age as of", "Failure streak", "Workload density",
    "Last human interaction", "Oldest unresolved human need"]) {
    assert.match(html, new RegExp(`<dt>${label}</dt><dd`));
  }
  assert.equal((html.match(/<dt>/g) || []).length, 5);
  assert.match(html, /<dl><dt>Log age as of<\/dt><dd class="mood-asof">/);
  assert.match(html, /Presence is stale; staleness is not scored/);
  assert.doesNotMatch(html, /animation|transition/);

  for (const boundary of [60 * 60 * 1000 + 1, 6 * 60 * 60 * 1000 + 1,
    24 * 60 * 60 * 1000 + 1]) {
    const exact = one([eventMs(-boundary, "task_started", { prompt: "human" }), event(0, "idle")]);
    const rendered = glyph.render("Exact", exact);
    const hours = Math.floor(boundary / (60 * 60 * 1000));
    assert.match(rendered, new RegExp(`log age ${hours}h 0m 0s 1ms`));
  }
});
