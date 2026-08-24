"use strict";

/* Destinations are values, not strings (viewer/destinations.js). These are the
 * rules the map is not allowed to break — above all that a villager is never
 * sent to a door with nobody behind it. */

const test = require("node:test");
const assert = require("node:assert/strict");

const { DELEGATION, destinationFor, destinationKey, delegatePlot } =
  require("../viewer/destinations.js");
const { PLACE_OF_VERB, reduce, hashCode } = require("../viewer/projection.js");
const fs = require("node:fs");

const working = place => ({ id: "x:one", state: "working", place });

test("the projection and the map agree on what delegation is called", () => {
  // The only string joining the two modules; a rename here fails loudly.
  assert.equal(PLACE_OF_VERB.delegating, DELEGATION);
});

test("each kind of destination is a distinct value", () => {
  assert.deepEqual(destinationFor(working("library")), { kind: "building", id: "library" });
  assert.deepEqual(destinationFor({ state: "knocking", place: null }), { kind: "door" });
  assert.deepEqual(destinationFor({ state: "resting", place: null }), { kind: "home" });
  // resting or idle is home even when the last action named a place
  assert.deepEqual(destinationFor({ state: "resting", place: "workshop" }), { kind: "home" });
});

test("a stale villager stays where its last event put it", () => {
  assert.deepEqual(destinationFor({ state: "stale", place: "workshop" }),
                   { kind: "building", id: "workshop" });
});

test("visitors share the lodge whenever resident work would happen at home", () => {
  const ctx = { places: { "visitor-lodge": {} }, occupied: [], ownPlot: -1, hash: 1 };
  assert.deepEqual(destinationFor({ state: "resting", base: "visitor-lodge" }, ctx),
                   { kind: "building", id: "visitor-lodge" });
  assert.deepEqual(destinationFor({ state: "working", base: "visitor-lodge", place: null }, ctx),
                   { kind: "building", id: "visitor-lodge" });
  assert.deepEqual(destinationFor({ state: "working", base: "visitor-lodge", place: "library" },
                                  { ...ctx, places: { ...ctx.places, library: {} } }),
                   { kind: "building", id: "library" });
});

test("destination keys separate every destination into its own slot bucket", () => {
  const keys = [
    destinationKey({ kind: "home" }), destinationKey({ kind: "door" }),
    destinationKey({ kind: "building", id: "library" }),
    destinationKey({ kind: "building", id: "workshop" }),
    destinationKey({ kind: "plot", plot: 1 }), destinationKey({ kind: "plot", plot: 2 }),
  ];
  assert.equal(new Set(keys).size, keys.length);
  // two villagers at one destination must share a bucket, or they overlap
  assert.equal(destinationKey({ kind: "building", id: "library" }),
               destinationKey({ kind: "building", id: "library" }));
});

test("delegation only ever targets a plot somebody is living in", () => {
  // The event says an Agent ran, never who took the work, so the map may claim
  // only that it went to somebody: an occupied door, never an empty one.
  const occupied = [0, 2, 5];
  for (const hash of [0, 1, 2, 3, 17, 4242]) {
    const dest = destinationFor(working(DELEGATION), { ownPlot: 0, occupied, hash });
    assert.equal(dest.kind, "plot");
    assert.ok(occupied.includes(dest.plot), `plot ${dest.plot} has nobody in it`);
    assert.notEqual(dest.plot, 0, "a villager cannot delegate to itself");
  }
});

test("delegation with nobody else in the village stays home", () => {
  assert.deepEqual(destinationFor(working(DELEGATION), { ownPlot: 3, occupied: [3], hash: 9 }),
                   { kind: "home" });
  assert.deepEqual(destinationFor(working(DELEGATION), { ownPlot: -1, occupied: [], hash: 9 }),
                   { kind: "home" });
});

test("visitor delegation with no resident returns to the shared lodge", () => {
  assert.deepEqual(destinationFor({ ...working(DELEGATION), base: "visitor-lodge" },
                                  { ownPlot: -1, occupied: [], hash: 9 }),
                   { kind: "building", id: "visitor-lodge" });
});

test("a delegating villager holds its door while that neighbour is home", () => {
  // A slot is held until its villager leaves: an arrival must not drag anyone
  // sideways, because that is movement no event asked for.
  const first = delegatePlot({ held: null, ownPlot: 0, occupied: [0, 4], hash: 7 });
  assert.equal(first, 4);
  assert.equal(delegatePlot({ held: first, ownPlot: 0, occupied: [0, 1, 4, 6], hash: 7 }), 4);
  assert.equal(delegatePlot({ held: first, ownPlot: 0, occupied: [0, 4], hash: 7 }), 4);
  // …and only moves when that neighbour actually leaves the village
  assert.equal(delegatePlot({ held: first, ownPlot: 0, occupied: [0, 6], hash: 7 }), 6);
});

test("the same villager picks the same door twice", () => {
  const pick = () => delegatePlot({ held: null, ownPlot: 1, occupied: [1, 2, 3], hash: 88 });
  assert.equal(pick(), pick());
  // and the order the village happened to be listed in cannot change it
  assert.equal(delegatePlot({ held: null, ownPlot: 1, occupied: [3, 2, 1], hash: 88 }), pick());
});

test("a traveler without a house can still delegate", () => {
  const dest = destinationFor(working(DELEGATION), { ownPlot: -1, occupied: [2], hash: 5 });
  assert.deepEqual(dest, { kind: "plot", plot: 2 });
});

test("the fixture delegates to the only other villager in the village", () => {
  // End to end over fixtures/meaningful-locations.jsonl: real events, real
  // projection, and the plot assignment the scene performs in list order.
  const lines = fs.readFileSync("fixtures/meaningful-locations.jsonl", "utf8")
    .split("\n").filter(Boolean);
  const now = Date.parse("2026-08-24T10:01:35.000Z");   // just after the Agent call
  const upTo = lines.filter(l => Date.parse(JSON.parse(l).ts) <= now);

  const plotsFor = village => new Map(village.map((v, i) => [v.id, i]));
  const walk = (village, id) => {
    const plots = plotsFor(village);
    const v = village.find(x => x.id === id);
    return destinationFor(v, { held: null, ownPlot: plots.get(id),
                               occupied: [...plots.values()], hash: hashCode(id) });
  };

  const village = reduce(upTo, now, []);
  const traveler = village.find(v => v.id === "fixture:traveler");
  assert.equal(traveler.place, DELEGATION);
  const dest = walk(village, "fixture:traveler");
  assert.deepEqual(dest, { kind: "plot", plot: plotsFor(village).get("fixture:crafter") });

  // Take the crafter out and there is nobody to hand work to: this unsouled
  // fixture villager returns to the shared lodge, never an empty resident home.
  const alone = reduce(upTo.filter(l => !l.includes("fixture:crafter")), now, []);
  assert.deepEqual(walk(alone, "fixture:traveler"),
                   { kind: "building", id: "visitor-lodge" });
});
