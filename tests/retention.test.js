"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const projection = require("../viewer/projection.js");
const retention = require("../viewer/retention-policy.js");

const matrix = JSON.parse(fs.readFileSync("tests/fixtures/retention-parity.json"));

test("shared retention fixture selects the same projection witnesses", () => {
  for (const item of matrix) {
    const selected = new Set(projection.projectionWitnesses(
      projection.parseEvents(item.events), item.now, retention.viewer_line_limit));
    const indexes = item.events.map((event, index) => selected.has(event) ? index : null)
      .filter(index => index !== null);
    assert.deepEqual(indexes, item.projection_witnesses, item.name);
  }
});
