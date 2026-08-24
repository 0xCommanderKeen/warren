#!/usr/bin/env node

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const viewerPath = process.env.BURROW_VIEWER || new URL("../viewer/index.html", `file://${__filename}`);
const html = fs.readFileSync(viewerPath, "utf8");
const start = html.indexOf("const T = 16");
const end = html.indexOf("/* ————— DOM chrome", start);
const source = html.slice(start, end) + "\nthis.exports = { Village };";
const context = {
  Phaser: { Scene: class {} }, console,
  hashCode(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return Math.abs(h);
  },
};
vm.runInNewContext(source, context);
const { Village } = context.exports;

function village(blocked = []) {
  const v = new Village();
  v.blocked = Array.from({ length: 22 }, () => Array(32).fill(false));
  v.dirt = Array.from({ length: 22 }, () => Array(32).fill(false));
  for (const [x, y] of blocked) v.blocked[y][x] = true;
  return v;
}

function pathCost(v, path) {
  let cost = 0;
  let previousDirection = -1;
  for (let i = 1; i < path.length; i++) {
    const dx = path[i].x - path[i - 1].x;
    const dy = path[i].y - path[i - 1].y;
    const direction = dx === 1 ? 0 : dx === -1 ? 1 : dy === 1 ? 2 : 3;
    cost += v.tileCost(path[i].x, path[i].y);
    if (previousDirection >= 0 && previousDirection !== direction) cost += 2;
    previousDirection = direction;
  }
  return cost;
}

// This generated-map route used to cost 102 because a cheaper arrival at an
// intermediate tile pruned the heading that produces the optimal 100 route.
if (process.env.BURROW_CASE !== "no-path") {
  const v = village();
  const fluent = { setOrigin() { return this; }, setDepth() { return this; }, setVisible() { return this; } };
  v.add = { image() { return Object.create(fluent); } };
  v.make = { tilemap() { return {
    addTilesetImage() {}, createLayer() { return Object.create(fluent); },
  }; } };
  v.buildGround();
  for (const [px, py] of [[3,8],[8,8],[20,8],[25,8],[3,16],[8,16],[20,16],[25,16]])
    for (let y = py; y < py + 3; y++) for (let x = px; x < px + 4; x++) v.block(x, y);
  for (let y = 3; y < 6; y++) for (let x = 14; x < 17; x++) v.block(x, y);
  v.buildTrees();
  v.buildFences();
  v.buildDecor();
  const path = v.findPath(5, 3, 9, 3);
  assert.ok(path);
  assert.equal(pathCost(v, path), 100);
}

// A failed route must not fabricate a straight tween through a wall.
if (process.env.BURROW_CASE !== "cost") {
  const v = village(Array.from({ length: 22 }, (_, y) => [1, y]));
  let chained = false;
  v.route = () => null;
  v.tweens = { chain() { chained = true; } };
  const sprite = {
    texture: { key: "Villager-idle" }, stop() {}, setTexture() {}, play() {},
  };
  const viz = { cont: { x: 8, y: 8 }, spr: sprite, walk: null };
  const oldError = console.error;
  console.error = () => {};
  try {
    assert.equal(v.walkTo(viz, 40, 8), false);
  } finally {
    console.error = oldError;
  }
  assert.equal(chained, false);
  assert.deepEqual([viz.cont.x, viz.cont.y], [8, 8]);
}

console.log("pathfinding regressions: ok");
