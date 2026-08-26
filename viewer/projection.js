"use strict";
/* burrow projection — events (docs/protocol.md, v0 rules) → villagers.
 *
 * Loaded two ways, both without a build step:
 *   - browser: <script src="/projection.js"> before the viewer's own script.
 *     Top-level declarations here are global lexical bindings, so index.html
 *     keeps using `reduce`, `describe`, `ago`, `esc`, `hashCode`, `CHARS` as-is.
 *   - node:    require("../viewer/projection.js") for the test suite.
 *
 * Everything in this file is pure: no DOM, no fetch, no clock. `reduce()` takes
 * `now` so the stale/drop windows are testable.
 */

const NAMES = ["Bramble","Poppy","Wren","Sorrel","Fern","Alder","Maple","Rowan",
               "Thistle","Clover","Hazel","Juniper","Moss","Reed","Tansy","Willow"];
const ACCENTS = ["#7d5ba6","#4f7d5b","#a65b5b","#5b7da6","#a68a4f","#5ba69b",
                 "#a65b8a","#6b7da0","#8aa65b","#a6764f"];
const SPRITES = typeof module === "object" && module.exports ?
  require("./sprites.js") : BurrowSprites;
const CHARS = SPRITES.CHARS;
const STALE_MS = 30 * 60 * 1000;
const DROP_MS  = 12 * 60 * 60 * 1000;
const MAX_EVENTS = 80;
const MAX_ARTIFACTS = 30;
const APPROVAL_ORDINALS = typeof module === "object" && module.exports ?
  require("./approval-knocks.js") : null;
const JOURNALS = typeof module === "object" && module.exports ?
  require("./journal-observations.js") : null;
const TYPED_JSON = typeof module === "object" && module.exports ?
  require("./typed-json.js") : BurrowTypedJSON;
const VERBS = {
  Read: "reading", Grep: "searching", Glob: "searching", WebSearch: "researching",
  WebFetch: "researching", Bash: "tinkering", Write: "crafting", Edit: "crafting",
  NotebookEdit: "crafting", Agent: "delegating", Task: "delegating",
  Email: "emailing", Inbox: "emailing",
  AskUserQuestion: "asking you", Skill: "consulting a manual", Workflow: "orchestrating",
};
/* Some verbs belong somewhere other than the villager's own house
 * (docs/protocol.md, "Where the work happens"). The viewer owns where a place
 * *is* on the map; the projection only decides which one a villager belongs at,
 * so this stays pure and testable. Every name here but `delegation` must match
 * the PLACES table in the viewer — an unknown name renders as "own house"
 * rather than throwing, and tests/test_places.js fails on the mismatch.
 *
 * `delegation` is the one entry that is not a building: it means "handing work
 * to somebody else", and the viewer resolves it to a neighbour's door. See
 * viewer/destinations.js.
 */
const PLACE_OF_VERB = {
  researching: "library",
  crafting: "workshop",
  tinkering: "workshop",
  emailing: "post-office",
  delegating: "delegation",
};

/* The place implied by an event, or null for "its own house". Only
 * `tool_called` moves anybody: every other event type leaves the verb unknown,
 * and no verb means no trip. */
function workPlace(ev) {
  if (!ev || ev.type !== "tool_called") return null;
  return PLACE_OF_VERB[VERBS[(ev.payload || {}).tool]] || null;
}

function hashCode(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}
function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
}
function ago(ts, now) {
  const s = Math.max(0, Math.round((now - ts) / 1000));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  return (s / 3600).toFixed(1) + "h ago";
}
function describe(ev) {
  const p = ev.payload || {};
  switch (ev.type) {
    case "task_started":      return "took up a task: “" + (p.prompt || "…") + "”";
    case "tool_called":       return (VERBS[p.tool] || "using " + p.tool) +
                                     (p.detail ? " — " + p.detail : "");
    case "tool_failed":       return (p.tool || "tool") + " failed" +
                                     (p.error ? " — " + p.error : "");
    case "artifact_produced": return "crafted " + (p.artifact || "something");
    case "heartbeat":         return "finished " + (p.tool || "a tool");
    case "needs_human":       return "needs you: " + (p.message || "(no message)");
    case "idle":              return "finished, resting";
    case "session_ended":     return "went home";
    case "journal_written":   return "wrote the journal for " + p.day + " after " + p.routine;
    default:                  return ev.type;
  }
}
function doingLabel(ev) {
  const p = ev.payload || {};
  switch (ev.type) {
    case "task_started":      return "starting a task";
    case "tool_called":       return VERBS[p.tool] || ("using " + p.tool);
    case "artifact_produced": return "crafting";
    default:                  return "";
  }
}
const EVENT_TYPES = new Set(["task_started","tool_called","tool_failed",
                             "artifact_produced","heartbeat","needs_human",
                             "idle","session_ended","routine_started",
                             "routine_finished","routine_failed","task_posted",
                             "task_claimed","task_done","task_failed",
                             "needs_human_resolved"]);
EVENT_TYPES.add("journal_written");
const ACTION_TYPES = new Set(["task_started","tool_called","artifact_produced"]);
const TIMESTAMP_V0 = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
const REQUIRED_PAYLOAD_TEXT = {
  task_started: ["prompt"], tool_called: ["tool"], tool_failed: ["tool"],
  artifact_produced: ["artifact"], needs_human: ["message"],
};
const OPTIONAL_PAYLOAD_TEXT = [
  "prompt", "tool", "artifact", "message", "detail", "error", "phase",
  "turn_id", "agent_type", "parent_agent_id",
];

/* The browser adapter for docs/protocol.md's strict v0 contract. Keep this
 * interface aligned with protocol.validate_event; one fixture matrix exercises
 * both so ingestion and projection cannot silently drift. */
