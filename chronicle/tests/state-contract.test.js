"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { validateEnvelope } = require("../viewer/state-transport.js");

const directory = path.join(__dirname, "fixtures", "state-contract");
const fixtures = fs.readdirSync(directory).filter(name => name.endsWith(".json")).sort();
assert.ok(fixtures.length > 0, "at least one UI contract fixture exists");

for (const name of fixtures) {
  const envelope = JSON.parse(fs.readFileSync(path.join(directory, name), "utf8"));
  assert.equal(validateEnvelope(envelope), null, `${name} satisfies the UI client contract`);
  assert.ok(envelope.snapshot.villagers.length > 0, `${name} includes a villager`);
  assert.ok(envelope.snapshot.tasks.length > 0, `${name} includes a task`);
  assert.ok(envelope.snapshot.approvals.length > 0, `${name} includes an approval`);
}

console.log(`state contract: ${fixtures.length} fixture(s) valid`);
