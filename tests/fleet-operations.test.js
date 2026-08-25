"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fleet = require("../viewer/fleet-operations.js");

const BASE = Date.parse("2026-08-25T12:00:00.000Z");
function event(index, overrides = {}) {
  return { v: 0, ts: new Date(BASE + index).toISOString(), source: "claude-code",
    agent_id: "agent-a", project: "burrow", type: "tool_called",
    payload: { tool: "Read", detail: `file-${index}` }, ...overrides };
}

test("recent activity stays bounded while incremental batches remain searchable", () => {
  const state = fleet.createFleetState();
  for (let i = 0; i < fleet.MAX_RECENT_EVENTS + 37; i++) fleet.foldFleet(state, [event(i)]);
  assert.equal(state.recent.length, fleet.MAX_RECENT_EVENTS);
  assert.equal(state.recent[0].event.payload.detail, `file-${fleet.MAX_RECENT_EVENTS + 36}`);
  assert.equal(fleet.filterRecent(state, { query: "file-236", project: "burrow",
    source: "claude-code", state: "working", villager: "agent-a" }).length, 1);
  assert.equal(fleet.filterRecent(state, { project: "life" }).length, 0);
});

test("outstanding needs-human means the latest retained event still needs a human", () => {
  const state = fleet.createFleetState();
  fleet.foldFleet(state, [
    event(1, { agent_id: "waiting", project: "life", type: "needs_human",
      payload: { message: "Choose a time" } }),
    event(2, { agent_id: "resolved", type: "needs_human", payload: { message: "Approve" } }),
    event(3, { agent_id: "resolved", type: "idle", payload: {} }),
  ], 2);
  assert.deepEqual(fleet.outstandingNeeds(state).map(x => x.agent_id), ["waiting"]);
  assert.equal(state.malformed, 2);
});

test("capability truth distinguishes declaration, absence, invalidity, and outage", () => {
  assert.equal(fleet.capabilityStatus({ id: "mail", status_ref: "owner:status" }), "configured");
  assert.equal(fleet.capabilityStatus(null), "missing");
  assert.equal(fleet.capabilityStatus({ id: "mail", status_ref: "invalid" }), "configured",
    "opaque locator text cannot manufacture invalid state");
  assert.equal(fleet.capabilityStatus({ id: "mail", status_ref: "externally-unavailable" }),
    "configured", "opaque locator text cannot manufacture outage state");
  assert.equal(fleet.capabilityStatus({ id: "mail", status_ref: "owner:status" }, false),
    "externally unavailable");
  assert.equal(fleet.capabilityStatus({ id: "missing-status" }), "invalid");
  assert.equal(fleet.capabilityStatus({ id: "mail", status_ref: "owner:status" }, true, true),
    "invalid");
  const directory = fleet.residentDirectory([{ valid: true, home: 3 }, { file: "partial.json" }],
    [{ file: "broken.json", path: "$.memory", message: "is required" }]);
  assert.equal(directory.residents[0].home, 3);
  assert.equal(directory.invalid[0].status, "invalid");
  assert.equal(directory.invalid.length, 2);
  assert.equal(directory.status, "invalid");
  assert.equal(fleet.residentDirectory([], [], true).status, "missing");
  assert.equal(fleet.residentDirectory([], [], false).status, "externally unavailable");
});

test("safe diagnostic residents remain separate and drive row-level invalid state", () => {
  const partial = { file: "unsafe.resident.json", valid: false, diagnostic: true,
    declared_home: 4, match: { agent_id: "safe-agent" },
    capabilities: { skills: [{ id: "summary", status_ref: "config:summary", invalid: true,
      diagnostic_path: "$.skills[0].access_token" }] } };
  const directory = fleet.residentDirectory([], [{ file: partial.file,
    path: "$.skills[0].access_token", message: "credential field is forbidden" }], true, [partial]);
  assert.deepEqual(directory.residents, []);
  assert.equal(directory.diagnosticResidents[0], partial);
  assert.equal(fleet.capabilityStatus(partial.capabilities.skills[0], true,
    partial.capabilities.skills[0].invalid), "invalid");
});

test("keyboard tab navigation wraps and supports home/end", () => {
  assert.equal(fleet.moveFocus(0, "ArrowRight", 3), 1);
  assert.equal(fleet.moveFocus(2, "ArrowDown", 3), 0);
  assert.equal(fleet.moveFocus(0, "ArrowLeft", 3), 2);
  assert.equal(fleet.moveFocus(1, "Home", 3), 0);
  assert.equal(fleet.moveFocus(1, "End", 3), 2);
  assert.equal(fleet.moveFocus(1, "Enter", 3), 1);
});
