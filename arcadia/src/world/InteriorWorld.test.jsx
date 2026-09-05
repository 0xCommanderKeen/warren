import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { InteriorWorld } from "./InteriorWorld.jsx";

const renderer = vi.hoisted(() => ({ update: vi.fn(), dispose: vi.fn() }));
vi.mock("./interior.js", () => ({ createInteriorRenderer: () => renderer }));
afterEach(() => { cleanup(); localStorage.clear(); vi.clearAllMocks(); });

it("reconciles persisted desks against the full roster, including while the room is empty", () => {
  const key = "arcadia:room-layout:v1";
  localStorage.setItem(key, JSON.stringify({ version: 1, rooms: [["workshop", [["gone", 0], ["resting", 1]]]] }));
  const building = { id: "workshop" };
  const resting = { id: "resting", residency: "resident", state: "resting" };
  const agents = [];
  const view = render(<InteriorWorld building={building} agents={agents} villagers={[resting]} />);
  expect(renderer.update.mock.lastCall[0].room.stations.map(s => [s.id, s.slot, s.agent])).toEqual([["resting", 1, null]]);
  expect(JSON.parse(localStorage.getItem(key)).rooms[0][1]).toEqual([["resting", 1]]);
  view.rerender(<InteriorWorld building={building} agents={agents} villagers={[]} />);
  expect(renderer.update.mock.lastCall[0].room.stations).toEqual([]);
  expect(JSON.parse(localStorage.getItem(key)).rooms[0][1]).toEqual([]);
});
