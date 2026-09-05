import { describe, expect, it } from "vitest";
import { createRoomLayout } from "./roomLayout.js";

const agent = id => ({ id, name: `Name ${id}`, state: "working" });

describe("personal room stations", () => {
  it("retains stations across arrivals, departures, room changes and reloads", () => {
    const layout = createRoomLayout();
    const before = layout.update("workshop", [agent("b"), agent("c")]);
    const after = layout.update("workshop", [agent("a"), agent("c")]);
    expect(after.stations.find(s => s.id === "c").position).toEqual(before.stations.find(s => s.id === "c").position);
    expect(after.stations.find(s => s.id === "b").agent).toBeNull();
    expect(after.stations.find(s => s.id === "b").position).toEqual(before.stations.find(s => s.id === "b").position);
    layout.update("home:c", [agent("c")]);
    const restored = createRoomLayout(JSON.parse(JSON.stringify(layout.serialize())));
    const returned = restored.update("workshop", [agent("b"), agent("c"), agent("a")]);
    expect(returned.stations.map(({ id, position }) => ({ id, position }))).toEqual(after.stations.map(({ id, position }) => ({ id, position })));
    expect(restored.update("workshop", []).stations.every(station => station.agent === null)).toBe(true);
  });

  it("expands symmetrically without moving stations and keeps 100 places separate", () => {
    const layout = createRoomLayout();
    const first = layout.update("workshop", [agent("z")]);
    const many = layout.update("workshop", Array.from({ length: 100 }, (_, i) => agent(String(i))));
    expect(many.stations[0].position).toEqual(first.stations[0].position);
    expect(new Set(many.stations.map(s => String(s.position))).size).toBe(101);
    expect(many.width).toBe(many.depth);
    for (const station of many.stations) for (const coordinate of station.position) expect(Math.abs(coordinate) + 2).toBeLessThan(many.width / 2);
    expect(layout.update("workshop", []).width).toBe(many.width);
  });

  it.each([
    null, {}, { version: 2, rooms: [] },
    { version: 1, rooms: [["w", [["a", 0], ["b", 0]]]] },
    { version: 1, rooms: [["w", [["a", 999999]]]] },
    { version: 1, rooms: [["w", [["a", NaN]]]] },
    { version: 1, rooms: [["w", [["a", 0], ["a", 1]]]] },
    { version: 1, rooms: [["w", Array(10001).fill(["a", 0])]] },
  ])("safely ignores invalid storage (%#)", saved => {
    expect(createRoomLayout(saved).update("w", [agent("new")]).stations[0].slot).toBe(0);
  });

  it("stores only IDs and slots with independent copies", () => {
    const layout = createRoomLayout();
    layout.update("w", [agent("a")]);
    const saved = layout.serialize();
    expect(saved).toEqual({ version: 1, rooms: [["w", [["a", 0]]]] });
    saved.rooms[0][1][0][0] = "changed";
    expect(layout.serialize().rooms[0][1][0][0]).toBe("a");
  });
});
