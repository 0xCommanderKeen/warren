import { describe, expect, it } from "vitest";

import mapJson from "../../public/assets/village.tmj?raw";
import { buildVillageModel, validateReachability } from "./villageModel.js";

const map = JSON.parse(mapJson);

describe("village map", () => {
  it("keeps every walkable tile connected to the street", () => {
    expect(validateReachability(map)).toEqual({ walkable: 148, reachable: 148 });
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
      { id: "visitor-a", dwelling: { kind: "lodge", label: "Lodge", x: 320, y: 288 }, x: 320, y: 304 },
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
});
