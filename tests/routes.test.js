"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { routePoints } = require("../viewer/routes.js");

function crossesRect(a, b, rect) {
  if (a.x === b.x) {
    return a.x >= rect.left && a.x <= rect.right &&
      Math.max(a.y, b.y) >= rect.top && Math.min(a.y, b.y) <= rect.bottom;
  }
  return a.y >= rect.top && a.y <= rect.bottom &&
    Math.max(a.x, b.x) >= rect.left && Math.min(a.x, b.x) <= rect.right;
}

test("upper-row villager approaches the library without crossing its footprint", () => {
  const start = { x: 80, y: 190 };
  const librarySlot = { x: 264, y: 310, approachX: 216 };
  const footprint = { left: 240, right: 288, top: 256, bottom: 304 };
  const points = routePoints(start, librarySlot);
  const segments = [start, ...points].slice(1).map((to, i) => [[start, ...points][i], to]);

  assert.equal(segments.some(([a, b]) => crossesRect(a, b, footprint)), false);
});
