"use strict";

const assert = require("assert");
const { createBrowserRuntime } = require("../viewer/browser-runtime.js");
const { adapt } = require("../viewer/village-adapter.js");

const state = {
  schema_version: 1, generation: 4, cursor: "cursor-4", log_generation: 2,
  evaluated_at: "2026-08-26T12:00:00.000Z",
  residents: [{ valid: true, file: "resident.json", manifest_version: 1, match: { agent_id: "resident" }, home: 3,
    meta: { name: "Keeper", char: "Monk", accent: "#123456" } }],
  diagnostic_residents: [{ valid: false, diagnostic: true, file: "bad.resident.json" }],
  villagers: [{ id: "resident", name: "Keeper", char: "Monk", accent: "#123456",
    residency: "resident", resident_file: "resident.json", base: "home", home: 3, state: "knocking", project: "burrow",
    last_ts: "2026-08-26T12:00:00.000Z", last_line: "needs you", history: [], mood: { kind: "focused" },
    pending_approval_ids: ["request-1"] }],
  approvals: [{ request_id: "request-1", agent_id: "resident", state: "pending", message: "Ship?",
    action: "ship", detail: null, options: ["approve"], opened_at: "2026-08-26T12:00:00.000Z" }],
  tasks: [{ id: "task-1", title: "Fix", state: "open", required_skills: [], claimant: null,
    updated_at: "2026-08-26T12:00:00.000Z" }],
  artifacts: [],
  journals: [{ day: "2026-08-26", agent_id: "resident", project: "burrow", routine: "daily",
    path: "journals/2026-08-26.md", observed_at: "2026-08-26T11:59:00.000Z", owner_file: "resident.json" }],
  routines: [{ run_id: "run-1", routine: "daily", agent_id: "resident", project: "burrow",
    state: "running", trigger: "schedule", started_at: "2026-08-26T11:58:00.000Z",
    updated_at: "2026-08-26T11:58:00.000Z" }],
  diagnostics: [], capacity: {}, capabilities: {},
};

const view = adapt(state);
assert.equal(view.villagers[0].soul.home, 3);
assert.equal(view.villagers[0].knock.request_id, "request-1");
assert.equal(view.jobState.authoritativeRows[0].state, "open");
assert.equal(view.diagnosticResidents[0].file, "bad.resident.json");
assert.equal(view.journalState.records.size, 1);
assert.equal([...view.journalState.records.values()][0].event.payload.day, "2026-08-26");
assert.equal(view.state.routineRecent.length, 1);
assert.equal(view.state.routineRecent[0].event.payload.run_id, "run-1");

(async () => {
  let published;
  const requested = [];
  const runtime = createBrowserRuntime({
    baseUrl: "/burrow/",
    fetch: async url => { requested.push(url);
      return { status: 200, json: async () => url.endsWith("/transport/status") ?
        { notifications: {} } : { kind: "snapshot", snapshot: state } }; },
    EventSource: null,
    onProjection: value => { published = value; },
  });
  await runtime.poll();
  assert.strictEqual(published.villagers, state.villagers,
    "the shared runtime passes authoritative collections through unchanged");
  assert.strictEqual(published.approvals, state.approvals);
  assert.equal(requested[0], "/burrow/state");
  assert.equal(published.transport, "polling");
  assert.equal(runtime.snapshot().transport, "polling");
  await runtime.refreshTransportStatus();
  assert.equal(requested[1], "/burrow/transport/status");
  console.log("browser state runtime fixture passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