function validateEvent(ev) {
  if (!exactPlainObject(ev)) return "event must be an object";
  if (!Number.isInteger(ev.v) || ev.v !== 0) return "unsupported protocol version";
  if (typeof ev.ts !== "string" || !TIMESTAMP_V0.test(ev.ts) ||
      ev.ts.slice(0, 4) === "0000" ||
      !Number.isFinite(Date.parse(ev.ts)) || new Date(ev.ts).toISOString() !== ev.ts) {
    return "invalid timestamp";
  }
  for (const field of ["source", "agent_id", "project"]) {
    if (typeof ev[field] !== "string" || !ev[field].trim()) return "invalid " + field;
  }
  if (Object.hasOwn(ev, "cwd") && typeof ev.cwd !== "string") return "invalid cwd";
  if (typeof ev.type !== "string" || !EVENT_TYPES.has(ev.type)) return "unsupported event type";
  if (!exactPlainObject(ev.payload)) {
    return "payload must be an object";
  }
  for (const field of REQUIRED_PAYLOAD_TEXT[ev.type] || []) {
    if (typeof ev.payload[field] !== "string" || !ev.payload[field].trim()) {
      return "invalid payload." + field;
    }
  }
  if (ev.type.startsWith("routine_")) {
    for (const field of ["routine", "run_id"]) {
      if (typeof ev.payload[field] !== "string" || !ev.payload[field].trim()) {
        return "invalid payload." + field;
      }
    }
    if (ev.type === "routine_started" && !["manual", "schedule"].includes(ev.payload.trigger)) {
      return "invalid payload.trigger";
    }
    if (ev.type === "routine_finished") {
      if (typeof ev.payload.outcome !== "string" || !ev.payload.outcome.trim()) return "invalid payload.outcome";
      const artifacts = ev.payload.artifacts;
      if (!Array.isArray(artifacts) || !artifacts.every(item => typeof item === "string" && item.trim())) {
        return "invalid payload.artifacts";
      }
      if (typeof ev.payload.duration_s !== "number" || !Number.isFinite(ev.payload.duration_s) || ev.payload.duration_s < 0) {
        return "invalid payload.duration_s";
      }
    }
    if (ev.type === "routine_failed") {
      if (typeof ev.payload.error !== "string" || !ev.payload.error.trim()) return "invalid payload.error";
      if (Object.hasOwn(ev.payload, "duration_s") &&
          (typeof ev.payload.duration_s !== "number" || !Number.isFinite(ev.payload.duration_s) || ev.payload.duration_s < 0)) {
        return "invalid payload.duration_s";
      }
    }
    // Steward is the authoritative producer of routine lifecycle facts. Other
    // emitters may use the protocol transport, but cannot manufacture routine
    // history or acknowledge a run-now request.
    if (ev.source !== "steward") return "routine events require source steward";
  }
  if (["task_posted", "task_claimed", "task_done", "task_failed"].includes(ev.type)) {
    for (const field of ["task_id", "title"]) {
      if (typeof ev.payload[field] !== "string" || !ev.payload[field].trim()) {
        return "invalid payload." + field;
      }
    }
    if (ev.type === "task_posted") {
      if (typeof ev.payload.posted_by !== "string" || !ev.payload.posted_by.trim()) {
        return "invalid payload.posted_by";
      }
      if (!Array.isArray(ev.payload.required_skills) ||
          !ev.payload.required_skills.every(item => typeof item === "string")) {
        return "invalid payload.required_skills";
      }
    } else {
      if (typeof ev.payload.claimant !== "string" || !ev.payload.claimant.trim()) {
        return "invalid payload.claimant";
      }
      if (ev.payload.claimant !== ev.agent_id) {
        return "payload.claimant must match agent_id";
      }
    }
    if (Object.hasOwn(ev.payload, "parent_task_id") &&
        (typeof ev.payload.parent_task_id !== "string" || !ev.payload.parent_task_id.trim())) {
      return "invalid payload.parent_task_id";
    }
    if (ev.type === "task_done" && (!Array.isArray(ev.payload.artifacts) ||
        !ev.payload.artifacts.every(item => typeof item === "string" && item.trim()))) {
      return "invalid payload.artifacts";
    }
    if (ev.type === "task_failed" &&
        (typeof ev.payload.reason !== "string" || !ev.payload.reason.trim())) {
      return "invalid payload.reason";
    }
    if (ev.source !== "steward") return "task events require source steward";
  }
  if (ev.type === "needs_human_resolved") {
    for (const field of ["request_id", "decided_by", "action"]) {
      if (typeof ev.payload[field] !== "string" || !ev.payload[field].trim()) {
        return "invalid payload." + field;
      }
    }
    if (!["approve", "deny", "edit"].includes(ev.payload.decision)) {
      return "invalid payload.decision";
    }
    if (ev.source !== "steward") return "approval resolutions require source steward";
  }
  if (ev.type === "journal_written") {
    const journalApi = JOURNALS || globalThis.BurrowJournals;
    if (!journalApi || typeof journalApi.validate !== "function") {
      return "journal validation unavailable";
    }
    const error = journalApi.validate(ev);
    if (error) return error;
  }
  for (const field of OPTIONAL_PAYLOAD_TEXT) {
    // Any attempted structured detail remains ingestible so its message can
    // degrade to a plain knock; approval-knocks.js owns the stricter shape.
    const structuredDetail = ev.type === "needs_human" && field === "detail";
    if (Object.hasOwn(ev.payload, field) && typeof ev.payload[field] !== "string" &&
        !structuredDetail) {
      return "invalid payload." + field;
    }
  }
  if (Object.hasOwn(ev.payload, "stop_hook_active") &&
      typeof ev.payload.stop_hook_active !== "boolean") {
    return "invalid payload.stop_hook_active";
  }
  return null;
}

/* One parse and validation pass per batch for village and notice board. */
const VALIDATED_BATCH = Symbol("burrow validated event batch");
const REJECTIONS = Symbol("burrow rejected event diagnostics");
const MOOD_AUTHORITY = Symbol("burrow internal mood authority");
const MOOD_AUTHORITY_COPIES = Symbol("burrow internal mood authority copies");
const MOOD_AUTHORITY_ORDINALS = Symbol("burrow internal mood authority ordinals");
const MOOD_AUTHORITY_ORDER = Symbol("burrow internal mood authority raw order");
const MOOD_AUTHORITY_RAW_INDEXES = Symbol("burrow internal mood authority raw indexes");
const MOOD_AUTHORITY_RAW_COUNT = Symbol("burrow internal mood authority raw count");
const MOOD_AUTHORITY_OVERFLOW = Symbol("burrow internal mood authority overflow");
const MOOD_AUTHORITY_OBSERVED = Symbol("burrow internal mood authority observed");
const MOOD_AUTHORITY_KIND = "mood-authority-v1";
const MOOD_AUTHORITY_ENCODING = "typed-binary64-v1";
const MOOD_AUTHORITY_MAX_BYTES = 32 * 1024;
// Capsule metadata is recursively canonicalized, so bound its container depth
// before that traversal. The capsule object itself is depth one; 64 leaves 60
// nested containers for a valid v0 needs_human detail value.
const MOOD_AUTHORITY_MAX_DEPTH = 64;
function exactDenseArray(value) {
  return TYPED_JSON.exactDenseArray(value);
}
function exactPlainObject(value) {
  return TYPED_JSON.exactPlainObject(value);
}
function jsonDomainWithin(value, maxDepth = Infinity) {
  const seen = new WeakSet(), active = new WeakSet();
  const stack = [{ value, depth: 0, exit: false }];
  try {
    while (stack.length) {
      const current = stack.pop(), item = current.value;
      if (current.exit) { active.delete(item); continue; }
      if (item === null || ["boolean", "number", "string"].includes(typeof item)) continue;
      if (typeof item !== "object") return false;
      const depth = current.depth + 1;
      // A parsed JSON value can never contain a repeated container identity.
      // Reject direct-object aliases globally, before a later canonicalizer
      // can expand a small shared DAG into exponentially many nodes.
      if (depth > maxDepth || seen.has(item)) return false;
      if (!Array.isArray(item) && !exactPlainObject(item)) return false;
      seen.add(item); active.add(item);
      stack.push({ value: item, depth, exit: true });
      if (Array.isArray(item)) {
        if (!exactDenseArray(item)) return false;
        for (let index = item.length - 1; index >= 0; index--) {
          stack.push({ value: item[index], depth, exit: false });
        }
      } else {
        const keys = Object.keys(item);
        for (let index = keys.length - 1; index >= 0; index--) {
          stack.push({ value: item[keys[index]], depth, exit: false });
        }
      }
    }
  } catch {
    return false;
  }
  return true;
}

