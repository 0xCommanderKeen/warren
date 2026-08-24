"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { reduce, MAX_EVENTS } = require("../viewer/projection.js");

const NOW = Date.parse("2026-08-24T12:00:00.000Z");
const event = (agent_id, project = "burrow") => ({
  v: 0, ts: "2026-08-24T11:59:00.000Z", source: "test", agent_id, project,
  type: "task_started", payload: { prompt: "work" },
});
const resident = ({ file, home, agent_id, project, name }) => ({
  file, home, valid: true, manifest_version: 1, match: agent_id ? { agent_id } : { project },
  meta: { ...(agent_id ? { agent_id } : { project }), name, char: "Monk", accent: "#a68a4f" },
  body: name + " soul",
  capabilities: { soul: {}, skills: [], memory: {}, routes: [], app_grants: [] },
});

test("a manifest promotes the same visitor without rewriting event history", () => {
  const history = [event("claude-code:session")];
  const [visitor] = reduce(history, NOW, []);
  const [promoted] = reduce(history, NOW, [resident({
    file: "burrow.resident.json", home: 5, project: "burrow", name: "Maren",
  })]);
  assert.equal(visitor.residency, "visitor");
  assert.equal(visitor.base, "visitor-lodge");
  assert.equal(visitor.home, null);
  assert.equal(promoted.id, visitor.id);
  assert.deepEqual(promoted.events, visitor.events);
  assert.equal(promoted.residency, "resident");
  assert.equal(promoted.home, 5);
});

test("resident homes come from the manifest and ignore active fleet composition", () => {
  const hob = resident({ file: "life.resident.json", home: 6,
                         agent_id: "claude-code:life-agent", name: "Hob" });
  const alone = reduce([event("claude-code:life-agent", "life")], NOW, [hob]);
  const crowded = reduce([event("a-visitor"), event("claude-code:life-agent", "life"),
                          event("z-visitor")], NOW, [hob]);
  assert.equal(alone[0].home, 6);
  assert.equal(crowded.find(v => v.id === "claude-code:life-agent").home, 6);
  assert.ok(crowded.filter(v => v.residency === "visitor").every(v => v.home === null));
});

test("exact agent identity is reserved before project fallback", () => {
  const project = resident({ file: "project.resident.json", home: 0,
                             project: "burrow", name: "Maren" });
  const exact = resident({ file: "exact.resident.json", home: 1,
                           agent_id: "z-exact", name: "Vesper" });
  const village = reduce([event("a-fallback"), event("z-exact")], NOW, [project, exact]);
  assert.equal(village.find(v => v.id === "z-exact").name, "Vesper");
  assert.equal(village.find(v => v.id === "a-fallback").name, "Maren");
});

test("one project resident is never assigned to two simultaneous sessions", () => {
  const project = resident({ file: "project.resident.json", home: 0,
                             project: "burrow", name: "Maren" });
  const village = reduce([event("a"), event("b")], NOW, [project]);
  assert.equal(village.filter(v => v.residency === "resident").length, 1);
  assert.equal(village.filter(v => v.base === "visitor-lodge").length, 1);
});

test("project fallback stays with the parent while unsouled children are visitors", () => {
  const project = resident({ file: "project.resident.json", home: 0,
                             project: "burrow", name: "Maren" });
  const child = event("a-child");
  child.payload = { prompt: "delegated work", parent_agent_id: "z-parent", agent_type: "reviewer" };
  const village = reduce([child, event("z-parent")], NOW, [project]);
  assert.equal(village.find(v => v.id === "z-parent").residency, "resident");
  assert.equal(village.find(v => v.id === "a-child").residency, "visitor");
});

test("child lineage survives beyond the bounded display history", () => {
  const project = resident({ file: "project.resident.json", home: 0,
                             project: "burrow", name: "Maren" });
  const child = event("a-child");
  child.payload = { prompt: "delegated work", parent_agent_id: "z-parent", agent_type: "reviewer" };
  const history = [child, event("z-parent")];
  for (let index = 0; index < MAX_EVENTS + 1; index++) {
    history.push({ ...event("a-child"),
      ts: new Date(NOW - (MAX_EVENTS - index) * 10).toISOString(),
      type: "tool_called", payload: { tool: "Read", detail: `file-${index}` } });
  }
  const village = reduce(history, NOW, [project]);
  assert.equal(village.find(v => v.id === "a-child").events.length, MAX_EVENTS);
  assert.equal(village.find(v => v.id === "a-child").residency, "visitor");
  assert.equal(village.find(v => v.id === "z-parent").residency, "resident");
});

test("an unvalidated declaration can style a legacy soul but cannot grant residency", () => {
  const declaration = resident({ file: "unchecked.json", home: 7,
                                 agent_id: "unchecked", name: "Unverified" });
  delete declaration.valid;
  const [villager] = reduce([event("unchecked")], NOW, [declaration]);
  assert.equal(villager.name, "Unverified");
  assert.equal(villager.residency, "visitor");
  assert.equal(villager.home, null);
});

test("a resident manifest wins over a legacy soul for the same agent identity", () => {
  const manifest = resident({ file: "resident.resident.json", home: 3,
                              agent_id: "shared", name: "Resident" });
  const legacy = {
    file: "legacy.md", meta: { agent_id: "shared", name: "Legacy",
                                char: "Hunter", accent: "#a65b5b" },
  };
  const [villager] = reduce([event("shared")], NOW, [manifest, legacy]);

  assert.equal(villager.name, "Resident");
  assert.equal(villager.residency, "resident");
  assert.equal(villager.home, 3);
});

test("a resident manifest wins over a legacy soul for the same project", () => {
  const manifest = resident({ file: "resident.resident.json", home: 4,
                              project: "burrow", name: "Resident" });
  const legacy = {
    file: "legacy.md", meta: { project: "burrow", name: "Legacy",
                                char: "Hunter", accent: "#a65b5b" },
  };
  const [villager] = reduce([event("visitor")], NOW, [manifest, legacy]);

  assert.equal(villager.name, "Resident");
  assert.equal(villager.residency, "resident");
  assert.equal(villager.home, 4);
});
