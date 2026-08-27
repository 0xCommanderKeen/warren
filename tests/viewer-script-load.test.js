"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "viewer/index.html"), "utf8");
const scripts = [...html.matchAll(/<script src="([^"]+)"><\/script>/g)]
  .map(match => match[1]);
const moduleGlobals = new Map([
  ["/presence.js", "BurrowPresence"], ["/sprites.js", "BurrowSprites"],
  ["/retention-policy.js", "BurrowRetentionPolicy"],
  ["/journal-observations.js", "BurrowJournals"],
  ["/typed-json.js", "BurrowTypedJSON"], ["/routes.js", "BurrowRoutes"],
  ["/destinations.js", "BurrowDestinations"],
  ["/routine-lifecycle.js", "BurrowRoutineLifecycle"],
  ["/fleet-operations.js", "BurrowFleet"],
  ["/fleet-controller.js", "BurrowFleetController"],
  ["/routine-ledger.js", "BurrowRoutines"], ["/job-board.js", "BurrowJobs"],
  ["/approval-knocks.js", "BurrowApprovals"], ["/mood-glyph.js", "BurrowMoodGlyph"],
  ["/nursery.js", "BurrowNursery"], ["/charter-journal.js", "BurrowIdentity"],
  ["/state-transport.js", "BurrowStateTransport"],
  ["/village-adapter.js", "BurrowVillageAdapter"],
  ["/browser-runtime.js", "BurrowBrowser"],
]);

class XMLHttpRequest {
  open(_method, url) { this.url = url; }
  send() {
    assert.equal(this.url, "/retention-policy.json");
    this.status = 200;
    this.responseText = fs.readFileSync(path.join(root, "retention-policy.json"), "utf8");
  }
}

test("the real viewer script list evaluates in browser order", () => {
  const context = vm.createContext({
    console,
    globalThis: null,
    Phaser: {},
    XMLHttpRequest,
  });
  context.globalThis = context;

  for (const sourcePath of scripts) {
    const file = path.join(root, "viewer", sourcePath);
    assert.equal(fs.existsSync(file), true, `${sourcePath} must exist`);
    if (sourcePath.startsWith("/vendor/")) continue;
    assert.doesNotThrow(
      () => vm.runInContext(fs.readFileSync(file, "utf8"), context, { filename: file }),
      `${sourcePath} must evaluate with the globals provided by earlier scripts`,
    );
    const globalName = moduleGlobals.get(sourcePath);
    if (globalName) assert.ok(context[globalName], `${sourcePath} must expose ${globalName}`);
  }
});