/* JSON.parse deliberately accepts duplicate object names. Internal capsule
 * wire data does not: ambiguity at any envelope/graph/event nesting level
 * invalidates the capsule atomically. Public v0 parsing remains unchanged. */
function duplicateFreeJSON(text) {
  let at = 0;
  const whitespace = () => { while (/\s/.test(text[at] || "")) at++; };
  function string() {
    const start = at++;
    while (at < text.length) {
      const char = text[at++];
      if (char === '"') return JSON.parse(text.slice(start, at));
      if (char === "\\") {
        if (text[at] === "u") at += 5;
        else at++;
      }
    }
    throw new SyntaxError("unterminated JSON string");
  }
  function value(depth) {
    if (depth > MOOD_AUTHORITY_MAX_DEPTH + 4) throw new SyntaxError("JSON too deep");
    whitespace();
    if (text[at] === '"') { string(); return; }
    if (text[at] === "[") {
      at++; whitespace();
      if (text[at] === "]") { at++; return; }
      while (true) {
        value(depth + 1); whitespace();
        if (text[at] === "]") { at++; return; }
        if (text[at++] !== ",") throw new SyntaxError("invalid JSON array");
      }
    }
    if (text[at] === "{") {
      at++; whitespace(); const keys = new Set();
      if (text[at] === "}") { at++; return; }
      while (true) {
        if (text[at] !== '"') throw new SyntaxError("invalid JSON object key");
        const key = string();
        if (keys.has(key)) throw new SyntaxError("duplicate JSON object key");
        keys.add(key); whitespace();
        if (text[at++] !== ":") throw new SyntaxError("invalid JSON object");
        value(depth + 1); whitespace();
        if (text[at] === "}") { at++; return; }
        if (text[at++] !== ",") throw new SyntaxError("invalid JSON object");
        whitespace();
      }
    }
    const start = at;
    while (at < text.length && !/[\s,\]}]/.test(text[at])) at++;
    if (start === at) throw new SyntaxError("invalid JSON value");
    JSON.parse(text.slice(start, at));
  }
  try { value(0); whitespace(); return at === text.length; } catch { return false; }
}
function canonicalJSONString(value) {
  let encoded = '"';
  for (let index = 0; index < value.length; index++) {
    const code = value.charCodeAt(index);
    if (code === 0x22) encoded += '\\"';
    else if (code === 0x5c) encoded += "\\\\";
    else if (code === 0x08) encoded += "\\b";
    else if (code === 0x09) encoded += "\\t";
    else if (code === 0x0a) encoded += "\\n";
    else if (code === 0x0c) encoded += "\\f";
    else if (code === 0x0d) encoded += "\\r";
    else if (code >= 0x20 && code <= 0x7e) encoded += value[index];
    else encoded += "\\u" + code.toString(16).padStart(4, "0");
  }
  return encoded + '"';
}
const canonicalJSONStringify = value => TYPED_JSON.identity(value);
function encodedBytes(value) {
  const text = typeof value === "string" ? value : canonicalJSONStringify(value);
  if (typeof Buffer === "function") return Buffer.byteLength(text, "utf8");
  return new TextEncoder().encode(text).length;
}
function canonicalIdentity(value) { return TYPED_JSON.identity(value); }
function capsuleIdentityEqual(left, right) {
  try { return canonicalIdentity(left) === canonicalIdentity(right); } catch { return false; }
}
function validOrdinal(value) {
  return typeof value === "string" && /^(0|[1-9]\d*)$/.test(value) &&
    Number.isSafeInteger(Number(value));
}
function increasingOrdinals(values) {
  return values.every((value, index) => index === 0 ||
    Number(values[index - 1]) < Number(value));
}
function validRawIndex(value) {
  return typeof value === "string" && /^\d{16}$/.test(value) &&
    Number.isSafeInteger(Number(value));
}
function rawIndex(index) {
  return String(index).padStart(16, "0");
}
function parseMoodAuthority(item, encoded = item) {
  if (typeof encoded === "string" && !duplicateFreeJSON(encoded)) return null;
  // Validate the entire direct-object domain before reading even the envelope
  // discriminator. This prevents getters, hidden fields, symbols, and exotic
  // prototypes at any envelope/graph/event/payload depth from executing or
  // being silently discarded during canonicalization.
  if (!jsonDomainWithin(item, MOOD_AUTHORITY_MAX_DEPTH)) return null;
  if (exactPlainObject(item) && item.encoding === MOOD_AUTHORITY_ENCODING) {
    if (Object.keys(item).sort().join("|") !== "_burrow_internal|encoding|graph") return null;
    try {
      const logical = TYPED_JSON.decodeGraph(item.graph);
      if (!logical || typeof logical !== "object" || Array.isArray(logical)) return null;
      if (Object.keys(logical).sort().join("|") !==
          "copies|events|observed|ordinals|overflow|raw_count|raw_indexes|raw_ordinals") return null;
      item = { _burrow_internal: MOOD_AUTHORITY_KIND, ...logical };
    } catch { return null; }
  }
  const has = key => Object.hasOwn(item, key);
  if (!item || typeof item !== "object" || Array.isArray(item) ||
      Object.keys(item).sort().join("|") !==
        "_burrow_internal|copies|events|observed|ordinals|overflow|raw_count|raw_indexes|raw_ordinals" ||
      item._burrow_internal !== MOOD_AUTHORITY_KIND || !Array.isArray(item.events) ||
      !Array.isArray(item.ordinals) || item.ordinals.length !== item.events.length ||
      (has("copies") && !Array.isArray(item.copies)) ||
      (has("raw_ordinals") && !Array.isArray(item.raw_ordinals)) ||
      (has("raw_indexes") && !Array.isArray(item.raw_indexes)) ||
      typeof item.overflow !== "boolean" || typeof item.observed !== "number" ||
      !Number.isFinite(item.observed) || !Number.isInteger(item.observed) ||
      item.observed < 0 || item.observed > 257 ||
      (item.overflow ? item.observed !== 257 : item.observed !== item.events.length) ||
      encodedBytes(encoded) > MOOD_AUTHORITY_MAX_BYTES ||
      (item.overflow && (item.events.length || item.ordinals.length ||
        (item.copies || []).length || (item.raw_ordinals || []).length ||
        (item.raw_indexes || []).length))) return null;
  const events = [];
  for (let index = 0; index < item.events.length; index++) {
    const event = item.events[index];
    if (validateEvent(event)) return null;
    const payload = event.payload || {};
    const rootPrompt = event.type === "task_started" &&
      ["claude-code", "codex"].includes(event.source) &&
      !Object.hasOwn(payload, "parent_agent_id");
    if (!["needs_human", "needs_human_resolved"].includes(event.type) && !rootPrompt) return null;
    events.push(event);
  }
  const copies = item.copies;
  const rawOrdinals = item.raw_ordinals;
  const rawIndexes = item.raw_indexes;
  const rawCount = item.raw_count;
  if (rawIndexes.length !== rawOrdinals.length ||
      !validRawIndex(rawCount) || (item.overflow && Number(rawCount) !== 0) ||
      rawIndexes.some(index => Number(index) >= Number(rawCount)) ||
      !rawIndexes.every(validRawIndex) || !increasingOrdinals(rawIndexes) ||
      !item.ordinals.every(validOrdinal) || !increasingOrdinals(item.ordinals) ||
      !copies.every(validOrdinal) || !increasingOrdinals(copies) ||
      !rawOrdinals.every(validOrdinal) ||
      !increasingOrdinals(rawOrdinals)) return null;
  return { events, ordinals: item.ordinals, copies,
    rawOrdinals, rawIndexes, rawCount, overflow: item.overflow,
    observed: item.observed };
}
function parseItem(item) {
  try {
    let ev = item;
    if (typeof ev === "string") {
      try { ev = JSON.parse(ev); } catch { return { event: null, rejection: null }; }
    }
    // Recognize our reserved marker before walking hostile direct-object data:
    // malformed/cyclic metadata is internal null authority, never a public
    // protocol rejection, and cannot hide any following raw event.
    let internalMarker = false;
    try {
      if (ev && typeof ev === "object" && !Array.isArray(ev)) {
        const marker = Object.getOwnPropertyDescriptor(ev, "_burrow_internal");
        // An accessor in the reserved namespace is itself malformed internal
        // metadata; never invoke it or report it as a public protocol record.
        internalMarker = Boolean(marker && (!Object.hasOwn(marker, "value") ||
          marker.value === MOOD_AUTHORITY_KIND));
      }
    } catch { return { event: null, rejection: null }; }
    if (internalMarker) return { event: null, rejection: null,
      moodAuthority: parseMoodAuthority(ev, item), internalMarker: true };
    if (!jsonDomainWithin(ev)) return { event: null, rejection: null };
    const reason = validateEvent(ev);
    if (!reason) return { event: ev, rejection: null };
    const diagnostic = ev && typeof ev.type === "string" &&
      (ev.type.startsWith("routine_") || ev.type === "needs_human_resolved" ||
        ev.type === "journal_written" ||
        ["task_posted", "task_claimed", "task_done", "task_failed"].includes(ev.type));
    return { event: null, rejection: diagnostic ? { type: ev.type, reason } : null };
  } catch {
    // Direct object inputs can contain getters/proxies or values JSON cannot
    // represent. They fail closed just like malformed wire JSON.
    return { event: null, rejection: null };
  }
}
function parseEvents(batch) {
  if (batch && batch[VALIDATED_BATCH]) return batch;
  const events = [], rejections = [], capsules = [];
  for (let physicalIndex = 0; physicalIndex < batch.length; physicalIndex++) {
    const item = batch[physicalIndex];
    const parsed = parseItem(item);
    if (parsed.event) events.push(parsed.event);
    if (parsed.rejection) rejections.push(parsed.rejection);
    if (parsed.internalMarker) capsules.push({ authority: parsed.moodAuthority, physicalIndex });
  }
  let moodAuthority = [], moodOrdinals = [], moodCopies = [], moodOrder = [], moodRawIndexes = [],
    moodRawCount = rawIndex(0), moodOverflow = false,
    moodObserved = 0;
  // Exactly one capsule is legal. Its manifests are accepted only as an
  // atomic unit after proving they are structural multisets of both its own
  // authority and the surrounding raw batch. This prevents metadata from
  // suppressing an unrelated event with a merely similar serialization.
  if (capsules.length === 1 && capsules[0].physicalIndex === 0 && capsules[0].authority) {
    const capsule = capsules[0].authority;
    const byOrdinal = new Map(capsule.ordinals.map((ordinal, index) =>
      [ordinal, capsule.events[index]]));
    const rawByOrdinal = new Map(capsule.rawOrdinals.map((ordinal, index) =>
      [ordinal, events[Number(capsule.rawIndexes[index])]]));
    const copiesSafe = Number(capsule.rawCount) <= events.length &&
      capsule.copies.length === new Set(capsule.copies).size &&
      capsule.rawIndexes.every(index => Number(index) < events.length) && capsule.copies.every(ordinal =>
        byOrdinal.has(ordinal) && rawByOrdinal.has(ordinal) &&
        capsuleIdentityEqual(byOrdinal.get(ordinal), rawByOrdinal.get(ordinal)));
    const authorityOrdinals = new Set(capsule.ordinals);
    const intersections = capsule.rawOrdinals.filter(ordinal => authorityOrdinals.has(ordinal));
    const orderSafe = capsule.rawIndexes.every(index => Number(index) < events.length) &&
      intersections.length === capsule.copies.length &&
      intersections.every(ordinal => capsule.copies.includes(ordinal));
    if (copiesSafe && orderSafe) {
      moodAuthority = capsule.events; moodOrdinals = capsule.ordinals;
      moodCopies = capsule.copies; moodOrder = capsule.rawOrdinals;
      moodRawIndexes = capsule.rawIndexes;
      moodRawCount = capsule.rawCount;
      moodOverflow = capsule.overflow;
      moodObserved = capsule.observed;
    }
  }
  Object.defineProperty(events, VALIDATED_BATCH, { value: true });
  Object.defineProperty(events, REJECTIONS, { value: rejections });
  Object.defineProperty(events, MOOD_AUTHORITY, { value: moodAuthority });
  Object.defineProperty(events, MOOD_AUTHORITY_COPIES, {
    value: moodCopies });
  Object.defineProperty(events, MOOD_AUTHORITY_ORDINALS, { value: moodOrdinals });
  Object.defineProperty(events, MOOD_AUTHORITY_ORDER, { value: moodOrder });
  Object.defineProperty(events, MOOD_AUTHORITY_RAW_INDEXES, { value: moodRawIndexes });
  Object.defineProperty(events, MOOD_AUTHORITY_RAW_COUNT, { value: moodRawCount });
  Object.defineProperty(events, MOOD_AUTHORITY_OVERFLOW, { value: moodOverflow });
  Object.defineProperty(events, MOOD_AUTHORITY_OBSERVED, { value: moodObserved });
  return events;
}
/* Parse a grouped raw response once while retaining two independently marked
 * validated views: complete journal authority and the ordinary transport
 * window. The window is measured in raw records, matching the server cursor
 * contract (a malformed line still occupied its append position). */
