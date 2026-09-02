import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const lifecycle = vi.hoisted(() => ({ starts: [], applies: [], destroys: [] }));

vi.mock("./startGame.js", () => ({
  startGame(parent, snapshot) {
    lifecycle.starts.push({ parent, snapshot });
    return {
      applySnapshot(next) { lifecycle.applies.push(next); },
      destroy(removeCanvas) { lifecycle.destroys.push(removeCanvas); },
    };
  },
}));
vi.mock("./EventBus.js", () => ({
  EventBus: { on: vi.fn(), off: vi.fn() },
}));

import { PhaserGame } from "./PhaserGame.jsx";

afterEach(() => cleanup());

describe("PhaserGame", () => {
  it("keeps the engine alive while applying new snapshots", () => {
    lifecycle.starts.length = 0;
    lifecycle.applies.length = 0;
    lifecycle.destroys.length = 0;
    const first = { generation: 1, villagers: [], approvals: [] };
    const second = { generation: 2, villagers: [], approvals: [] };
    const view = render(<PhaserGame snapshot={first} />);

    expect(lifecycle.starts).toHaveLength(1);
    expect(lifecycle.destroys).toHaveLength(0);
    view.rerender(<PhaserGame snapshot={second} />);

    expect(lifecycle.starts.map(({ snapshot }) => snapshot.generation)).toEqual([1]);
    expect(lifecycle.applies.map((snapshot) => snapshot.generation)).toEqual([2]);
    expect(lifecycle.destroys).toEqual([]);
    view.rerender(<PhaserGame snapshot={first} />);
    expect(lifecycle.applies.map((snapshot) => snapshot.generation)).toEqual([2, 1]);
    expect(lifecycle.starts).toHaveLength(1);
    expect(lifecycle.destroys).toEqual([]);
    view.unmount();
    expect(lifecycle.destroys).toEqual([true]);
  });
});
