"use strict";

const assert = require("node:assert/strict");
const { createBrowserRuntime } = require("../viewer/browser-runtime.js");

const oldResident = { file: "old.resident.json", valid: true, manifest_version: 1,
  meta: { project: "burrow", name: "Old" } };
const newResident = { file: "new.resident.json", valid: true, manifest_version: 1,
  meta: { project: "burrow", name: "New" } };
const legacySoul = { file: "legacy.md", meta: { agent_id: "legacy", name: "Legacy" } };

const runtime = createBrowserRuntime({
  now: () => Date.now(), EventSource: null, setTimeout() {}, clearTimeout() {},
  fetch(url) {
    if (url.startsWith("/events")) return Promise.resolve({
      ok: true, headers: { get: () => "10" }, text: async () => "",
    });
    if (url === "/villagers") return Promise.resolve({
      ok: true, json: async () => [oldResident, legacySoul],
    });
    if (url === "/residents") return Promise.resolve({
      ok: true, json: async () => ({ residents: [newResident], diagnostics: [] }),
    });
    throw new Error(`unexpected fetch ${url}`);
  },
});

(async () => {
  await runtime.poll();
  assert.deepEqual(runtime.snapshot().souls.map(soul => soul.file),
                   ["old.resident.json", "legacy.md"]);
  await runtime.refreshResidents();
  assert.deepEqual(runtime.snapshot().souls.map(soul => soul.file),
                   ["new.resident.json", "legacy.md"]);
  console.log("resident refresh preserves legacy soul styling");
})().catch(error => { console.error(error); process.exitCode = 1; });
