"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const projection = require("../viewer/projection.js");
const moods = require("../viewer/moods.js");
const typedJSON = require("../viewer/typed-json.js");
const { validateEvent, reduce } = projection;

const cases = JSON.parse(fs.readFileSync("tests/fixtures/protocol-v0-validation.json"));

test("ingestion and projection share the documented v0 fixture matrix", () => {
  for (const fixture of cases) {
    assert.equal(validateEvent(fixture.event) === null, fixture.valid, fixture.name);
    if (fixture.error) assert.equal(validateEvent(fixture.event), fixture.error, fixture.name);
  }
});

test("the shared timestamp range excludes ISO year zero in both adapters", () => {
  const fixture = cases.find(f => f.name === "year zero timestamp");
  assert.ok(fixture);
  assert.equal(validateEvent(fixture.event), fixture.error);
});

test("routine durations must be finite across both adapters", () => {
  for (const fixture of cases.filter(f => f.name.includes("infinite duration"))) {
    assert.equal(validateEvent(fixture.event), fixture.error, fixture.name);
  }
});

test("projection ignores every invalid contract fixture", () => {
  const invalid = cases.filter(f => !f.valid).map(f => f.event);
  assert.deepEqual(reduce(invalid, Date.parse("2026-08-24T12:00:01.000Z"), []), []);
});

test("a failed tool is an explicit terminal activity state", () => {
  const failed = cases.find(f => f.name === "valid failed tool").event;
  const [villager] = reduce([failed], Date.parse("2026-08-24T12:00:01.000Z"), []);
  assert.equal(villager.state, "failed");
  assert.equal(villager.lastLine, "Bash failed — exit code 1");
});

test("Mood authority capsule is transport metadata, never a public v0 event", () => {
  const root = { v: 0, ts: "2026-08-25T10:00:00.000Z", source: "codex",
    agent_id: "codex:capsule", project: "burrow", type: "task_started",
    payload: { prompt: "Hello" } };
  const capsule = JSON.stringify({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: [root], ordinals: ["0"], copies: [], raw_ordinals: [], raw_indexes: [],
    raw_count: "0000000000000000", overflow: false,
    observed: 1 });
  const windows = projection.parseEventWindows([capsule], 4000);
  assert.equal(windows.full.length, 0);
  assert.equal(windows.rejected, 0);
  assert.deepEqual(projection.moodAuthority(windows.full), [root]);
  assert.equal(projection.validateEvent(JSON.parse(capsule)), "unsupported protocol version");
});