function parseEventWindows(batch, limit) {
  if (!Array.isArray(batch) || !Number.isInteger(limit) || limit < 0) {
    throw new TypeError("parseEventWindows requires an array and a non-negative limit");
  }
  const tailStart = Math.max(0, batch.length - limit);
  const all = parseEvents(batch), tailSelection = [];
  let eventIndex = 0;
  for (let index = 0; index < batch.length; index++) {
    const parsed = parseItem(batch[index]);
    if (!parsed.event) continue;
    if (index >= tailStart) tailSelection.push(all[eventIndex]);
    eventIndex++;
  }
  const tail = validatedSelection(all, tailSelection);
  const internal = batch.reduce((count, item) => count +
    (parseItem(item).internalMarker ? 1 : 0), 0);
  const tailItems = batch.slice(tailStart);
  const tailInternal = tailItems.reduce((count, item) => count +
    (parseItem(item).internalMarker ? 1 : 0), 0);
  return { full: all, tail, rejected: batch.length - all.length - internal,
    tailRejected: tailItems.length - tail.length - tailInternal };
}
function routineRejections(batch) {
  const parsed = parseEvents(batch);
  return (parsed[REJECTIONS] || []).filter(item => item.type.startsWith("routine_"));
}
function taskRejections(batch) {
  const parsed = parseEvents(batch);
  return (parsed[REJECTIONS] || []).filter(item => item.type.startsWith("task_"));
}
function approvalRejections(batch) {
  const parsed = parseEvents(batch);
  return (parsed[REJECTIONS] || []).filter(item => item.type === "needs_human_resolved");
}
function journalRejections(batch) {
  const parsed = parseEvents(batch);
  return (parsed[REJECTIONS] || []).filter(item => item.type === "journal_written");
}
function isValidatedBatch(batch) {
  return Boolean(batch && batch[VALIDATED_BATCH]);
}
function validatedSelection(source, selection) {
  if (!isValidatedBatch(source) || !Array.isArray(selection)) {
    throw new TypeError("validatedSelection requires a validated source and an array");
  }
  const available = new Set(source);
  if (selection.some(event => !available.has(event))) {
    throw new TypeError("validatedSelection cannot introduce unvalidated records");
  }
  if (new Set(selection).size !== selection.length) {
    throw new TypeError("validatedSelection requires distinct source records");
  }
  Object.defineProperty(selection, VALIDATED_BATCH, { value: true });
  Object.defineProperty(selection, REJECTIONS, { value: [] });
  Object.defineProperty(selection, MOOD_AUTHORITY, {
    value: source[MOOD_AUTHORITY] || [], configurable: true });
  Object.defineProperty(selection, MOOD_AUTHORITY_COPIES, {
    value: source[MOOD_AUTHORITY_COPIES] || [], configurable: true });
  Object.defineProperty(selection, MOOD_AUTHORITY_ORDINALS, {
    value: source[MOOD_AUTHORITY_ORDINALS] || [], configurable: true });
  Object.defineProperty(selection, MOOD_AUTHORITY_ORDER, {
    value: source[MOOD_AUTHORITY_ORDER] || [], configurable: true });
  Object.defineProperty(selection, MOOD_AUTHORITY_RAW_INDEXES, {
    value: source[MOOD_AUTHORITY_RAW_INDEXES] || [], configurable: true });
  Object.defineProperty(selection, MOOD_AUTHORITY_RAW_COUNT, {
    value: source[MOOD_AUTHORITY_RAW_COUNT] || rawIndex(0), configurable: true });
  Object.defineProperty(selection, MOOD_AUTHORITY_OVERFLOW, {
    value: source[MOOD_AUTHORITY_OVERFLOW] || false, configurable: true });
  Object.defineProperty(selection, MOOD_AUTHORITY_OBSERVED, {
    value: source[MOOD_AUTHORITY_OBSERVED] || 0, configurable: true });
  return selection;
}

