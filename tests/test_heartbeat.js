/* Fixture test for the heartbeat rule (docs/protocol.md, issue #4).
 *
 * The projection lives inline in viewer/index.html; lift the shared block out of
 * the file and run it in a vm so the reducer can be tested without a browser.
 *
 *   node tests/test_heartbeat.js        (run from the repo root)
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "viewer/index.html"), "utf8");
const start = html.indexOf("const NAMES");
const end = html.indexOf("/* ————— the village scene", start);
assert.notEqual(start, -1, "projection block not found in viewer/index.html");
assert.notEqual(end, -1, "end of projection block not found in viewer/index.html");

const context = {};
vm.createContext(context);
vm.runInContext(
  html.slice(start, end) + "\nthis.projection = { reduce, describe, doingLabel, STALE_MS };",
  context,
);
const { reduce, STALE_MS } = context.projection;

const lines = fs.readFileSync(path.join(root, "tests/fixtures/heartbeat.jsonl"), "utf8")
  .trimEnd().split("\n");
const now = Date.parse("2026-08-24T13:00:00.000Z");
const byId = new Map(reduce(lines, now, []).map(v => [v.id, v]));

// The point of the issue: the last *tool start* is nearly two hours old, so
// without heartbeats this villager would have faded to stale long ago.
const build = byId.get("claude-code:aaa");
const lastToolCall = Date.parse("2026-08-24T11:05:00.000Z");
assert.ok(now - lastToolCall > STALE_MS, "fixture must outrun the stale window");
assert.equal(build.state, "working", "tool completions keep a long run un-stale");
assert.equal(build.lastTs, Date.parse("2026-08-24T12:58:00.000Z"),
             "liveness clock follows the newest heartbeat");

// …but a heartbeat must not invent a visible action: the villager is still shown
// doing the last thing it actually started, and heartbeats stay out of its log.
assert.equal(build.doing, "tinkering");
assert.equal(build.lastLine, "tinkering — make build");
assert.equal(build.events.map(e => e.type).join(","), "task_started,tool_called",
             "heartbeats must not flood the villager's event log");

// Control: same session, same clock, no heartbeats -> still stale.
const wedged = byId.get("claude-code:bbb");
assert.equal(wedged.state, "stale", "a silent session must still go stale");

// A window holding only heartbeats: alive and working, with no action claimed.
const beatOnly = byId.get("claude-code:ccc");
assert.equal(beatOnly.state, "working");
assert.equal(beatOnly.doing, "");
assert.equal(beatOnly.lastLine, "finished Grep");

// A tool finishing after a knock means the human answered and work resumed.
const answered = byId.get("claude-code:ddd");
assert.equal(answered.state, "working");

console.log("ok - heartbeat projection (4 villagers)");