test("Mood capsule manifests are canonical, atomic, and cannot suppress unrelated raw evidence", () => {
  const root = { v: 0, ts: "2026-08-25T10:00:00.000Z", source: "codex",
    agent_id: "codex:owner", project: "burrow", type: "task_started",
    payload: { prompt: "Hello", n: 1 } };
  const other = { ...root, agent_id: "codex:other" };
  const envelope = overrides => JSON.stringify({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: [root], ordinals: ["7"], copies: ["7"],
    raw_ordinals: ["7"], raw_indexes: ["0000000000000000"],
    raw_count: "0000000000000001", overflow: false, observed: 1, ...overrides });
  const safe = projection.parseEvents([envelope({}), JSON.stringify(root)]);
  assert.deepEqual(projection.moodAuthority(safe), [root]);
  assert.equal(projection.moodAuthorityState(safe).copies.length, 1);

  for (const [unsafe, raw] of [
    [envelope({ copies: ["7", "7"] }), root],
    [envelope({}), other],
    [envelope({ ordinals: ["01"] }), root],
    [envelope({ raw_indexes: [] }), root],
    [envelope({ raw_indexes: ["0000000000000001"] }), root],
    [envelope({ raw_count: "1" }), root],
  ]) {
    const parsed = projection.parseEvents([unsafe, JSON.stringify(raw)]);
    assert.deepEqual(projection.moodAuthority(parsed), []);
    assert.deepEqual(parsed, [raw], "unsafe metadata is ignored without suppressing raw evidence");
  }
  const multiple = projection.parseEvents([envelope({ copies: [], raw_ordinals: [] }),
    envelope({ copies: [], raw_ordinals: [] }), JSON.stringify(root)]);
  assert.deepEqual(projection.moodAuthority(multiple), []);
  assert.deepEqual(multiple, [root]);
  const malformed = JSON.stringify({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: "broken" });
  for (const records of [
    ["not-json", envelope({}), JSON.stringify(root)],
    [malformed, JSON.stringify(root)],
    [envelope({ copies: [], raw_ordinals: [] }), malformed, JSON.stringify(root)],
  ]) {
    const parsed = projection.parseEvents(records);
    assert.deepEqual(projection.moodAuthority(parsed), [],
      "junk-before, malformed, and extra recognized markers invalidate capsule authority");
    assert.deepEqual(parsed, [root]);
  }
  for (const field of ["events", "ordinals", "copies", "raw_ordinals", "raw_indexes"]) {
    const payload = { events: [], ordinals: [], copies: [], raw_ordinals: [], raw_indexes: [],
      raw_count: "0000000000000000", overflow: true, observed: 257 };
    payload[field] = field === "events" ? [root] : ["7"];
    assert.deepEqual(projection.moodAuthority(projection.parseEvents([
      envelope(payload), JSON.stringify(root)])), [], `overflow ${field} must be empty`);
  }
  assert.deepEqual(projection.moodAuthority(projection.parseEvents([
    envelope({ events: [], ordinals: [], copies: [], raw_ordinals: [], raw_indexes: [],
      raw_count: "0000000000000001", overflow: true, observed: 257 }),
    JSON.stringify(root)])), [], "overflow raw_count must be zero");
  const oversized = envelope({ pad: "x".repeat(projection.MOOD_AUTHORITY_MAX_BYTES) });
  assert.deepEqual(projection.moodAuthority(projection.parseEvents([oversized,
    JSON.stringify(root)])), []);
  assert.equal(projection.canonicalIdentity(root), projection.canonicalIdentity(
    { ...root, payload: { n: 1.0, prompt: "Hello" } }),
  "numeric value and key order have one language-independent identity");
});

test("shared capsule field-domain matrix rejects malformed markers atomically", () => {
  const matrix = JSON.parse(fs.readFileSync(path.join(__dirname,
    "fixtures/mood-capsule-malformed.json")));
  const base = { _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: [matrix.authority_event], ordinals: ["0"], copies: [], raw_ordinals: [],
    raw_indexes: [], raw_count: "0000000000000000", overflow: false, observed: 1 };
  const raw = JSON.stringify(matrix.raw_event);
  for (const [field, value] of matrix.invalid_mutations) {
    const parsed = projection.parseEvents([JSON.stringify({ ...base, [field]: value }), raw]);
    assert.deepEqual(projection.moodAuthority(parsed), [], `${field}=${String(value)} is invalid`);
    assert.deepEqual(parsed, [matrix.raw_event], "malformed metadata leaves raw evidence unchanged");
  }
  for (const token of matrix.nonstandard_observed_tokens) {
    const line = JSON.stringify(base).replace('"observed":1', `"observed":${token}`);
    const parsed = projection.parseEvents([line, raw]);
    assert.deepEqual(projection.moodAuthority(parsed), []);
    assert.deepEqual(parsed, [matrix.raw_event]);
  }
  const integralFloat = JSON.stringify(base).replace('"observed":1', '"observed":1.0');
  assert.deepEqual(projection.moodAuthority(projection.parseEvents([integralFloat, raw])),
    [matrix.authority_event], "integral JSON numbers are accepted independent of spelling");
  const multiple = projection.parseEvents([JSON.stringify(base), JSON.stringify(base), raw]);
  assert.deepEqual(projection.moodAuthority(multiple), []);
  assert.deepEqual(multiple, [matrix.raw_event]);
});

