"use strict";

const assert = require("assert");
const { createStateTransport, validateEnvelope } = require("../viewer/state-transport.js");

function snapshot(generation, cursor, villagers = []) {
  return { schema_version: 1, generation, cursor, evaluated_at: "2026-08-26T12:00:00.000Z",
    villagers, residents: [], artifacts: [], tasks: [], approvals: [], journals: [], routines: [],
    diagnostics: [], capacity: {}, capabilities: {} };
}

assert.equal(validateEnvelope({ kind: "snapshot", snapshot: snapshot(1, "c1") }), null);
assert.match(validateEnvelope({ kind: "snapshot", snapshot: { schema_version: 2 } }), /schema/);

(async () => {
  const published = [], statuses = [], requests = [];
  const responses = [
    { status: 200, json: async () => ({ kind: "snapshot", snapshot: snapshot(2, "c2", [{ id: "a" }]) }) },
    { status: 200, json: async () => ({ kind: "snapshot", snapshot: snapshot(1, "c1", [{ id: "old" }]) }) },
  ];
  const transport = createStateTransport({
    fetch: async url => { requests.push(url); return responses.shift(); }, EventSource: null,
    onState: value => published.push(value), onStatus: value => statuses.push(value), warn: () => {},
  });
  await transport.poll();
  await transport.poll();
  assert.deepEqual(published.map(value => value.villagers[0].id), ["a"]);
  assert.match(requests[1], /generation=2/);
  assert.match(requests[1], /cursor=c2/);
  assert.equal(transport.snapshot().generation, 2);
  assert.ok(statuses.includes("polling"));

  const resets = [];
  const resetTransport = createStateTransport({ fetch: async () => ({ status: 204 }), EventSource: null,
    onState: value => resets.push(value), warn: () => {} });
  assert.equal(resetTransport.apply({ kind: "snapshot", snapshot: snapshot(5,
    "v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:1:2:1:20") }), true);
  assert.equal(resetTransport.apply({ kind: "reset", snapshot: snapshot(1,
    "v1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:1:3:1:0") }), true);
  assert.equal(resetTransport.apply({ kind: "reset", snapshot: snapshot(6,
    "v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:1:2:1:30") }), false);
  assert.equal(resetTransport.snapshot().cursor,
    "v1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:1:3:1:0");
  assert.equal(resets.length, 2);
  console.log("state transport tests passed");
})().catch(error => { console.error(error); process.exit(1); });
