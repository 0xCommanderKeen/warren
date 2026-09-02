import { describe, expect, it, vi } from "vitest";

vi.mock("phaser", () => ({
  default: {
    Scene: class Scene {},
    Tilemaps: { Formats: { TILED_JSON: "tiled-json" } },
  },
}));
vi.mock("./EventBus.js", () => ({
  EventBus: { emit: vi.fn() },
}));

import { createVillageScene } from "./VillageScene.js";

describe("VillageScene snapshots", () => {
  it("reconciles snapshot objects without rebuilding the scene", () => {
    const initial = { villagers: [], approvals: [] };
    const next = {
      villagers: [{
        id: "keeper",
        name: "Keeper",
        residency: "resident",
        home: 2,
        state: "resting",
      }],
      approvals: [],
    };
    const Scene = createVillageScene(initial);
    const scene = new Scene();
    const previous = { destroy: vi.fn() };
    scene.created = true;
    scene.snapshotObjects = [previous];
    scene.tweens = { killTweensOf: vi.fn() };
    scene.createHome = vi.fn();
    scene.createVillager = vi.fn();

    scene.applySnapshot(next);

    expect(scene.snapshot).toBe(next);
    expect(scene.tweens.killTweensOf).toHaveBeenCalledWith(previous);
    expect(previous.destroy).toHaveBeenCalledOnce();
    expect(scene.createHome).toHaveBeenCalledWith(expect.objectContaining({
      id: "keeper",
      x: 464,
      y: 112,
    }));
    expect(scene.createVillager).toHaveBeenCalledWith(expect.objectContaining({
      id: "keeper",
      x: 464,
      y: 112,
    }));
  });

  it("retains a pre-create snapshot for the initial render", () => {
    const Scene = createVillageScene({ villagers: [], approvals: [] });
    const scene = new Scene();
    const latest = { villagers: [{ id: "latest" }], approvals: [] };
    scene.renderSnapshot = vi.fn();

    scene.applySnapshot(latest);

    expect(scene.snapshot).toBe(latest);
    expect(scene.renderSnapshot).not.toHaveBeenCalled();
  });
});
