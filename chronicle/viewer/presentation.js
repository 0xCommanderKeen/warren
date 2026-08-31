"use strict";

/* Rendering-only helpers. Domain state arrives already decided by Python. */
const NAMES = ["Bramble","Poppy","Wren","Sorrel","Fern","Alder","Maple","Rowan",
  "Thistle","Clover","Hazel","Juniper","Moss","Reed","Tansy","Willow"];
const CHARS = BurrowSprites.CHARS;
const PLACE_OF_VERB = { researching: "library", crafting: "workshop", tinkering: "workshop",
  emailing: "post-office", delegating: "delegation" };
function hashCode(value) { let hash = 0; for (const character of String(value))
  hash = (hash * 31 + character.charCodeAt(0)) | 0; return Math.abs(hash); }
function esc(value) { return String(value).replace(/[&<>"']/g, character =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[character]); }
function ago(timestamp, now) { const seconds = Math.max(0, Math.round((now - timestamp) / 1000));
  if (seconds < 60) return seconds + "s ago"; if (seconds < 3600) return Math.round(seconds / 60) + "m ago";
  return (seconds / 3600).toFixed(1) + "h ago"; }
function describe(record) { return record && (record.description || record.last_line ||
  String(record.type || "activity").replaceAll("_", " ")); }
// Legacy interaction trackers accept a validator argument, but authoritative
// snapshot acknowledgements never supply raw event evidence.
function validateEvent() { return "raw events are not a browser contract"; }
