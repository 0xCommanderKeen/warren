"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { CAPACITY, createRoutingMap, createSlotAllocator } = require("../viewer/routes.js");

const places = {
  library: { tx: 11, ty: 19, w: 3, h: 3 },
  workshop: { tx: 15, ty: 19, w: 3, h: 3 },
  "post-office": { tx: 19, ty: 19, w: 3, h: 3 },
  "visitor-lodge": { tx: 23, ty: 19, w: 3, h: 3 },
};
const plots = [[3,8],[8,8],[20,8],[25,8],[3,16],[8,16],[20,16],[25,16]];

function map(blockedTiles = []) {
  const blocked = new Set(blockedTiles.map(([x, y]) => `${x},${y}`));
  return createRoutingMap({ width: 32, height: 27, tileSize: 16, places, plots,
    ownDoor: { x: 248, y: 96 }, blocked: (x, y) => blocked.has(`${x},${y}`),
    costAt: () => 10 });
}

test("every shared destination promises at least eight distinct endpoints", () => {
  const routing = map();
  for (const id of Object.keys(places)) {
    const dest = { kind: "building", id };
    assert.ok(routing.capacity(dest) >= 8);
    const endpoints = Array.from({ length: routing.capacity(dest) }, (_, slot) =>
      routing.endpoint(dest, slot));
    assert.equal(new Set(endpoints.map(point => `${point.x},${point.y}`)).size, endpoints.length);
    assert.ok(endpoints.every(point => routing.walkable(
      routing.tileAt(point.x, point.y).x, routing.tileAt(point.x, point.y).y)));
  }
});

test("the lodge explicitly handles a representative burst of 32 visitors", () => {
  const routing = map(), dest = { kind: "building", id: "visitor-lodge" };
  assert.equal(routing.capacity(dest), 32);
  const allocator = createSlotAllocator(() => routing.capacity(dest));
  const slots = Array.from({ length: 32 }, (_, i) => allocator.allocate("building:visitor-lodge", `v${i}`));
  assert.deepEqual(slots, Array.from({ length: 32 }, (_, i) => i));
  assert.equal(allocator.allocate("building:visitor-lodge", "overflow"), null);
});

test("stable slots survive arbitrary fleet joins and leaves", () => {
  const allocator = createSlotAllocator(() => CAPACITY.shared);
  const original = new Map(Array.from({ length: 12 }, (_, i) => {
    const id = `resident-${i}`; return [id, allocator.allocate("building:library", id)];
  }));
  for (let i = 0; i < 4; i++) allocator.release(`resident-${i}`);
  for (let i = 0; i < 4; i++) allocator.allocate("building:library", `visitor-${i}`);
  for (const [id, slot] of [...original].slice(4))
    assert.equal(allocator.allocate("building:library", id), slot);
});

test("a full two-destination exchange reconciles atomically without spare capacity", () => {
  const allocator = createSlotAllocator(() => 2);
  assert.deepEqual(allocator.reconcile([
    { where: "a", id: "a1" }, { where: "a", id: "a2" },
    { where: "b", id: "b1" }, { where: "b", id: "b2" },
  ]), [0, 1, 0, 1]);
  const swapped = [
    { where: "b", id: "a2" }, { where: "b", id: "a1" },
    { where: "a", id: "b2" }, { where: "a", id: "b1" },
  ];
  assert.deepEqual(allocator.reconcile(swapped), [1, 0, 1, 0]);
  assert.deepEqual(allocator.reconcile([...swapped].reverse()), [0, 1, 0, 1],
    "claim order changes result order, not stable deterministic assignments");
});

test("an overflowing reconciliation leaves the previous allocation untouched", () => {
  const allocator = createSlotAllocator(() => 2);
  const original = [{ where: "lodge", id: "a" }, { where: "lodge", id: "b" }];
  assert.deepEqual(allocator.reconcile(original), [0, 1]);
  assert.equal(allocator.reconcile([...original, { where: "lodge", id: "overflow" }]), null);
  assert.deepEqual(allocator.reconcile(original), [0, 1]);
});

test("blocked goals and disconnected routes fail without appending a blocked point", () => {
  const goal = { x: 88, y: 40 };
  assert.equal(map([[5, 2]]).route({ x: 24, y: 40 }, goal), null);
  const wall = Array.from({ length: 27 }, (_, y) => [4, y]);
  assert.equal(map(wall).route({ x: 24, y: 40 }, { x: 120, y: 40 }), null);
});

test("map validation checks endpoint connectivity with one component traversal", () => {
  const wall = Array.from({ length: 27 }, (_, y) => [18, y]);
  const validation = map(wall).validate();
  assert.equal(validation.connected, false);
  assert.ok(validation.problems.some(problem => problem.includes("unreachable")));
  assert.ok(validation.reachableTiles < validation.walkableTiles);
});

test("map validation reports invalid extra-point collections without throwing", () => {
  for (const extraPoints of [null, false, 7, "street", {}]) {
    const validation = map().validate(extraPoints);
    assert.deepEqual(validation.problems, ["extra points must be an array"]);
    assert.equal(validation.connected, true);
  }
  assert.deepEqual(map().validate(undefined).problems, []);
});

