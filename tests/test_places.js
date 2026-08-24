"use strict";

/* The projection names places; the viewer's PLACES table puts them on the map.
 * Only a string joins the two halves, and the failure mode of a broken join is
 * silence — the villager quietly works at home, which is exactly the bug this
 * suite exists to keep fixed. So check the join in CI, not just in a console.
 *
 * The viewer is one HTML file with no build step, so the place registry is
 * lifted out of it and evaluated directly: no second copy to keep in sync. */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const { PLACE_OF_VERB, VERBS } = require("../viewer/projection.js");
const { DELEGATION, destinationFor } = require("../viewer/destinations.js");
const html = fs.readFileSync("viewer/index.html", "utf8");

function lift(from, to) {
  const start = html.indexOf(from);
  assert.notEqual(start, -1, `viewer/index.html no longer contains "${from}"`);
  const end = html.indexOf(to, start);
  assert.notEqual(end, -1, `no "${to}" after "${from}" in viewer/index.html`);
  return html.slice(start, end + to.length);
}

const viewer = vm.runInNewContext(
  [lift("const PLACES = {", "\n};"),
   lift("function unmappedPlaces() {", "\n}\n"),
   "({ PLACES, unmappedPlaces })"].join("\n"),
  { PLACE_OF_VERB, DELEGATION },
);

test("every place the projection can name is on the map", () => {
  // spread: the array comes back from the vm realm, so compare contents
  const missing = [...viewer.unmappedPlaces()];
  assert.deepEqual(missing, [],
    "PLACE_OF_VERB in viewer/projection.js names a place the map cannot draw: " +
    missing.join(", "));
});

test("every building the projection can name has a label and a footprint", () => {
  for (const id of new Set(Object.values(PLACE_OF_VERB))) {
    if (id === DELEGATION) continue;
    const p = viewer.PLACES[id];
    assert.ok(p.label, `${id} has no label for the villager panel`);
    assert.ok(Number.isFinite(p.tx) && Number.isFinite(p.ty), `${id} has no position`);
  }
});

test("delegation is deliberately not a building", () => {
  // It means "handing work to somebody else", and resolves to a neighbour's
  // door. A PLACES entry for it would be a building nobody ever asked for.
  assert.equal(viewer.PLACES[DELEGATION], undefined);
});

test("every place is reached by a verb the tool table actually produces", () => {
  const verbs = new Set(Object.values(VERBS));
  for (const verb of Object.keys(PLACE_OF_VERB))
    assert.ok(verbs.has(verb), `no tool maps to "${verb}", so its place is unreachable`);
});

test("a place the map cannot draw sends the villager home, not to an exception", () => {
  const ctx = { places: viewer.PLACES, occupied: [], ownPlot: 0, hash: 1 };
  assert.deepEqual(destinationFor({ id: "a", state: "working", place: "librari" }, ctx),
                   { kind: "home" });
  assert.deepEqual(destinationFor({ id: "a", state: "working", place: "library" }, ctx),
                   { kind: "building", id: "library" });
});
