"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const presence = require("../viewer/presence.js");

test("an away villager does not light their house until arriving home", () => {
  const viz = { home: false, state: "knocking" };

  presence.startJourney(viz, true);
  viz.state = "working";
  assert.deepEqual(presence.houseLight(viz), { home: false, working: false });

  presence.finishJourney(viz, true);
  assert.deepEqual(presence.houseLight(viz), { home: true, working: true });
});

test("a departing villager stops lighting their house when travel starts", () => {
  const viz = { home: true, state: "working" };

  presence.startJourney(viz, false);
  viz.state = "knocking";
  assert.deepEqual(presence.houseLight(viz), { home: false, working: false });
});

test("home-to-home movement keeps the house lit", () => {
  const viz = { home: true, state: "resting" };

  presence.startJourney(viz, true);
  viz.state = "working";
  assert.deepEqual(presence.houseLight(viz), { home: true, working: true });
});

test("stale state never claims known presence", () => {
  const viz = { home: true, state: "stale" };
  assert.deepEqual(presence.houseLight(viz), { home: false, working: false });
});

test("a villager at a shared place leaves its own house dark", () => {
  // Regression: spawning straight into the library (the viewer loads while the
  // research is already running) used to light the villager's own house.
  assert.equal(presence.atHome({ state: "working", place: "library" }), false);
  assert.equal(presence.atHome({ state: "stale", place: "library" }), false);

  const viz = { home: presence.atHome({ state: "working", place: "library" }),
                state: "working" };
  assert.deepEqual(presence.houseLight(viz), { home: false, working: false });
});

test("a villager working or resting at home lights it", () => {
  assert.equal(presence.atHome({ state: "working", place: null }), true);
  assert.equal(presence.atHome({ state: "resting", place: null }), true);
  assert.equal(presence.atHome({ state: "knocking", place: null }), false);
});