test("shared capsule schema and typed graph attacks preserve every raw Mood witness", () => {
  const matrix = JSON.parse(fs.readFileSync(path.join(__dirname,
    "fixtures/mood-capsule-malformed.json")));
  const second = { ...matrix.authority_event,
    ts: "2026-08-25T10:30:00.000Z", agent_id: "codex:capsule-second",
    payload: { prompt: "Second retained root" } };
  const rawEvents = [matrix.authority_event, second, matrix.raw_event];
  const logical = { events: rawEvents.slice(0, 2), ordinals: ["1", "2"],
    copies: ["1", "2"], raw_ordinals: ["1", "2", "3"],
    raw_indexes: ["0000000000000000", "0000000000000001", "0000000000000002"],
    raw_count: "0000000000000003", overflow: false, observed: 2 };
  const direct = value => JSON.stringify({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    ...value });
  const envelope = (value, outer = {}) => JSON.stringify({
    _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    encoding: projection.MOOD_AUTHORITY_ENCODING,
    graph: typedJSON.typedGraph(value), ...outer });
  const raw = rawEvents.map(JSON.stringify);
  const expected = Object.fromEntries(moods.deriveMoods(projection.parseEvents(raw)));
  const assertIgnored = (line, name) => {
    const parsed = projection.parseEvents([line, ...raw]);
    assert.deepEqual(parsed, rawEvents, `${name}: raw public evidence remains present`);
    assert.deepEqual(projection.moodAuthority(parsed), [], `${name}: no capsule authority`);
    assert.deepEqual(Object.fromEntries(moods.deriveMoods(parsed)), expected,
      `${name}: no raw Mood witness is suppressed`);
  };

  assert.equal(projection.moodAuthority(projection.parseEvents([
    envelope(logical), ...raw])).length, 2, "canonical tree envelope is accepted");
  for (const mutation of matrix.canonical_schema_mutations) {
    const changed = structuredClone(logical);
    changed[mutation.field] = mutation.value;
    assertIgnored(direct(changed), `${mutation.name} direct root`);
    assertIgnored(envelope(changed), `${mutation.name} encoded state`);
  }
  const missing = structuredClone(logical); delete missing.copies;
  assertIgnored(direct(missing), "missing direct root field");
  assertIgnored(envelope(missing), "missing encoded state field");
  assertIgnored(envelope(logical, { surplus: true }), "surplus envelope field");

  const cloneGraph = () => structuredClone(typedJSON.typedGraph(logical));
  const references = node => node[0] === "a" ? node[1] :
    node[0] === "o" ? node[1].map(entry => entry[1]) : [];
  const unused = cloneGraph();
  unused[0].splice(unused[1], 0, ["s", "unused"]); unused[1]++;

  const sharedScalar = cloneGraph();
  const prompt = sharedScalar[0].findIndex(node => node[0] === "s" && node[1] === "Retained root");
  const source = sharedScalar[0].findIndex(node => node[0] === "s" &&
    node[1] === "codex:capsule-authority");
  const scalarParent = sharedScalar[0].find(node => references(node).includes(prompt));
  if (scalarParent[0] === "a") scalarParent[1][scalarParent[1].indexOf(prompt)] = source;
  else scalarParent[1].find(entry => entry[1] === prompt)[1] = source;

  const sharedContainer = cloneGraph();
  const rootEntries = sharedContainer[0][sharedContainer[1]][1];
  const eventArrayIndex = rootEntries.find(entry => entry[0] === "events")[1];
  sharedContainer[0][eventArrayIndex][1][1] = sharedContainer[0][eventArrayIndex][1][0];

  const amplified = cloneGraph();
  const leaf = amplified[0].findIndex(node => node[0] === "s" && node[1] === "Retained root");
  const depth = matrix.amplification_depth;
  for (const node of amplified[0]) {
    if (node[0] === "a") node[1] = node[1].map(index => index > leaf ? index + depth : index);
    if (node[0] === "o") for (const entry of node[1]) if (entry[1] > leaf) entry[1] += depth;
  }
  if (amplified[1] > leaf) amplified[1] += depth;
  const chain = [];
  for (let index = 0; index < depth; index++) {
    const prior = leaf + index;
    chain.push(["a", [prior, prior]]);
  }
  amplified[0].splice(leaf + 1, 0, ...chain);
  const detailParent = amplified[0].find(node => node[0] === "o" &&
    node[1].some(entry => entry[0] === "prompt" && entry[1] === leaf));
  detailParent[1].find(entry => entry[0] === "prompt")[1] = leaf + depth;
  assert.ok(envelope(logical).length < projection.MOOD_AUTHORITY_MAX_BYTES);
  let expanded = "Retained root";
  for (let index = 0; index < depth; index++) {
    expanded = [structuredClone(expanded), structuredClone(expanded)];
  }
  assert.ok(Buffer.byteLength(typedJSON.graphString(typedJSON.typedGraph(expanded))) >
    projection.MOOD_AUTHORITY_MAX_BYTES,
  "nested DAG's canonical tree representation exceeds the capsule limit");

  const sharedLeaf = { bounded: true };
  assert.throws(() => typedJSON.typedGraph([sharedLeaf, sharedLeaf]), /aliased JSON value/,
    "the encoder rejects a repeated container before expanding a direct-object DAG");
  const aliasedLogical = structuredClone(logical);
  const aliasedDetail = { small: true };
  aliasedLogical.events[0].payload = { left: aliasedDetail, right: aliasedDetail };
  assertIgnored({ _burrow_internal: projection.MOOD_AUTHORITY_KIND, ...aliasedLogical },
    "direct shared-container DAG");

  const attacks = { "unused node": unused, "shared scalar": sharedScalar,
    "shared container": sharedContainer, "nested amplification": amplified };
  for (const name of matrix.typed_graph_attacks) {
    const line = JSON.stringify({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
      encoding: projection.MOOD_AUTHORITY_ENCODING, graph: attacks[name] });
    assert.ok(Buffer.byteLength(line) < projection.MOOD_AUTHORITY_MAX_BYTES,
      `${name}: hostile compressed envelope is below the wire limit`);
    assert.throws(() => typedJSON.decodeGraph(attacks[name]), /noncanonical/,
      `${name}: graph is structurally valid but is not a canonical emitter tree`);
    assertIgnored(line, name);
  }
});

