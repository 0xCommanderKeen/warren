const assert = require("node:assert/strict");
const fs = require("node:fs");
const { foldEvents, reduce } = require("../viewer/projection.js");

const lines = fs.readFileSync("tests/fixtures/events.jsonl", "utf8").trimEnd().split("\n");
const now = Date.parse("2026-08-24T12:04:00.000Z");
const fullState = new Map();
foldEvents(fullState, lines);
const full = reduce(fullState, now, []);

const incrementalState = new Map();
foldEvents(incrementalState, lines.slice(0, 2));
foldEvents(incrementalState, lines.slice(2, 4));
foldEvents(incrementalState, lines.slice(4));
const incremental = reduce(incrementalState, now, []);

assert.deepEqual(JSON.parse(JSON.stringify(incremental)), JSON.parse(JSON.stringify(full)));
assert.deepEqual(JSON.parse(JSON.stringify(
  full.map(({ id, state, events }) => [id, state, events.length])
)), [
  ["alpha", "working", 2],
  ["beta", "knocking", 1],
]);
console.log("fixture bootstrap and incremental projection match");