test("map validation deterministically reports malformed extra points", () => {
  const validation = map().validate([
    null, undefined, false, 7, "street", 1n, Symbol("street"), {}, { x: 24 },
    { x: NaN, y: 24 }, { x: 24, y: Infinity },
  ]);
  assert.deepEqual(validation.problems, [
    "extra:0 invalid", "extra:1 invalid", "extra:2 invalid",
    "extra:3 invalid", "extra:4 invalid", "extra:5 invalid",
    "extra:6 invalid", "extra:7 invalid", "extra:8 invalid",
    "extra:9 invalid", "extra:10 invalid",
  ]);
  assert.equal(validation.connected, true);
});

test("map validation preserves useful names and still checks connectivity", () => {
  const wall = Array.from({ length: 27 }, (_, y) => [18, y]);
  const validation = map([[1, 1], ...wall]).validate([
    { name: "market crossing", x: 24, y: 24 },
    { name: "broken crossing", x: Infinity, y: 24 },
  ]);
  assert.ok(validation.problems.includes("market crossing blocked"));
  assert.ok(validation.problems.includes("broken crossing invalid"));
  assert.ok(validation.problems.includes("map disconnected"));
  assert.equal(validation.connected, false);
});

test("map validation never throws for revoked or hostile extra-point arrays", () => {
  const revoked = Proxy.revocable([], {});
  revoked.revoke();
  const hostileLength = new Proxy([], { get(target, key, receiver) {
    if (key === "length") throw new Error("length trap");
    return Reflect.get(target, key, receiver);
  }});
  const hostileElement = new Proxy([{ x: 24, y: 40 }], { get(target, key, receiver) {
    if (key === "0") throw new Error("element trap");
    return Reflect.get(target, key, receiver);
  }});
  const hostileFields = ["name", "x", "y"].map(field => new Proxy(
    { name: "crossing", x: 24, y: 40 }, { get(target, key, receiver) {
      if (key === field) throw new Error(`${field} trap`);
      return Reflect.get(target, key, receiver);
    }}));

  assert.deepEqual(map().validate(revoked.proxy).problems,
    ["extra points must be an array"]);
  assert.deepEqual(map().validate(hostileLength).problems,
    ["extra points length invalid"]);
  assert.deepEqual(map().validate(hostileElement).problems, ["extra:0 invalid"]);
  assert.deepEqual(map().validate(hostileFields).problems,
    ["extra:0 invalid", "crossing invalid", "crossing invalid"]);
  const wall = Array.from({ length: 27 }, (_, y) => [18, y]);
  const disconnected = map(wall).validate(revoked.proxy);
  assert.equal(disconnected.connected, false);
  assert.ok(disconnected.problems.includes("map disconnected"));
  assert.ok(disconnected.problems.some(problem => problem.includes("unreachable")));
});

test("map validation snapshots and bounds hostile extra-point lengths", () => {
  let lengthReads = 0;
  const mutatingLength = new Proxy([{ name: "crossing", x: 24, y: 40 }], {
    get(target, key, receiver) {
      if (key === "length") return ++lengthReads;
      return Reflect.get(target, key, receiver);
    },
  });
  const huge = new Proxy([], { get(target, key, receiver) {
    if (key === "length") return 1_000_000;
    if (typeof key === "string" && /^\d+$/.test(key)) return null;
    return Reflect.get(target, key, receiver);
  }});

  assert.deepEqual(map().validate(mutatingLength).problems, []);
  assert.equal(lengthReads, 1);
  const validation = map().validate(huge);
  assert.equal(validation.problems.length, 257);
  assert.equal(validation.problems[0], "extra points exceed limit of 256");
  assert.equal(validation.problems.at(-1), "extra:255 invalid");
  assert.equal(validation.connected, true);
});

test("non-finite, out-of-map, and blocked starts and targets are rejected", () => {
  const routing = map([[1, 1], [5, 2]]);
  const goodStart = { x: 40, y: 40 }, goodTarget = { x: 88, y: 56 };
  for (const bad of [
    { x: NaN, y: 40 }, { x: Infinity, y: 40 }, { x: -1, y: 40 },
    { x: 512, y: 40 }, { x: 40, y: 432 }, { x: 24, y: 24 },
  ]) assert.equal(routing.route(bad, goodTarget), null, `accepted start ${bad.x},${bad.y}`);
  for (const bad of [
    { x: 40, y: NaN }, { x: 40, y: -1 }, { x: 512, y: 40 },
    { x: 40, y: 432 }, { x: 88, y: 40 },
  ]) assert.equal(routing.route(goodStart, bad), null, `accepted target ${bad.x},${bad.y}`);
});

test("every successful property-style route contains only walkable endpoints", () => {
  const blocked = [[8,8],[9,8],[10,8],[8,9],[9,9],[10,9]];
  const routing = map(blocked);
  for (let sx = 1; sx < 8; sx += 2) for (let gy = 2; gy < 24; gy += 3) {
    const from = { x: sx * 16 + 8, y: 13 * 16 + 8 };
    const target = { x: 20 * 16 + 8, y: gy * 16 + 8 };
    const route = routing.route(from, target);
    if (!route) continue;
    for (const point of route) {
      const tile = routing.tileAt(point.x, point.y);
      assert.equal(routing.walkable(tile.x, tile.y), true);
    }
  }
});
