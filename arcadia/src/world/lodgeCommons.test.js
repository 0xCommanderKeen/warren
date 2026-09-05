import { describe, it, expect } from "vitest";
import { lodgeCommons } from "./lodgeCommons.js";
describe("lodge common room", () => {
  it("groups current guests by recorded project beyond the fixed bedrooms", () => {
    const room = lodgeCommons([{id:"a",project:"warren"},{id:"b",project:"warren"},{id:"c",project:"books"}], 14, 14);
    expect(room.tables.map(t => [t.project,t.agentIds])).toEqual([["books",["c"]],["warren",["a","b"]]]);
    expect(room.tables.every(t => t.position[1] > 7)).toBe(true);
  });
  it("furnishes an empty common room without inventing guests", () => {
    expect(lodgeCommons([], 11, 11).tables[0].agentIds).toEqual([]);
  });
});
