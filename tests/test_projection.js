const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync("viewer/index.html", "utf8");
const start = html.indexOf("const NAMES");
const end = html.indexOf("/* ————— the village scene", start);
assert.notEqual(start, -1);
assert.notEqual(end, -1);

const context = {};
vm.createContext(context);
vm.runInContext(
  html.slice(start, end) + "\nthis.projection = { foldEvents, reduce };",
  context,
);

const lines = fs.readFileSync("tests/fixtures/events.jsonl", "utf8").trimEnd().split("\n");
const now = Date.parse("2026-08-24T12:04:00.000Z");
const fullState = new Map();
context.projection.foldEvents(fullState, lines);
const full = context.projection.reduce(fullState, now, []);

const incrementalState = new Map();
context.projection.foldEvents(incrementalState, lines.slice(0, 2));
context.projection.foldEvents(incrementalState, lines.slice(2, 4));
context.projection.foldEvents(incrementalState, lines.slice(4));
const incremental = context.projection.reduce(incrementalState, now, []);

assert.deepEqual(JSON.parse(JSON.stringify(incremental)), JSON.parse(JSON.stringify(full)));
assert.deepEqual(JSON.parse(JSON.stringify(
  full.map(({ id, state, events }) => [id, state, events.length])
)), [
  ["alpha", "working", 2],
  ["beta", "knocking", 1],
]);
console.log("fixture bootstrap and incremental projection match");
