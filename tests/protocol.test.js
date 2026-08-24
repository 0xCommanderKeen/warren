"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { validateEvent, reduce } = require("../viewer/projection.js");

const cases = JSON.parse(fs.readFileSync("tests/fixtures/protocol-v0-validation.json"));

test("ingestion and projection share the documented v0 fixture matrix", () => {
  for (const fixture of cases) {
    assert.equal(validateEvent(fixture.event) === null, fixture.valid, fixture.name);
  }
});

test("the shared timestamp range excludes ISO year zero in both adapters", () => {
  const fixture = cases.find(f => f.name === "year zero timestamp");
  assert.ok(fixture);
  assert.equal(validateEvent(fixture.event), fixture.error);
});

test("projection ignores every invalid contract fixture", () => {
  const invalid = cases.filter(f => !f.valid).map(f => f.event);
  assert.deepEqual(reduce(invalid, Date.parse("2026-08-24T12:00:01.000Z"), []), []);
});

test("a failed tool is an explicit terminal activity state", () => {
  const failed = cases.find(f => f.name === "valid failed tool").event;
  const [villager] = reduce([failed], Date.parse("2026-08-24T12:00:01.000Z"), []);
  assert.equal(villager.state, "failed");
  assert.equal(villager.lastLine, "Bash failed — exit code 1");
});