function moodAuthority(batch) {
  return parseEvents(batch)[MOOD_AUTHORITY] || [];
}

function moodAuthorityCopies(batch) {
  return parseEvents(batch)[MOOD_AUTHORITY_COPIES] || [];
}

function moodAuthorityState(batch) {
  const parsed = parseEvents(batch);
  return { events: parsed[MOOD_AUTHORITY] || [],
    ordinals: parsed[MOOD_AUTHORITY_ORDINALS] || [],
    copies: parsed[MOOD_AUTHORITY_COPIES] || [],
    rawOrdinals: parsed[MOOD_AUTHORITY_ORDER] || [],
    rawIndexes: parsed[MOOD_AUTHORITY_RAW_INDEXES] || [],
    rawCount: parsed[MOOD_AUTHORITY_RAW_COUNT] || rawIndex(0),
    overflow: Boolean(parsed[MOOD_AUTHORITY_OVERFLOW]),
    observed: parsed[MOOD_AUTHORITY_OBSERVED] || 0 };
}

function withMoodAuthority(source, selection, authority, copies = [], options = {}) {
  const validated = validatedSelection(source, selection);
  Object.defineProperty(validated, MOOD_AUTHORITY, {
    value: authority.slice(), configurable: true });
  Object.defineProperty(validated, MOOD_AUTHORITY_COPIES, {
    value: copies.slice(), configurable: true });
  Object.defineProperty(validated, MOOD_AUTHORITY_ORDINALS, {
    value: (options.ordinals || authority.map((_, index) => String(index))).slice(), configurable: true });
  Object.defineProperty(validated, MOOD_AUTHORITY_ORDER, {
    value: (options.rawOrdinals || []).slice(), configurable: true });
  Object.defineProperty(validated, MOOD_AUTHORITY_RAW_INDEXES, {
    value: (options.rawIndexes || (options.rawOrdinals || []).map((_, index) => rawIndex(index))).slice(),
    configurable: true });
  Object.defineProperty(validated, MOOD_AUTHORITY_RAW_COUNT, {
    value: options.rawCount || rawIndex(selection.length), configurable: true });
  Object.defineProperty(validated, MOOD_AUTHORITY_OVERFLOW, {
    value: options.overflow === true, configurable: true });
  Object.defineProperty(validated, MOOD_AUTHORITY_OBSERVED, {
    value: options.observed || 0, configurable: true });
  return validated;
}

