import { describe, expect, it } from "vitest";

import mapJson from "../../public/assets/village.tmj?raw";
import { buildVillageModel, validateReachability } from "./villageModel.js";

const map = JSON.parse(mapJson);

describe("village map", () => {
  it("keeps every walkable tile connected to the street", () => {
    expect(validateReachability(map)).toEqual({ walkable: 180, reachable: 180 });
  });

  it("rejects open ground walled away from the street", () => {
    const blocked = structuredClone(map);
    const terrain = blocked.layers.find((layer) => layer.name === "Terrain");
    const collision = blocked.layers.find((layer) => layer.name === "Collision");
    const index = 10 * blocked.width + 18;

    terrain.data[index] = 1;
    collision.data[index - 1] = 3;
    collision.data[index - blocked.width] = 3;

    expect(() => validateReachability(blocked)).toThrow(
      "Village map has 1 unreachable walkable tile",
    );
  });
});

describe("village snapshot model", () => {
  it("puts residents at their numbered homes and visitors together at the Lodge", () => {
    const villagers = buildVillageModel(map, [
      { id: "resident", name: "Keeper", residency: "resident", home: 2, state: "resting" },
      { id: "visitor-a", name: "Ada", residency: "visitor", home: null, state: "working" },
      { id: "visitor-b", name: "Lin", residency: "visitor", home: null, state: "failed" },
    ]);

    expect(villagers.map(({ id, dwelling, x, y }) => ({ id, dwelling, x, y }))).toEqual([
      { id: "resident", dwelling: { kind: "home", label: "Home 2", x: 464, y: 96 }, x: 464, y: 112 },
      { id: "visitor-a", dwelling: { kind: "lodge", label: "Lodge", x: 320, y: 288 }, x: 304, y: 304 },
      { id: "visitor-b", dwelling: { kind: "lodge", label: "Lodge", x: 320, y: 288 }, x: 336, y: 304 },
    ]);
  });

  it("only gives working villagers a route away from home", () => {
    const villagers = buildVillageModel(map, [
      { id: "worker", name: "Worker", residency: "resident", home: 0, state: "working" },
      { id: "resting", name: "Resting", residency: "resident", home: 1, state: "resting" },
      { id: "stale", name: "Stale", residency: "resident", home: 2, state: "stale" },
    ]);

    expect(villagers[0].moving).toBe(true);
    expect(villagers[0].route).toEqual([
      { x: 112, y: 144 }, { x: 112, y: 176 }, { x: 112, y: 208 },
    ]);
    expect(villagers.slice(1).map(({ id, moving, route }) => ({ id, moving, route }))).toEqual([
      { id: "resting", moving: false, route: [] },
      { id: "stale", moving: false, route: [] },
    ]);
  });

  it("places only villagers backed by pending approvals at the operator's doorstep", () => {
    const villagers = buildVillageModel(map, [
      { id: "knocker-b", name: "B", residency: "resident", home: 1, state: "knocking" },
      { id: "resting", name: "Resting", residency: "resident", home: 2, state: "resting" },
      { id: "knocker-a", name: "A", residency: "visitor", home: null, state: "idle" },
    ], [
      { request_id: "approval-b", agent_id: "knocker-b", state: "pending" },
      { request_id: "approval-a", agent_id: "knocker-a", state: "pending" },
    ]);

    expect(villagers.map(({ id, x, y }) => ({ id, x, y }))).toEqual([
      { id: "knocker-b", x: 328, y: 192 },
      { id: "resting", x: 464, y: 112 },
      { id: "knocker-a", x: 312, y: 192 },
    ]);
  });

  it("does not draw a doorstep knock from villager state alone", () => {
    const [villager] = buildVillageModel(map, [
      { id: "stale-knock", name: "Stale", residency: "resident", home: 1, state: "knocking" },
    ], []);

    expect({ x: villager.x, y: villager.y }).toEqual({ x: 304, y: 112 });
  });
});

describe("population safety", () => {
  it("keeps a large visitor population inside walkable map bounds", () => {
    const visitors = Array.from({ length: 60 }, (_, i) => ({ id: `visitor-${i}`, residency: "visitor", state: "resting" }));
    for (const v of buildVillageModel(map, visitors)) {
      expect(v.x).toBeGreaterThan(16);
      expect(v.x).toBeLessThan(map.width * map.tilewidth - 16);
      expect(v.y).toBeLessThan(map.height * map.tileheight - 16);
      const index = Math.floor(v.y / map.tileheight) * map.width + Math.floor(v.x / map.tilewidth);
      expect(map.layers.find(l => l.name === "Collision").data[index]).not.toBe(3);
    }
  });

  it("keeps a working villager at the doorstep while an approval is pending", () => {
    const [v] = buildVillageModel(map, [{ id: "worker", residency: "resident", home: 0, state: "working" }], [{ agent_id: "worker", request_id: "yes", state: "pending" }]);
    expect(v.moving).toBe(false);
    expect(v.route).toEqual([]);
  });
});
