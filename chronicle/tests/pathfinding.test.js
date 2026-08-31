#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const BurrowRoutes = require("../viewer/routes.js");

const html = fs.readFileSync("viewer/index.html", "utf8");
const start = html.indexOf("const T = 16");
const end = html.indexOf("/* ————— DOM chrome", start);
const source = html.slice(start, end) + "\nthis.exports = { Village };";
const context = {
  Phaser: { Scene: class {} }, console, PLACES: {}, BurrowRoutes,
  BurrowPresence: require("../viewer/presence.js"),
  BurrowDestinations: require("../viewer/destinations.js"),
  hashCode: () => 1,
};
vm.runInNewContext(source, context);
const { Village } = context.exports;

// This crosses the actual browser Village.walkTo adapter and the same routing
// module imported by routes.test.js. A route failure is a transaction failure:
// animation, target coordinates, projected state, and home presence all hold.
const village = new Village();
village.routing = { route: () => null };
village.tweens = { chain() { throw new Error("must not animate"); } };
const viz = {
  cont: { x: 8, y: 8 },
  spr: { texture: { key: "Villager-walk" }, stop() { throw new Error("must not stop sprite"); },
    setTexture() { throw new Error("must not retexture sprite"); }, play() {} },
  walk: { marker: "active tween", destroy() { throw new Error("must not destroy tween"); } },
  home: true, state: "working", target: { x: 8, y: 8 }, dest: { kind: "home", plot: 2 },
  slotKey: null, delegateTo: null,
};
const before = { x: viz.cont.x, y: viz.cont.y, home: viz.home,
  state: viz.state, target: viz.target, dest: viz.dest, slotKey: viz.slotKey,
  delegateTo: viz.delegateTo, walk: viz.walk, texture: viz.spr.texture.key };
const oldError = console.error;
console.error = () => {};
try { assert.equal(village.walkTo(viz, { x: 40, y: 8 }, false), false); }
finally { console.error = oldError; }
assert.deepEqual({ x: viz.cont.x, y: viz.cont.y, home: viz.home,
  state: viz.state, target: viz.target, dest: viz.dest, slotKey: viz.slotKey,
  delegateTo: viz.delegateTo, walk: viz.walk, texture: viz.spr.texture.key }, before);

console.log("pathfinding regressions: ok");