function moodAuthorityCapsuleByteLength(authority, copies = [], options = {}) {
  const rawOrdinals = options.rawOrdinals || [];
  const capsule = { _burrow_internal: MOOD_AUTHORITY_KIND,
    events: authority, ordinals: options.ordinals || authority.map((_, index) => String(index)),
    copies, raw_ordinals: rawOrdinals,
    raw_indexes: options.rawIndexes || rawOrdinals.map((_, index) => rawIndex(index)),
    raw_count: options.rawCount || rawIndex(rawOrdinals.length),
    overflow: options.overflow === true,
    observed: options.observed || 0 };
  try {
    const logical = { ...capsule }; delete logical._burrow_internal;
    const graph = TYPED_JSON.graphString(TYPED_JSON.typedGraph(logical));
    return encodedBytes(`{"_burrow_internal":${canonicalJSONString(MOOD_AUTHORITY_KIND)},` +
      `"encoding":${canonicalJSONString(MOOD_AUTHORITY_ENCODING)},"graph":${graph}}`);
  } catch { return Infinity; }
}

function foldEvents(agents, batch, journalState = null) {
  for (const ev of parseEvents(batch)) {
    // Routines have their own run ledger. They neither create an ordinary
    // villager nor refresh/change one whose last interactive state is known.
    if (ev.type.startsWith("routine_") || ev.type === "needs_human_resolved" ||
        ["task_posted", "task_claimed", "task_done", "task_failed"].includes(ev.type)) continue;
    if (ev.type === "journal_written" && (!journalState ||
        typeof journalState.recordForEvent !== "function" ||
        !journalState.recordForEvent(ev))) continue;
    let a = agents.get(ev.agent_id);
    if (!a) {
      a = { id: ev.agent_id, events: [], lastAny: null, lastOrdinaryAny: null,
        parentAgentId: null };
      agents.set(ev.agent_id, a);
    }
    a.lastAny = ev;
    if (ev.type !== "journal_written") a.lastOrdinaryAny = ev;
    const payload = ev.payload || {};
    // A retained journal carries its own direct lineage for ownership checks;
    // it must not make that lineage permanent after the journal is evicted.
    if (ev.type !== "journal_written" && payload.parent_agent_id) {
      a.parentAgentId = String(payload.parent_agent_id);
    }
    if (ev.type === "heartbeat") continue;
    a.events.push(ev);
    if (a.events.length > MAX_EVENTS) a.events.shift();
  }
  if (journalState && typeof journalState.recordForEvent === "function") {
    const journalApi = JOURNALS || globalThis.BurrowJournals;
    const affected = new Set(journalApi && typeof journalApi.evictedAgentIds === "function" ?
      journalApi.evictedAgentIds(journalState) : []);
    for (const agentId of affected) {
      const agent = agents.get(agentId);
      if (!agent) continue;
      agent.events = agent.events.filter(event => event.type !== "journal_written" ||
        journalState.recordForEvent(event));
      if (agent.lastAny && agent.lastAny.type === "journal_written" &&
          !journalState.recordForEvent(agent.lastAny)) {
        const retained = journalApi && typeof journalApi.latestForAgent === "function" ?
          journalApi.latestForAgent(journalState, agent.id) : null;
        const ordinary = agent.lastOrdinaryAny || null;
        const ordinaryOrdinal = ordinary && typeof journalState.ordinalForEvent === "function" ?
          journalState.ordinalForEvent(ordinary) : null;
        agent.lastAny = retained && (!ordinary || (typeof ordinaryOrdinal === "string" &&
          journalApi.compareOrdinal(retained.ordinal, ordinaryOrdinal) > 0)) ?
          retained.event : ordinary;
      }
      const retained = journalApi && typeof journalApi.latestForAgent === "function" ?
        journalApi.latestForAgent(journalState, agent.id) : null;
      const ordinaryAuthority = agent.lastOrdinaryAny ||
        agent.events.some(event => event.type !== "journal_written") ||
        (agent.lastAny && agent.lastAny.type !== "journal_written");
      if (!retained && !ordinaryAuthority && !agent.parentAgentId) agents.delete(agentId);
    }
  }
}

/* Keep the notice board bounded while events arrive. Sorting each small batch
 * also makes the board truthful when two emitters flush out of order. */
function foldArtifacts(artifacts, batch) {
  for (const ev of parseEvents(batch)) {
    if (ev.type !== "artifact_produced") continue;
    const artifact = ev.payload && ev.payload.artifact;
    if (!artifact) continue;
    artifacts.push({
      artifact: String(artifact), agent_id: ev.agent_id,
      project: ev.project || "unknown", ts: Date.parse(ev.ts) || 0,
    });
  }
  artifacts.sort((a, b) => b.ts - a.ts);
  if (artifacts.length > MAX_ARTIFACTS) artifacts.length = MAX_ARTIFACTS;
  return artifacts;
}

