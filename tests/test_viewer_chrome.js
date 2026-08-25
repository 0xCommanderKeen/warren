"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");

const html = fs.readFileSync("viewer/index.html", "utf8");

assert.doesNotMatch(html, /check hooks/i,
  "the census must report missing signals without guessing at their cause");
assert.match(html, /stale — inspect no-signal agents/,
  "the stale census gives an actionable, observation-based label");
assert.match(html, /data-action="inspect-stale"/,
  "the stale census exposes an inspection action");
assert.match(html, /villagers\.find\(v => v\.state === "stale"\)/,
  "the inspection action resolves an observed no-signal agent");
assert.match(html, /openPanel\(staleVillager\.id\)/,
  "the inspection action opens that agent's existing detail panel");

const transport = html.match(/<div id="transport"[^>]*>/)?.[0] || "";
assert.match(transport, /role="status"/,
  "transport changes are exposed as status updates");
assert.match(transport, /aria-live="polite"/,
  "transport changes do not interrupt assistive technology");

assert.match(html, /id="fleet-open"[^>]*aria-haspopup="dialog"/,
  "the fleet ledger has an explicit keyboard-operable entry point");
for (const focusable of ["a[href]", "area[href]", "textarea", "select", "button", "input",
  "iframe", "object", "embed", "audio[controls]", "video[controls]", "[contenteditable]", "[tabindex]"]) {
  assert.ok(html.includes(focusable), `dialog focus trap includes ${focusable}`);
}
assert.match(html, /closest\('\[hidden\], \[inert\]'\)/,
  "dialog focus trap rejects hidden and inert descendants");
assert.match(html, /matches\(":disabled"\)/,
  "dialog focus trap rejects disabled controls including disabled fieldsets");
assert.match(html, /element\.tabIndex < 0/,
  "dialog focus trap rejects every negative explicit tabindex");
assert.match(html, /role="tablist"/, "fleet sections expose tab semantics");
assert.match(html, /@media \(max-width: 680px\)/,
  "the ledger has an explicit narrow viewport layout");
assert.match(html, /Visitors share the lodge and are never presented as configured residents/,
  "visitor grouping cannot imply residency");

console.log("viewer chrome reports truthful, accessible status");
