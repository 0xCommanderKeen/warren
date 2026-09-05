import { describe, expect, it } from "vitest";
import { createVillageLayout } from "./layout.js";

const person = (id, extra = {}) => ({ id, name: id, residency: "resident", home: null,
  state: "resting", project: "", accent: "#ff9933", ...extra });
const snapshot = (villagers, approvals = []) => ({ villagers, approvals });
const byId = list => new Map(list.map(item => [item.id, item]));

function intersectsInterior(from, to, building) {
  const [x, z] = building.position;
  if (from[0] === to[0]) return Math.abs(from[0] - x) < building.width / 2 &&
    Math.min(from[1], to[1]) < z + building.depth / 2 && Math.max(from[1], to[1]) > z - building.depth / 2;
  return Math.abs(from[1] - z) < building.depth / 2 &&
    Math.min(from[0], to[0]) < x + building.width / 2 && Math.max(from[0], to[0]) > x - building.width / 2;
}

describe("expandable village layout", () => {
  it("restores retired plots, visitor slots and historical terrain across sessions", () => {
    const original = createVillageLayout();
    const people = [person("resident-z"), person("visitor-z", { residency: "visitor" }), person("retired")];
    const before = original.update(snapshot(people));
    original.update(snapshot(people.slice(0, 2)));
    const serialized = JSON.parse(JSON.stringify(original.serialize()));
    const restored = createVillageLayout(serialized);
    const after = restored.update(snapshot([person("earlier"), person("a-visitor", { residency: "visitor" }), ...people.slice(0, 2)]));
    for (const agent of before.agents.filter(agent => agent.id !== "retired")) {
      expect(byId(after.agents).get(agent.id).position).toEqual(agent.position);
    }
    const retiredHome = byId(before.buildings).get("home:retired").position;
    expect(after.buildings.some(building => String(building.position) === String(retiredHome))).toBe(false);
    expect(restored.update(snapshot(people)).buildings.find(building => building.id === "home:retired").position).toEqual(retiredHome);
    expect(after.bounds.minX).toBeLessThanOrEqual(before.bounds.minX);
    expect(after.bounds.maxX).toBeGreaterThanOrEqual(before.bounds.maxX);
    expect(createVillageLayout(serialized).update(snapshot([])).bounds).toEqual(before.bounds);
  });

  it.each([
    state => { state.version = 2; },
    state => { state.allocated[0][1] = [NaN, 0]; },
    state => { state.allocated[0][1] = [1, 0]; },
    state => { state.allocated.push(["other", [...state.allocated[0][1]]]); },
    state => { state.allocated.push(["other", [100000, 0]]); },
    state => { state.allocated = Array(10001).fill(["other", [0, 0]]); },
    state => { state.groups = [["visitors", [["one", 0], ["two", 0]]]]; },
    state => { state.groups = [["visitors", [["one", -1]]]]; },
    state => { state.groups = [["visitors", [["one", 5]]]]; },
    state => { state.groups = [["visitors", Array(10001).fill(["one", 0])]]; },
    state => { state.bounds.maxX = 100000; },
    state => { state.bounds = null; },
  ])("ignores an invalid persisted state atomically (%#)", corrupt => {
    const layout = createVillageLayout();
    layout.update(snapshot([person("stored-resident"), person("visitor", { residency: "visitor" })]));
    const state = layout.serialize();
    corrupt(state);
    const input = snapshot([person("new-resident")]);
    expect(createVillageLayout(state).update(input)).toEqual(createVillageLayout().update(input));
  });

  it("serializes only bounded presentation state, with no snapshot payload or mutable aliases", () => {
    const layout = createVillageLayout();
    layout.update(snapshot([person("agent", { name: "PRIVATE NAME", history: [{ message: "PRIVATE HISTORY" }] })]));
    const state = layout.serialize();
    expect(Object.keys(state)).toEqual(["version", "allocated", "groups", "bounds"]);
    expect(JSON.stringify(state)).not.toContain("PRIVATE");
    expect(JSON.stringify(state).length).toBeLessThan(1000);
    state.allocated[0][1][0] = 999;
    state.bounds.minX = -999;
    expect(layout.serialize().allocated[0][1]).toEqual([0, 0]);
    expect(layout.serialize().bounds.minX).not.toBe(-999);
    const pristine = createVillageLayout();
    expect(createVillageLayout(pristine.serialize()).serialize()).toEqual(pristine.serialize());
  });

  it("provides a finite civic village before any agents arrive", () => {
    const world = createVillageLayout().update(snapshot([]));
    expect(world.agents).toEqual([]);
    expect(world.buildings.map(item => item.kind)).toEqual(["square", "lodge", "archive", "noticeboard", "workshop"]);
    expect(Object.values(world.bounds).every(Number.isFinite)).toBe(true);
    expect(world.roads.length).toBeGreaterThan(0);
  });

  it("keeps plots, appearances and visitor positions through additions, reorders, departures and returns", () => {
    const layout = createVillageLayout();
    const original = [person("b"), person("c", { residency: "visitor" }), person("d", { project: "app", state: "working" })];
    const first = layout.update(snapshot(original));
    const second = layout.update(snapshot([person("a", { home: 0 }), ...original.toReversed()]));
    for (const agent of first.agents) {
      expect(byId(second.agents).get(agent.id)).toEqual(agent);
    }
    for (const item of first.buildings) expect(byId(second.buildings).get(item.id).position).toEqual(item.position);
    layout.update(snapshot([]));
    expect(layout.update(snapshot(original))).toEqual(first);
  });

  it("uses one indoor workshop for all work and actual approvals for outdoor attention", () => {
    const people = [person("working", { state: "working", project: "garden" }),
      person("approval", { state: "working", project: "garden" }),
      person("knocking", { state: "knocking", pending_approval_ids: ["old"] }),
      ...["failed", "stale", "resting"].map(state => person(state, { state, project: "garden" })),
      person("unknown-work", { state: "working" })];
    const world = createVillageLayout().update(snapshot(people, [
      { agent_id: "approval", state: "pending" }, { agent_id: "knocking", state: "resolved" },
    ]));
    const agents = byId(world.agents);
    expect(agents.get("working").buildingId).toBe("workshop");
    expect(agents.get("unknown-work").buildingId).toBe("workshop");
    expect(agents.get("working").indoor).toBe(true);
    expect(world.buildings.filter(building => building.kind === "workshop")).toHaveLength(1);
    expect(byId(world.buildings).get("workshop").agentIds.toSorted()).toEqual(["unknown-work", "working"]);
    expect(agents.get("approval").buildingId).toBe("square");
    expect(agents.get("approval").indoor).toBe(false);
    for (const id of ["knocking", "failed", "stale", "resting"]) {
      expect(agents.get(id).buildingId).toBe(`home:${id}`);
      expect(agents.get(id).destination).toEqual(agents.get(id).position);
      expect(agents.get(id).indoor).toBe(true);
    }
    expect(agents.get("stale").state).toBe("stale");
    expect(agents.get("working").appearance.body).toBe("#ff9933");
  });

  it.each([5, 25, 100, 160])("lays out %i residents with safe bounds, clear routes and no overlapping buildings", count => {
    const people = Array.from({ length: count }, (_, i) => person(`agent-${i}`, {
      state: "working", project: `project-${i % 3}`, home: i % 2 ? Number.MAX_SAFE_INTEGER : -999999,
    }));
    const world = createVillageLayout().update(snapshot(people));
    expect(world.buildings.filter(item => item.kind === "home")).toHaveLength(count);
    expect(world.bounds.maxX - world.bounds.minX).toBeLessThan(200);
    for (let i = 0; i < world.buildings.length; i += 1) {
      const a = world.buildings[i];
      expect(a.position[0] - a.width / 2).toBeGreaterThan(world.bounds.minX);
      expect(a.position[1] + a.depth / 2).toBeLessThan(world.bounds.maxZ);
      for (const b of world.buildings.slice(i + 1)) {
        expect(Math.abs(a.position[0] - b.position[0]) >= (a.width + b.width) / 2 ||
          Math.abs(a.position[1] - b.position[1]) >= (a.depth + b.depth) / 2).toBe(true);
      }
    }
    expect(world.buildings.filter(building => building.kind === "workshop")).toHaveLength(1);
    expect(new Set(world.agents.map(agent => String(agent.destination))).size).toBe(1);
    expect(world.agents.every(agent => agent.indoor && agent.buildingId === "workshop")).toBe(true);
    for (const agent of world.agents) {
      expect(agent.route[0]).toEqual(agent.position);
      expect(agent.route.at(-1)).toEqual(agent.destination);
      for (let i = 1; i < agent.route.length; i += 1) {
        for (const building of world.buildings) {
          expect(intersectsInterior(agent.route[i - 1], agent.route[i], building)).toBe(false);
        }
      }
    }
  });

  it("shares lodge entrances while retaining distinct outdoor positions for 100 approvals", () => {
    const layout = createVillageLayout();
    const people = Array.from({ length: 100 }, (_, i) => person(`guest-${i}`, { residency: "visitor" }));
    const approvals = people.map(agent => ({ agent_id: agent.id, state: "pending" }));
    const world = layout.update(snapshot(people, approvals));
    expect(new Set(world.agents.map(agent => String(agent.position))).size).toBe(Math.ceil(100 / 12));
    expect(new Set(world.agents.map(agent => String(agent.destination))).size).toBe(100);
    expect(world.agents.every(agent => !agent.indoor)).toBe(true);
    const after = layout.update(snapshot(people.slice(1), approvals.slice(1)));
    for (const agent of after.agents) expect(agent).toEqual(byId(world.agents).get(agent.id));
  });

  it("routes a state change from the previous destination rather than replaying the arrival", () => {
    const layout = createVillageLayout();
    const working = person("keeper", { state: "working", project: "garden" });
    const first = layout.update(snapshot([working])).agents[0];
    const waiting = layout.update(snapshot([working], [{ agent_id: "keeper", state: "pending" }])).agents[0];
    expect(waiting.position).toEqual(first.position);
    expect(waiting.route[0]).toEqual(first.destination);
    expect(waiting.route.at(-1)).toEqual(waiting.destination);
    const resting = layout.update(snapshot([{ ...working, state: "resting" }])).agents[0];
    expect(resting.route[0]).toEqual(waiting.destination);
    expect(resting.route.at(-1)).toEqual(first.position);
  });

  it("retains terrain and street extents after outlying residents depart", () => {
    const layout = createVillageLayout();
    const people = Array.from({ length: 100 }, (_, i) => person(`a${String(i).padStart(3, "0")}`, {
      state: "working", project: "one",
    }));
    const populated = layout.update(snapshot(people));
    const worker = { ...people[0], project: "new-project" };
    const working = layout.update(snapshot([worker]));
    const resting = layout.update(snapshot([{ ...worker, state: "resting", project: "" }]));
    expect(resting.buildings.some(building => building.project === "new-project")).toBe(false);
    expect(resting.agents[0].route[0]).toEqual(working.agents[0].destination);
    expect(resting.bounds).toEqual(working.bounds);
    for (const world of [populated, working, resting]) {
      for (const agent of world.agents) for (const [x, z] of agent.route) {
        expect(x).toBeGreaterThan(resting.bounds.minX);
        expect(x).toBeLessThan(resting.bounds.maxX);
        expect(z).toBeGreaterThan(resting.bounds.minZ);
        expect(z).toBeLessThan(resting.bounds.maxZ);
      }
    }
    const empty = layout.update(snapshot([]));
    expect(empty.bounds).toEqual(resting.bounds);
    // Main streets remain; removed buildings' private doorstep paths may disappear.
    expect(empty.roads.filter(road => road.width === 1.25))
      .toEqual(working.roads.filter(road => road.width === 1.25));
    expect(empty.agents).toEqual([]);
    expect(empty.buildings).toHaveLength(5);
  });

  it("keeps the shared workshop across project changes and sends resting visitors indoors", () => {
    const layout = createVillageLayout();
    const worker = person("worker", { state: "working", project: "first" });
    const first = layout.update(snapshot([worker]));
    const second = layout.update(snapshot([{ ...worker, project: "second" }, person("visitor", { residency: "visitor" })]));
    expect(byId(second.buildings).get("workshop").position).toEqual(byId(first.buildings).get("workshop").position);
    expect(byId(second.agents).get("worker").destination).toEqual(first.agents[0].destination);
    expect(byId(second.agents).get("visitor").indoor).toBe(true);
    expect(byId(second.agents).get("visitor").buildingId).toBe("lodge:0");
    expect(second.buildings.some(building => building.project)).toBe(false);
    const restored = createVillageLayout(JSON.parse(JSON.stringify(layout.serialize()))).update(snapshot([worker]));
    expect(byId(restored.buildings).get("workshop").position).toEqual(byId(first.buildings).get("workshop").position);
  });

  it("restores version-one retired project workshop plots without creating extra workshops", () => {
    const layout = createVillageLayout();
    const before = layout.update(snapshot([person("resident")]));
    const saved = layout.serialize();
    // A prior UI version reserved project workshops; keep their plots retired.
    const allocation = saved.allocated.find(entry => entry[0] === "workshop");
    allocation[0] = "workshop:legacy-project:0";
    saved.groups.push(["project:legacy-project", [["resident", 0]]]);
    const restored = createVillageLayout(saved).update(snapshot([person("resident", { state: "working" })]));
    expect(restored.buildings.filter(building => building.kind === "workshop")).toHaveLength(1);
    expect(byId(restored.buildings).get("home:resident").position).toEqual(byId(before.buildings).get("home:resident").position);
    expect(byId(restored.buildings).get("workshop").position).not.toEqual(allocation[1]);
  });

  it("is deterministic for the same population and does not mutate a snapshot", () => {
    const people = [person("a", { accent: "bad-color" }), person("b", { residency: "visitor" })];
    const input = snapshot(people);
    const before = structuredClone(input);
    const a = createVillageLayout().update(input);
    const b = createVillageLayout().update(snapshot(people.toReversed()));
    expect(a).toEqual(b);
    expect(input).toEqual(before);
    expect(a.agents[0].appearance.body).toMatch(/^#[0-9a-f]{6}$/i);
  });
});
