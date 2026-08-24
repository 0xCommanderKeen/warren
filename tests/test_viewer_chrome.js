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

console.log("viewer chrome reports truthful, accessible status");
