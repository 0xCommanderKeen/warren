import { describe, expect, it, vi } from "vitest";
vi.mock("phaser", () => ({ default: { Scene: class {}, Tilemaps: { Formats: { TILED_JSON: "tiled-json" } } } }));
vi.mock("./EventBus.js", () => ({ EventBus: { emit: vi.fn() } }));
import { createVillageScene } from "./VillageScene.js";

const resident = (overrides = {}) => ({ id: "keeper", name: "Keeper", char: "Monk", residency: "resident", home: 2, state: "working", ...overrides });
function object() {
  const obj = {};
  for (const method of ["setText", "setFillStyle", "setAlpha", "setVisible", "setDepth", "stop", "setTexture", "play", "destroy", "pause", "resume"]) obj[method] = vi.fn(() => obj);
  obj.anims = { pause: vi.fn(), resume: vi.fn() };
  return obj;
}
function setup(villagers = [resident()]) {
  const Scene = createVillageScene({ villagers, approvals: [] });
  const scene = new Scene();
  scene.created = true;
  scene.tweens = { chain: vi.fn(() => object()) };
  scene.createHome = vi.fn(() => ({ ...object(), getByName: () => object() }));
  scene.createVillager = vi.fn(model => ({ body: { ...object(), x: model.x, y: model.y }, sprite: object(), label: object(), dot: object(), ring: object(), model }));
  scene.renderSnapshot();
  return scene;
}

describe("VillageScene snapshot reconciliation", () => {
  it("preserves container, home, and in-flight walk across unrelated snapshots", () => {
    const scene = setup();
    const entry = scene.villagers.get("keeper");
    const { body, home, tween } = entry;
    body.x = 410;
    scene.applySnapshot({ generation: 2, villagers: [resident({ name: "New name", last_line: "Changed activity" })], approvals: [] });
    expect(scene.villagers.get("keeper").body).toBe(body);
    expect(scene.villagers.get("keeper").home).toBe(home);
    expect(scene.villagers.get("keeper").tween).toBe(tween);
    expect(body.x).toBe(410);
    expect(tween.destroy).not.toHaveBeenCalled();
    expect(scene.createVillager).toHaveBeenCalledTimes(1);
    expect(scene.tweens.chain).toHaveBeenCalledTimes(1);
    expect(entry.label.setText).toHaveBeenCalledWith("New name");
  });

  it("tweens the existing container when its destination changes", () => {
    const scene = setup();
    const { body, tween, home } = scene.villagers.get("keeper");
    scene.applySnapshot({ villagers: [resident({ home: 1 })], approvals: [] });
    expect(scene.villagers.get("keeper").body).toBe(body);
    expect(body.destroy).not.toHaveBeenCalled();
    expect(home.destroy).toHaveBeenCalledOnce();
    expect(tween.destroy).toHaveBeenCalledOnce();
    expect(scene.tweens.chain.mock.lastCall[0].tweens.every(step => step.targets === body)).toBe(true);
    expect(scene.createVillager).toHaveBeenCalledTimes(1);
  });

  it("only destroys removed villagers and their own homes and walks", () => {
    const scene = setup([resident(), resident({ id: "other", home: 1 })]);
    const removed = scene.villagers.get("keeper");
    const kept = scene.villagers.get("other");
    scene.applySnapshot({ villagers: [resident({ id: "other", home: 1 })], approvals: [] });
    expect(removed.body.destroy).toHaveBeenCalledOnce();
    expect(removed.home.destroy).toHaveBeenCalledOnce();
    expect(removed.tween.destroy).toHaveBeenCalledOnce();
    expect(kept.body.destroy).not.toHaveBeenCalled();
    expect(kept.tween.destroy).not.toHaveBeenCalled();
    expect(scene.villagers.size).toBe(1);
  });

  it("keeps selection and paused motion across updates", () => {
    const scene = setup();
    scene.selectVillager("keeper");
    scene.setMotionPaused(true);
    scene.applySnapshot({ villagers: [resident({ home: 1 })], approvals: [] });
    const entry = scene.villagers.get("keeper");
    expect(entry.ring.setVisible).toHaveBeenLastCalledWith(true);
    expect(entry.tween.pause).toHaveBeenCalled();
    expect(entry.sprite.anims.pause).toHaveBeenCalled();
    scene.setMotionPaused(false);
    expect(entry.tween.resume).toHaveBeenCalled();
  });

  it("releases completed tweens before a later pause or snapshot", () => {
    const scene = setup();
    scene.applySnapshot({ villagers: [resident({ home: 1, state: "resting" })], approvals: [] });
    const entry = scene.villagers.get("keeper");
    const completed = entry.tween;
    scene.tweens.chain.mock.lastCall[0].onComplete();
    expect(entry.tween).toBeNull();
    scene.setMotionPaused(true);
    expect(completed.pause).not.toHaveBeenCalled();
    scene.applySnapshot({ villagers: [], approvals: [] });
    expect(completed.destroy).not.toHaveBeenCalled();
  });

  it("retains a pre-create snapshot for the initial render", () => {
    const Scene = createVillageScene({ villagers: [], approvals: [] });
    const scene = new Scene();
    const latest = { villagers: [resident()], approvals: [] };
    scene.renderSnapshot = vi.fn();
    scene.applySnapshot(latest);
    expect(scene.snapshot).toBe(latest);
    expect(scene.renderSnapshot).not.toHaveBeenCalled();
  });
});
