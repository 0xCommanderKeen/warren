import { expect, it } from "vitest";
import { createRoomLayout } from "./roomLayout.js";

it("reclaims departed desks while keeping resting residents and visitors in place across reloads", () => {
  const layout = createRoomLayout();
  const people = [{ id: "a", residency: "visitor" }, { id: "b", residency: "resident" }, { id: "c", residency: "visitor" }];
  const before = layout.update("workshop", people, people);
  const live = people.slice(1);
  const away = layout.update("workshop", [], live);
  expect(away.stations.map(s => s.id)).toEqual(["b", "c"]);
  expect(away.stations.every(s => s.agent === null)).toBe(true);
  expect(away.stations.map(s => s.position)).toEqual(before.stations.slice(1).map(s => s.position));
  const restored = createRoomLayout(JSON.parse(JSON.stringify(layout.serialize())));
  const newcomer = { id: "d" };
  const after = restored.update("workshop", [newcomer], [...live, newcomer]);
  expect(after.stations.find(s => s.id === "d").slot).toBe(0);
  expect(after.stations.find(s => s.id === "c").position).toEqual(before.stations[2].position);
});

it("cleans legacy reservations and stays bounded through session turnover", () => {
  let layout = createRoomLayout({ version: 1, rooms: [["workshop", Array.from({ length: 100 }, (_, i) => [`old-${i}`, i])]] });
  for (let i = 0; i < 150; i++) {
    const live = { id: `session-${i}` };
    const room = layout.update("workshop", [live], [live]);
    expect(room.stations.map(s => [s.id, s.slot])).toEqual([[live.id, 0]]);
    expect(room.width).toBe(11);
    layout = createRoomLayout(layout.serialize());
  }
  expect(layout.update("workshop", [], []).stations).toEqual([]);
});
