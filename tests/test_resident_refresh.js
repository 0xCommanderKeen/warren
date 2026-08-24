"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync("viewer/index.html", "utf8");
const start = html.indexOf("let souls = [];");
const end = html.indexOf("/* ————— boot", start);

const oldResident = { file: "old.resident.json", valid: true, manifest_version: 1,
  meta: { project: "burrow", name: "Old" } };
const newResident = { file: "new.resident.json", valid: true, manifest_version: 1,
  meta: { project: "burrow", name: "New" } };
const legacySoul = { file: "legacy.md", meta: { agent_id: "legacy", name: "Legacy" } };

const context = {
  Map, Date, Promise,
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
  parseEvents: require("../viewer/projection.js").parseEvents,
  foldEvents() {}, foldArtifacts() {}, reduce() { return []; },
  beatEl: {}, renderChrome() {}, scene: null,
  console: { warn() {} },
};
vm.createContext(context);
vm.runInContext(html.slice(start, end) + `
  this.residentRefresh = { poll, refreshResidents, souls: () => souls };
`, context);

(async () => {
  await context.residentRefresh.poll();
  assert.deepEqual(context.residentRefresh.souls().map(soul => soul.file),
                   ["old.resident.json", "legacy.md"]);
  await context.residentRefresh.refreshResidents();
  assert.deepEqual(context.residentRefresh.souls().map(soul => soul.file),
                   ["new.resident.json", "legacy.md"]);
  console.log("resident refresh preserves legacy soul styling");
})().catch(error => { console.error(error); process.exitCode = 1; });
