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
assert.match(html, /id="job-board"|JOB_BOARD_ID = "job-board"/,
  "the square exposes a distinct job board");
assert.match(html, /data-post-job/, "the job board exposes its post form");
assert.match(html, /No tasks are open\./, "the empty job board states its truth plainly");
assert.match(html, /status-mark missing">absent/, "unknown claimants are explicitly absent");
assert.match(html, /jobAcks\.observe\(\{ events: view\.taskEvidence/,
  "the production UI acknowledges only runtime-published task evidence");
assert.match(html, /data-steward-change/, "Steward credentials can be changed accessibly");
assert.match(html, /data-steward-clear/, "Steward credentials can be cleared accessibly");
assert.match(html, /refreshJobListing\(view\.now\)/,
  "quiet clock ticks refresh the job list without replacing the post form");
assert.doesNotMatch(html, /(?:localStorage|sessionStorage)\s*\./,
  "Steward credentials are never persisted by the page");
assert.match(html, /data-approval-option/, "structured knocks expose contract options as buttons");
assert.match(html, /approvalAcks\.observe\(\{ events: view\.approvalEvidence/,
  "approval acknowledgement consumes only runtime-published closing evidence");
assert.match(html, /only the closing event releases the villager/,
  "the approval panel states its non-optimistic truth");
assert.match(html, /data-approval-edit/, "Steward's edit option has an accessible free-text bridge");

console.log("viewer chrome reports truthful, accessible status");