test("capsule arrays are exact dense JSON arrays without named or symbol properties", () => {
  const root = { v: 0, ts: "2026-08-25T10:00:00.000Z", source: "codex",
    agent_id: "codex:array-domain", project: "burrow", type: "task_started",
    payload: { prompt: "raw survives" } };
  const capsule = () => ({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: [structuredClone(root)], ordinals: ["0"], copies: ["0"], raw_ordinals: ["0"],
    raw_indexes: ["0000000000000000"], raw_count: "0000000000000001",
    overflow: false, observed: 1 });
  const attacks = [];
  const makeExotic = array => {
    Object.setPrototypeOf(array, { [Symbol.iterator]() {
      throw new Error("hostile inherited iterator must not run");
    } });
    return array;
  };
  for (const field of ["events", "ordinals", "copies", "raw_ordinals", "raw_indexes"]) {
    const named = capsule(); named[field].named = true; attacks.push(named);
    const symbolic = capsule(); symbolic[field][Symbol("hidden")] = true; attacks.push(symbolic);
    const sparse = capsule(); delete sparse[field][0]; attacks.push(sparse);
    const exotic = capsule(); makeExotic(exotic[field]); attacks.push(exotic);
    const accessor = capsule(); Object.defineProperty(accessor[field], "0", {
      enumerable: true, configurable: true,
      get() { throw new Error("hostile array accessor must not run"); },
    }); attacks.push(accessor);
  }
  const logical = capsule(); delete logical._burrow_internal;
  const graphNamed = { _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    encoding: projection.MOOD_AUTHORITY_ENCODING,
    graph: typedJSON.typedGraph(logical) };
  graphNamed.graph[0].named = true; attacks.push(graphNamed);
  const graphSymbol = structuredClone(graphNamed); delete graphSymbol.graph[0].named;
  graphSymbol.graph[0][Symbol("hidden")] = true; attacks.push(graphSymbol);
  const graphPrototype = { _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    encoding: projection.MOOD_AUTHORITY_ENCODING, graph: typedJSON.typedGraph(logical) };
  makeExotic(graphPrototype.graph); attacks.push(graphPrototype);
  const nodesPrototype = { _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    encoding: projection.MOOD_AUTHORITY_ENCODING, graph: typedJSON.typedGraph(logical) };
  makeExotic(nodesPrototype.graph[0]); attacks.push(nodesPrototype);
  const nodePrototype = { _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    encoding: projection.MOOD_AUTHORITY_ENCODING, graph: typedJSON.typedGraph(logical) };
  makeExotic(nodePrototype.graph[0][0]); attacks.push(nodePrototype);
  const referencePrototype = { _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    encoding: projection.MOOD_AUTHORITY_ENCODING, graph: typedJSON.typedGraph(logical) };
  const referenceNode = referencePrototype.graph[0].find(node =>
    (node[0] === "a" || node[0] === "o") && Array.isArray(node[1]));
  makeExotic(referenceNode[1]); attacks.push(referencePrototype);
  const eventArray = capsule(); makeExotic(eventArray.events); attacks.push(eventArray);
  const payloadArray = capsule(); payloadArray.events[0].payload.options = ["yes", "no"];
  makeExotic(payloadArray.events[0].payload.options); attacks.push(payloadArray);
  for (const attack of attacks) {
    const parsed = projection.parseEvents([attack, root]);
    assert.deepEqual(projection.moodAuthority(parsed), []);
    assert.deepEqual(parsed, [root], "non-JSON array properties cannot hide raw evidence");
    assert.equal(projection.parseEventWindows([attack, root], 80).rejected, 0,
      "hostile reserved arrays are silent metadata, never public rejections");
  }
});

