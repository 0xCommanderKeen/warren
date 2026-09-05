import { describe, expect, it } from "vitest";
import { buildingOccupancy } from "./occupancy.js";

describe("building occupancy", () => {
  it("counts current location rather than assigned homes", () => {
    const world = { agents: [{ id: "a", name: "Pip", buildingId: "workshop", state: "working" }] };
    expect(buildingOccupancy(world, { id: "home:a", kind: "home", agentIds: ["a"] }).summary).toBe("0 inside");
    expect(buildingOccupancy(world, { id: "workshop", kind: "workshop" })).toMatchObject({ count: 1, summary: "1 working", preview: "Pip" });
  });
  it("bounds preview names and distinguishes civic purposes", () => {
    const world = { agents: Array.from({length: 5}, (_, i) => ({ name: `Agent ${i}`, buildingId: "lodge:0" })) };
    expect(buildingOccupancy(world, { id: "lodge:0", kind: "lodge" }).preview).toBe("Agent 0, Agent 1, Agent 2 +2 more");
    expect(buildingOccupancy(world, { id: "archive", kind: "archive" }).summary).toBe("Village records");
  });
});
