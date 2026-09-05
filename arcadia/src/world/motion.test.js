import { describe, expect, it } from "vitest";
import { advanceMotion, createMotion, retargetMotion } from "./motion.js";

const journey = (destination, route) => ({ destination, route });
const axisAligned = (a, b) => a[0] === b[0] || a[1] === b[1];

describe("village movement", () => {
  it("starts at its recorded destination with no replay", () => {
    const destination = [3, 4];
    const motion = createMotion(destination);
    destination[0] = 99;
    expect(motion).toEqual({ position: [3, 4], destination: [3, 4], points: [], step: 0 });
    expect(advanceMotion(motion, 1)).toEqual({ walking: false, heading: 0 });
  });

  it("keeps street-safe remaining segments through interrupted travel", () => {
    const motion = createMotion([0, 0]);
    retargetMotion(motion, journey([10, 10], [[0, 0], [0, 10], [10, 10]]));
    advanceMotion(motion, 3, 1);
    expect(motion.position).toEqual([0, 3]);
    retargetMotion(motion, journey([20, 0], [[10, 10], [20, 10], [20, 0]]));
    expect(motion.points).toEqual([[0, 10], [10, 10], [20, 10], [20, 0]]);
    const path = [motion.position, ...motion.points];
    expect(path.slice(1).every((point, i) => axisAligned(path[i], point))).toBe(true);
    advanceMotion(motion, 12, 1);
    expect(motion.position).toEqual([5, 10]);
    // A second interruption must retain both outstanding routes, not cut across a plot.
    retargetMotion(motion, journey([30, 0], [[20, 0], [30, 0]]));
    expect(motion.points).toEqual([[10, 10], [20, 10], [20, 0], [30, 0]]);
    advanceMotion(motion, 1000, 1);
    expect(motion.position).toEqual([30, 0]);
  });

  it("does not restart movement on repeated identical snapshots", () => {
    const motion = createMotion([0, 0]);
    const model = journey([10, 0], [[0, 0], [5, 0], [10, 0]]);
    retargetMotion(motion, model);
    advanceMotion(motion, 6, 1);
    const before = structuredClone(motion);
    retargetMotion(motion, model);
    expect(motion).toEqual(before);
    advanceMotion(motion, 1, 1);
    expect(motion.position).toEqual([7, 0]);
  });

  it("snaps paused and reduced-motion updates to current truth, including an unchanged destination", () => {
    const motion = createMotion([0, 0]);
    const model = journey([10, 0], [[0, 0], [10, 0]]);
    retargetMotion(motion, model);
    advanceMotion(motion, 1, 1);
    retargetMotion(motion, model, true);
    expect(motion.position).toEqual([10, 0]);
    expect(motion.points).toEqual([]);
    retargetMotion(motion, journey([10, 10], [[10, 0], [10, 10]]), true);
    expect(motion.position).toEqual([10, 10]);
    expect(advanceMotion(motion, 1).walking).toBe(false);
  });

  it("skips duplicate points, follows bends, and settles without overshooting", () => {
    const motion = createMotion([0, 0]);
    retargetMotion(motion, journey([2, 3], [[0, 0], [0, 0], [2, 0], [2, 0], [2, 3], [2, 3]]));
    expect(motion.points).toEqual([[2, 0], [2, 3]]);
    const first = advanceMotion(motion, 1, 1);
    expect(first).toEqual({ walking: true, heading: Math.PI / 2 });
    expect(motion.position).toEqual([1, 0]);
    const second = advanceMotion(motion, 2, 1);
    expect(second).toEqual({ walking: true, heading: 0 });
    expect(motion.position).toEqual([2, 1]);
    expect(advanceMotion(motion, 10000, 1).walking).toBe(false);
    expect(motion.position).toEqual([2, 3]);
    expect(motion.points).toEqual([]);
    expect(advanceMotion(motion, 10000, 1).walking).toBe(false);
    expect(motion.position).toEqual([2, 3]);
  });

  it("leaves caller routes unchanged and does not move for invalid time budgets", () => {
    const motion = createMotion([0, 0]);
    const model = journey([2, 0], [[0, 0], [2, 0]]);
    const original = structuredClone(model);
    retargetMotion(motion, model);
    for (const dt of [-1, 0, NaN, Infinity]) expect(advanceMotion(motion, dt).walking).toBe(false);
    expect(motion.position).toEqual([0, 0]);
    advanceMotion(motion, 1);
    expect(model).toEqual(original);
  });
});