test("direct capsule objects require exact plain JSON objects at every depth", () => {
  const root = { v: 0, ts: "2026-08-25T10:00:00.000Z", source: "codex",
    agent_id: "codex:object-domain", project: "burrow", type: "task_started",
    payload: { prompt: "raw survives" } };
  const capsule = () => ({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: [structuredClone(root)], ordinals: ["0"], copies: ["0"],
    raw_ordinals: ["0"], raw_indexes: ["0000000000000000"],
    raw_count: "0000000000000001", overflow: false, observed: 1 });
  const attacks = [];
  const symbolOuter = capsule(); symbolOuter[Symbol("hidden")] = true; attacks.push(symbolOuter);
  const hiddenOuter = capsule(); Object.defineProperty(hiddenOuter, "hidden", { value: true });
  attacks.push(hiddenOuter);
  const getterOuter = capsule(); Object.defineProperty(getterOuter, "encoding", {
    enumerable: true, get() { throw new Error("must not execute outer getter"); }});
  attacks.push(getterOuter);
  const getterGraph = { _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    encoding: projection.MOOD_AUTHORITY_ENCODING };
  Object.defineProperty(getterGraph, "graph", { enumerable: true,
    get() { throw new Error("must not execute graph getter"); }});
  attacks.push(getterGraph);
  const exoticOuter = capsule(); Object.setPrototypeOf(exoticOuter, { inherited: true });
  attacks.push(exoticOuter);

  const symbolEvent = capsule(); symbolEvent.events[0][Symbol("hidden")] = true;
  attacks.push(symbolEvent);
  const hiddenPayload = capsule(); Object.defineProperty(hiddenPayload.events[0].payload,
    "hidden", { value: true }); attacks.push(hiddenPayload);
  const getterDetail = capsule(); Object.defineProperty(getterDetail.events[0].payload,
    "detail", { enumerable: true, get() { throw new Error("must not execute detail getter"); }});
  attacks.push(getterDetail);
  const exoticDetail = capsule(); exoticDetail.events[0].payload.detail = { safe: true };
  Object.setPrototypeOf(exoticDetail.events[0].payload.detail, { inherited: true });
  attacks.push(exoticDetail);

  for (const attack of attacks) {
    assert.doesNotThrow(() => projection.parseEvents([attack, root]));
    const parsed = projection.parseEvents([attack, root]);
    assert.deepEqual(parsed, [root], "hostile metadata leaves raw public evidence unchanged");
    assert.deepEqual(projection.moodAuthority(parsed), []);
    assert.equal(projection.parseEventWindows([attack, root], 80).rejected, 0,
      "reserved hostile direct object is silent internal metadata");
  }
});

