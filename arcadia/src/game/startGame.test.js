import { beforeEach, describe, expect, it, vi } from "vitest";

const doubles = vi.hoisted(() => ({
  games: [],
  scenes: [],
}));

vi.mock("phaser", () => ({
  default: {
    AUTO: "auto",
    Scale: { FIT: "fit", RESIZE: "resize", CENTER_BOTH: "center" },
    Game: class Game {
      constructor(config) {
        this.config = config;
        this.destroy = vi.fn();
        doubles.games.push(this);
      }
    },
  },
}));

vi.mock("./VillageScene.js", () => ({
  createVillageScene(initialSnapshot) {
    return class VillageScene {
      constructor() {
        this.initialSnapshot = initialSnapshot;
        this.applySnapshot = vi.fn();
        doubles.scenes.push(this);
      }
    };
  },
}));

import { startGame } from "./startGame.js";

beforeEach(() => {
  doubles.games.length = 0;
  doubles.scenes.length = 0;
});

describe("startGame", () => {
  it("applies snapshots to the existing scene and destroys the existing game", () => {
    const initial = { generation: 1 };
    const next = { generation: 2 };
    const host = document.createElement("div");
    const running = startGame(host, initial);

    expect(doubles.games).toHaveLength(1);
    expect(doubles.scenes).toHaveLength(1);
    expect(doubles.games[0].config.scale.mode).toBe("fit");
    expect(doubles.games[0].config.width / doubles.games[0].config.height).toBe(5 / 3);
    expect(doubles.games[0].config.scene).toEqual([doubles.scenes[0]]);
    expect(doubles.scenes[0].initialSnapshot).toBe(initial);

    running.applySnapshot(next);
    expect(doubles.scenes[0].applySnapshot).toHaveBeenCalledWith(next);
    expect(doubles.games).toHaveLength(1);

    running.destroy(true);
    expect(doubles.games[0].destroy).toHaveBeenCalledWith(true);
  });
});