function nameArtifacts(artifacts, villagers, souls) {
  const names = new Map((villagers || []).map(v => [v.id, v.name]));
  const soulByAgent = new Map(), soulByProject = new Map();
  for (const soul of souls || []) {
    if (!soul || !soul.meta || !soul.meta.name) continue;
    if (soul.meta.agent_id) soulByAgent.set(soul.meta.agent_id, soul.meta.name);
    if (soul.meta.project) soulByProject.set(soul.meta.project, soul.meta.name);
  }
  return artifacts.map(a => ({
    ...a,
    name: names.get(a.agent_id) || soulByAgent.get(a.agent_id) ||
      soulByProject.get(a.project) || NAMES[hashCode(a.agent_id) % NAMES.length],
  }));
}
function reduce(input, now, souls, approvalState = null, journalState = null) {
  const soulByAgent = new Map(), soulByProject = new Map();
  const isResident = soul => Boolean(soul && soul.valid === true &&
    soul.manifest_version === 1 && Number.isInteger(soul.home));
  const indexSoul = (index, key, soul) => {
    if (!index.has(key) || isResident(soul) || !isResident(index.get(key))) {
      index.set(key, soul);
    }
  };
  for (const s of souls || []) {
    if (!s || !s.meta) continue;
    const match = s.match || s.meta;
    if (match.agent_id) indexSoul(soulByAgent, match.agent_id, s);
    if (match.project) indexSoul(soulByProject, match.project, s);
  }
  const agents = input instanceof Map ? input : new Map();
  if (!(input instanceof Map)) foldEvents(agents, input, journalState);
  const journalApi = JOURNALS || globalThis.BurrowJournals;
  if (journalState && journalApi && typeof journalApi.resolveOwnership === "function") {
    journalApi.resolveOwnership(journalState, souls, agents);
  }
  const effectiveLast = agent => {
    const ordinary = agent.lastOrdinaryAny || agent.events.slice().reverse()
      .find(event => event.type !== "journal_written") || null;
    const retained = journalState && journalApi &&
      typeof journalApi.latestOwnedActiveForAgent === "function" ?
      journalApi.latestOwnedActiveForAgent(journalState, agent.id, now) : null;
    if (!retained) return ordinary;
    if (!ordinary) return retained.event;
    const ordinaryOrdinal = typeof journalState.ordinalForEvent === "function" ?
      journalState.ordinalForEvent(ordinary) : null;
    return typeof ordinaryOrdinal === "string" &&
      journalApi.compareOrdinal(retained.ordinal, ordinaryOrdinal) > 0 ?
      retained.event : ordinary;
  };
  const approvalRecordFor = event => {
    if (!approvalState || !(approvalState.requests instanceof Map) ||
        typeof approvalState.classify !== "function" || !event) return { shape: null, record: null };
    const shape = approvalState.classify(event);
    return { shape, record: shape.kind === "structured" &&
      typeof approvalState.recordForEvent === "function" ?
      approvalState.recordForEvent(event) : shape.kind === "structured" ?
      approvalState.requests.get(shape.request_id) || null : null };
  };
  const ordinalFor = event => approvalState &&
    typeof approvalState.ordinalForEvent === "function" ?
    approvalState.ordinalForEvent(event) : null;
  const ordinalApi = APPROVAL_ORDINALS || globalThis.BurrowApprovals;
  if (!ordinalApi || typeof ordinalApi.compareOrdinal !== "function" ||
      typeof ordinalApi.recentConfirmations !== "function") {
    throw new Error("approval append-ordinal authority is unavailable");
  }
  const compareOrdinal = ordinalApi.compareOrdinal;
  const closingLifecycleAfter = (agentId, ordinaryEvent) => {
    if (!approvalState || !(approvalState.requests instanceof Map)) return null;
    const boundary = ordinalFor(ordinaryEvent);
    if (typeof boundary !== "string") return null;
    let selected = null;
    for (const record of approvalState.requests.values()) {
      if (!record || !record.resolution || record.collided || !record.knock ||
          record.knock.agent_id !== agentId) continue;
      // Only a lifecycle already open at this ordinary append may close across
      // it. This prevents an agent-wide resolution from rewriting unrelated
      // evidence while preserving request -> activity -> exact-close order.
      if (compareOrdinal(record.knockOrdinal, boundary) <= 0 &&
          compareOrdinal(record.resolutionOrdinal, boundary) > 0 &&
          (!selected || compareOrdinal(record.resolutionOrdinal,
            selected.resolutionOrdinal) > 0)) {
        selected = record;
      }
    }
    return selected;
  };
  const out = [];
  const takenNames = new Set(), takenChars = new Set();
  const sorted = [...agents.values()].sort((x, y) => x.id < y.id ? -1 : 1);
  const visible = sorted.filter(a => {
    const last = effectiveLast(a);
    const tracked = approvalRecordFor(last);
    if (tracked.shape && tracked.shape.kind === "structured" && !tracked.record) return false;
    const hasPendingApproval = approvalState && approvalState.requests instanceof Map &&
      [...approvalState.requests.values()].some(record => record && !record.resolution && !record.collided &&
        record.knock && record.knock.agent_id === a.id);
    if (!hasPendingApproval && last && last.type === "session_ended") return false;
    const independentKnock = last && last.type === "needs_human" &&
      (!tracked.shape || tracked.shape.kind !== "structured");
    const closingLifecycle = independentKnock ? null : closingLifecycleAfter(a.id, last);
    const effective = closingLifecycle ? closingLifecycle.resolution :
      tracked.record && tracked.record.resolution || last;
    return effective && (hasPendingApproval ||
      (effective.type !== "session_ended" && now - (Date.parse(effective.ts) || 0) <= DROP_MS));
  });
  // Reserve exact identities in a separate pass. A project fallback must never
  // consume the declaration belonging to an exact agent that sorts later.
  const assignedSouls = new Map(), usedSouls = new Set();
  // Journal ownership is resolved once by BurrowJournals with full lineage and
  // declaration knowledge. Reuse that answer instead of independently falling
  // back by project and accidentally housing a child visitor.
  for (const a of visible) {
    const last = effectiveLast(a);
    const record = last && last.type === "journal_written" && journalState &&
      typeof journalState.recordForEvent === "function" ? journalState.recordForEvent(last) : null;
    const owner = record && journalApi && typeof journalApi.ownerFor === "function" ?
      journalApi.ownerFor(journalState, record) : null;
    if (owner && !usedSouls.has(owner)) {
      assignedSouls.set(a.id, owner); usedSouls.add(owner);
    }
  }
  for (const a of visible) {
    if (assignedSouls.has(a.id)) continue;
    const exact = soulByAgent.get(a.id);
    if (exact && !usedSouls.has(exact)) {
      assignedSouls.set(a.id, exact);
      usedSouls.add(exact);
    }
  }
  for (const a of visible) {
    if (assignedSouls.has(a.id)) continue;
    // Project declarations describe the root working session. A child without
    // its own exact declaration remains a Visitor even though it shares cwd.
    if (a.parentAgentId) continue;
    const last = effectiveLast(a);
    const fallback = soulByProject.get(last.project || "unknown");
    if (fallback && !usedSouls.has(fallback)) {
      assignedSouls.set(a.id, fallback);
      usedSouls.add(fallback);
    }
  }
  for (const a of sorted) {
    const ordinaryLast = effectiveLast(a);
    if (!ordinaryLast) continue;
    const trackedOrdinary = approvalRecordFor(ordinaryLast);
    if (trackedOrdinary.shape && trackedOrdinary.shape.kind === "structured" &&
        !trackedOrdinary.record) continue;
    const approvalRecords = approvalState && approvalState.requests instanceof Map ?
      [...approvalState.requests.values()].filter(record =>
        record && record.knock && record.knock.agent_id === a.id) : [];
    const pendingApprovals = approvalRecords.filter(record => !record.resolution && !record.collided)
      .sort((x, y) => compareOrdinal(y.knockOrdinal, x.knockOrdinal));
    const pendingApproval = pendingApprovals[0] || null;
    const recentApprovals = ordinalApi.recentConfirmations(approvalState, a.id);
    const ordinaryRecord = trackedOrdinary.record;
    const ordinaryResolution = ordinaryRecord && ordinaryRecord.resolution;
    if (ordinaryRecord && ordinaryRecord.collided && !ordinaryResolution &&
        ordinaryLast.type === "needs_human") continue;
    // A pending structured knock owns the doorstep even if the resident emits
    // later activity. Conversely, only its exact closing event can release it.
    // A plain/malformed knock is an independent lifecycle, so no structured
    // close for this agent can suppress it.
    const independentKnock = ordinaryLast.type === "needs_human" &&
      (!trackedOrdinary.shape || trackedOrdinary.shape.kind !== "structured");
    const resolvedAfterOrdinary = independentKnock ? null :
      closingLifecycleAfter(a.id, ordinaryLast);
    if (!pendingApproval && ordinaryLast.type === "session_ended") continue;
    const plainAfterPending = independentKnock && pendingApproval &&
      compareOrdinal(ordinalFor(ordinaryLast), pendingApproval.knockOrdinal) > 0;
    const lastRecord = !pendingApproval && resolvedAfterOrdinary ? resolvedAfterOrdinary :
      !pendingApproval && ordinaryResolution ? ordinaryRecord : null;
    const last = plainAfterPending ? ordinaryLast : pendingApproval ? pendingApproval.knock :
      lastRecord ? lastRecord.resolution : ordinaryLast;
    const lastTs = Date.parse(last.ts) || 0;
    if (!pendingApproval && last.type === "session_ended") continue;
    if (!pendingApproval && now - lastTs > DROP_MS) continue;
    let state =
      pendingApproval || last.type === "needs_human" ? "knocking" :
      last.type === "needs_human_resolved" ? "resting" :
      last.type === "idle"        ? "resting"  : "working";
    if (last.type === "tool_failed") state = "failed";
    if (state === "working" && now - lastTs > STALE_MS) state = "stale";
    const prev = a.events.slice().reverse().find(event => event.type !== "journal_written");
    const shown = last.type === "heartbeat" && prev && ACTION_TYPES.has(prev.type) ? prev : last;
    const h = hashCode(a.id);
    const project = last.project || "unknown";
    const soul = assignedSouls.get(a.id) || null;
    // probe past hash collisions so every villager in view looks distinct
    let n = 0;
    while (takenNames.has(NAMES[(h + n) % NAMES.length]) && n < NAMES.length) n++;
    let c = 0;
    while (takenChars.has(CHARS[(h + c) % CHARS.length]) && c < CHARS.length) c++;
    let name = NAMES[(h + n) % NAMES.length];
    let char = CHARS[(h + c) % CHARS.length];
    let accent = ACCENTS[(h + n) % ACCENTS.length];
    if (soul) {
      if (soul.meta.name) name = soul.meta.name;
      if (CHARS.includes(soul.meta.char)) char = soul.meta.char;
      if (soul.meta.accent) accent = soul.meta.accent;
    }
    takenNames.add(name);
    takenChars.add(char);
    const resident = isResident(soul);
    if (last.type === "journal_written" && !resident) continue;
    const journalRecord = last.type === "journal_written" && journalState &&
      typeof journalState.recordForEvent === "function" ? journalState.recordForEvent(last) : null;
    out.push({
      id: a.id, state, lastTs, events: a.events,
      name, char, accent, soul,
      residency: resident ? "resident" : "visitor",
      home: resident ? soul.home : null,
      base: resident ? "home" : "visitor-lodge",
      project,
      cwd: last.cwd || "",
      // A lost signal is not travel: a stale villager keeps its last place.
      place: state === "working" || state === "stale" ? workPlace(shown) : null,
      doing: state === "working" ? (shown.type === "journal_written" ? "writing the journal" : doingLabel(shown)) : "",
      lastLine: describe(shown),
      knock: state === "knocking"
        ? { message: (last.payload && last.payload.message) || "(no message)", ts: lastTs,
          structured: last === (pendingApproval && pendingApproval.knock) ? pendingApproval.shape : null,
          request_id: last === (pendingApproval && pendingApproval.knock) ? pendingApproval.id : null,
          queue: pendingApprovals }
        : null,
      approval: lastRecord ?
        { request_id: lastRecord.id, resolution: lastRecord.resolution } : null,
      approvals: recentApprovals,
      journal: journalRecord,
    });
  }
  return out;
}

// node (tests) picks the module up here; the browser never sees `module`.
if (typeof module === "object" && module.exports) {
  module.exports = {
    NAMES, ACCENTS, CHARS, STALE_MS, DROP_MS, MAX_EVENTS, MAX_ARTIFACTS,
    VERBS, EVENT_TYPES, ACTION_TYPES, PLACE_OF_VERB,
    hashCode, esc, ago, describe, doingLabel, workPlace,
    validateEvent, parseEvents, parseEventWindows, isValidatedBatch, validatedSelection,
    moodAuthority, moodAuthorityCopies, moodAuthorityState, withMoodAuthority,
    canonicalIdentity, canonicalJSONStringify, capsuleIdentityEqual,
    MOOD_AUTHORITY_KIND, MOOD_AUTHORITY_ENCODING, MOOD_AUTHORITY_MAX_BYTES,
    MOOD_AUTHORITY_MAX_DEPTH,
    moodAuthorityCapsuleByteLength,
    routineRejections, taskRejections,
    approvalRejections, journalRejections,
    foldEvents, foldArtifacts, nameArtifacts, reduce,
  };
}
