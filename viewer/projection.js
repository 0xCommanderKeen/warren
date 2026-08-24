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
const CHARS = ["Villager","Villager2","Villager3","Villager4","Villager5",
               "Woman","Boy","OldMan","Princess","Hunter","Noble","Monk"];
const STALE_MS = 30 * 60 * 1000;
const DROP_MS  = 12 * 60 * 60 * 1000;
const MAX_EVENTS = 80;
const MAX_ARTIFACTS = 30;
const VERBS = {
  Read: "reading", Grep: "searching", Glob: "searching", WebSearch: "researching",
  WebFetch: "researching", Bash: "tinkering", Write: "crafting", Edit: "crafting",
  NotebookEdit: "crafting", Agent: "delegating", Task: "delegating",
  AskUserQuestion: "asking you", Skill: "consulting a manual", Workflow: "orchestrating",
};

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
    case "artifact_produced": return "crafted " + (p.artifact || "something");
    case "heartbeat":         return "finished " + (p.tool || "a tool");
    case "needs_human":       return "needs you: " + (p.message || "(no message)");
    case "idle":              return "finished, resting";
    case "session_ended":     return "went home";
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
const EVENT_TYPES = new Set(["task_started","tool_called","artifact_produced",
                             "heartbeat","needs_human","idle","session_ended"]);
const ACTION_TYPES = new Set(["task_started","tool_called","artifact_produced"]);
/* One parse per batch. The village and the notice board both read what this
 * returns, so "a well-formed event" has exactly one definition and cannot drift
 * between the two reducers; each reducer keeps only the checks its own payload
 * needs. Already-parsed events pass straight through, so a caller can hand
 * either JSONL lines or records to a fold without a second JSON.parse. */
function parseEvents(batch) {
  const events = [];
  for (const item of batch) {
    let ev = item;
    if (typeof ev === "string") {
      try { ev = JSON.parse(ev); } catch { continue; }
    }
    if (!ev || typeof ev !== "object" || !ev.agent_id || !ev.type) continue;
    events.push(ev);
  }
  return events;
}

function foldEvents(agents, batch) {
  for (const ev of parseEvents(batch)) {
    if (!EVENT_TYPES.has(ev.type)) continue;
    let a = agents.get(ev.agent_id);
    if (!a) { a = { id: ev.agent_id, events: [], lastAny: null }; agents.set(ev.agent_id, a); }
    a.lastAny = ev;
    if (ev.type === "heartbeat") continue;
    a.events.push(ev);
    if (a.events.length > MAX_EVENTS) a.events.shift();
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
function reduce(input, now, souls) {
  const soulByAgent = new Map(), soulByProject = new Map();
  for (const s of souls || []) {
    if (!s || !s.meta) continue;
    if (s.meta.agent_id) soulByAgent.set(s.meta.agent_id, s);
    if (s.meta.project) soulByProject.set(s.meta.project, s);
  }
  const usedSouls = new Set();
  const agents = input instanceof Map ? input : new Map();
  if (!(input instanceof Map)) foldEvents(agents, input);
  const out = [];
  const takenNames = new Set(), takenChars = new Set();
  const sorted = [...agents.values()].sort((x, y) => x.id < y.id ? -1 : 1);
  for (const a of sorted) {
    const last = a.lastAny || a.events[a.events.length - 1];
    const lastTs = Date.parse(last.ts) || 0;
    if (last.type === "session_ended") continue;
    if (now - lastTs > DROP_MS) continue;
    let state =
      last.type === "needs_human" ? "knocking" :
      last.type === "idle"        ? "resting"  : "working";
    if (state === "working" && now - lastTs > STALE_MS) state = "stale";
    const prev = a.events[a.events.length - 1];
    const shown = last.type === "heartbeat" && prev && ACTION_TYPES.has(prev.type) ? prev : last;
    const h = hashCode(a.id);
    const project = last.project || "unknown";
    // soul files pin identity: exact agent_id beats project match
    let soul = soulByAgent.get(a.id);
    if (!soul || usedSouls.has(soul)) {
      const p = soulByProject.get(project);
      soul = p && !usedSouls.has(p) ? p : soul && !usedSouls.has(soul) ? soul : null;
    }
    if (soul) usedSouls.add(soul);
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
    out.push({
      id: a.id, state, lastTs, events: a.events,
      name, char, accent, soul,
      project,
      cwd: last.cwd || "",
      doing: state === "working" ? doingLabel(shown) : "",
      lastLine: describe(shown),
      knock: state === "knocking"
        ? { message: (last.payload && last.payload.message) || "(no message)", ts: lastTs }
        : null,
    });
  }
  return out;
}

// node (tests) picks the module up here; the browser never sees `module`.
if (typeof module === "object" && module.exports) {
  module.exports = {
    NAMES, ACCENTS, CHARS, STALE_MS, DROP_MS, MAX_EVENTS, MAX_ARTIFACTS,
    VERBS, EVENT_TYPES, ACTION_TYPES, hashCode, esc, ago, describe, doingLabel,
    parseEvents, foldEvents, foldArtifacts, nameArtifacts, reduce,
  };
}