test("duplicate capsule wire members reject outer, state, and nested logical ambiguity", () => {
  const root = { v: 0, ts: "2026-08-25T10:00:00.000Z", source: "codex",
    agent_id: "codex:duplicates", project: "burrow", type: "task_started",
    payload: { prompt: "raw survives" } };
  const direct = JSON.stringify({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: [root], ordinals: ["0"], copies: ["0"], raw_ordinals: ["0"],
    raw_indexes: ["0000000000000000"], raw_count: "0000000000000001",
    overflow: false, observed: 1 });
  const outer = direct.replace('{"_burrow_internal":',
    '{"_burrow_internal":"mood-authority-v1","_burrow_internal":');
  const state = direct.replace('"events":', '"events":[],"events":');
  const nested = direct.replace('"type":"task_started"',
    '"type":"idle","type":"task_started"');
  for (const line of [outer, state, nested]) {
    const parsed = projection.parseEvents([line, JSON.stringify(root)]);
    assert.deepEqual(projection.moodAuthority(parsed), []);
    assert.deepEqual(parsed, [root], "duplicate capsule keys preserve raw evidence");
  }
});

test("shared capsule depth bound and direct JSON-domain failures preserve raw evidence", () => {
  const matrix = JSON.parse(fs.readFileSync(path.join(__dirname,
    "fixtures/mood-capsule-malformed.json")));
  assert.equal(projection.MOOD_AUTHORITY_MAX_DEPTH, matrix.max_structural_depth);
  const nested = containers => {
    let value = "leaf";
    for (let index = 0; index < containers; index++) value = [value];
    return value;
  };
  const authority = containers => ({ ...matrix.authority_event, type: "needs_human",
    payload: { message: "Deep request", detail: nested(containers) } });
  const capsule = event => ({ _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: [event], ordinals: ["0"], copies: [], raw_ordinals: [], raw_indexes: [],
    raw_count: "0000000000000000", overflow: false, observed: 1 });
  const raw = matrix.raw_event;
  const accepted = authority(matrix.accepted_detail_containers);
  assert.deepEqual(projection.moodAuthority(projection.parseEvents([
    JSON.stringify(capsule(accepted)), JSON.stringify(raw)])), [accepted]);
  const overDepth = authority(matrix.rejected_detail_containers);
  const ignored = projection.parseEvents([JSON.stringify(capsule(overDepth)), JSON.stringify(raw)]);
  assert.deepEqual(projection.moodAuthority(ignored), []);
  assert.deepEqual(ignored, [raw]);

  const undefinedCapsule = { ...capsule(accepted), extra: undefined };
  const bigintCapsule = { ...capsule(accepted), extra: 1n };
  const cyclicCapsule = capsule(accepted); cyclicCapsule.extra = cyclicCapsule;
  const invalidOrdinary = { ...raw, payload: { extra: undefined } };
  const cyclicOrdinary = { ...raw, payload: {} };
  cyclicOrdinary.payload.loop = cyclicOrdinary.payload;
  for (const direct of [undefined, 1n, undefinedCapsule, bigintCapsule,
    cyclicCapsule, invalidOrdinary, cyclicOrdinary]) {
    assert.doesNotThrow(() => projection.parseEvents([direct, raw]));
    assert.deepEqual(projection.parseEvents([direct, raw]), [raw]);
  }
  const deepOrdinary = { ...raw, type: "needs_human",
    payload: { message: "Public depth is unrestricted", detail: nested(100) } };
  assert.deepEqual(projection.parseEvents([deepOrdinary]), [deepOrdinary],
    "the capsule metadata limit does not alter valid public-event parsing");
});

test("malformed direct internal markers are silent and preserve following raw authority", () => {
  const root = { v: 0, ts: "2026-08-25T10:00:00.000Z", source: "codex",
    agent_id: "codex:raw", project: "burrow", type: "task_started",
    payload: { prompt: "kept" } };
  const cyclic = { _burrow_internal: projection.MOOD_AUTHORITY_KIND };
  cyclic.events = cyclic;
  const undefinedField = { _burrow_internal: projection.MOOD_AUTHORITY_KIND,
    events: undefined };
  for (const marker of [cyclic, undefinedField]) {
    const parsed = projection.parseEvents([marker, root]);
    assert.deepEqual(parsed, [root]);
    assert.equal(projection.approvalRejections(parsed).length, 0);
    assert.deepEqual(projection.moodAuthority(parsed), []);
  }
});
