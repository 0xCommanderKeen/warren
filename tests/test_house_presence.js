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
