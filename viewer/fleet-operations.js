"use strict";

/* Bounded, DOM-free operational projection. The browser runtime hands this the
 * same validated objects used by the village, so there is one event truth. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowFleet = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  const MAX_RECENT_EVENTS = 200;
  const EVENT_STATE = {
    task_started: "working", tool_called: "working", artifact_produced: "working",
    heartbeat: "working", needs_human: "needs human", tool_failed: "failed",
    idle: "resting", session_ended: "ended",
  };
  function createFleetState() { return { recent: [], malformed: 0 }; }
  function foldFleet(state, events, rejected = 0) {
    state.malformed += Math.max(0, Number(rejected) || 0);
    for (const event of events) state.recent.push({
      event, ts: Date.parse(event.ts) || 0, agent_id: event.agent_id,
      project: event.project, source: event.source,
      state: EVENT_STATE[event.type] || "unknown",
    });
    state.recent.sort((a, b) => b.ts - a.ts);
    if (state.recent.length > MAX_RECENT_EVENTS) state.recent.length = MAX_RECENT_EVENTS;
    return state;
  }
  function text(value) { return String(value || "").trim().toLowerCase(); }
  function matches(entry, filters = {}) {
    const event = entry.event;
    const haystack = [event.agent_id, event.project, event.source, event.type,
      ...Object.values(event.payload || {})].join(" ").toLowerCase();
    return (!text(filters.query) || haystack.includes(text(filters.query))) &&
      (!text(filters.project) || text(entry.project) === text(filters.project)) &&
      (!text(filters.source) || text(entry.source) === text(filters.source)) &&
      (!text(filters.state) || text(entry.state) === text(filters.state)) &&
      (!text(filters.villager) || text(entry.agent_id) === text(filters.villager));
  }
  function filterRecent(state, filters) { return state.recent.filter(e => matches(e, filters)); }
  function outstandingNeeds(state) {
    const latest = new Map();
    for (const entry of state.recent) if (!latest.has(entry.agent_id)) latest.set(entry.agent_id, entry);
    return [...latest.values()].filter(entry => entry.event.type === "needs_human");
  }
  function optionsFor(state, key) {
    return [...new Set(state.recent.map(entry => entry[key]).filter(Boolean))]
      .sort((a, b) => String(a).localeCompare(String(b)));
  }
  /* status_ref is only an opaque locator for where status is established. Its
   * text never says whether that external status is healthy. These labels are
   * derived solely from public projection shape and feed availability. */
  function capabilityStatus(declaration, directoryAvailable = true, invalid = false) {
    if (!directoryAvailable) return "externally unavailable";
    if (invalid) return "invalid";
    if (!declaration) return "missing";
    const identifier = declaration.id ?? declaration.ref;
    if (!text(identifier) || !text(declaration.status_ref)) return "invalid";
    return "configured";
  }
  function residentDirectory(residents, diagnostics, directoryAvailable = true,
                             diagnosticResidents = []) {
    const valid = (residents || []).filter(item => item && item.valid === true)
      .sort((a, b) => a.home - b.home);
    const incomplete = (residents || []).filter(item => !item || item.valid !== true)
      .map(item => ({ file: item && item.file || "<resident>", path: "$",
        message: "public resident record is incomplete or invalid", status: "invalid" }));
    const invalid = incomplete.concat((diagnostics || [])
      .map(item => ({ ...item, status: "invalid" })));
    const partial = (diagnosticResidents || []).filter(item => item && item.diagnostic === true);
    return { residents: valid, diagnosticResidents: partial, invalid, available: directoryAvailable,
      status: directoryAvailable ? (invalid.length ? "invalid" : valid.length ? "configured" : "missing")
        : "externally unavailable" };
  }
  function moveFocus(current, key, count) {
    if (!count) return -1;
    if (key === "Home") return 0;
    if (key === "End") return count - 1;
    if (key === "ArrowDown" || key === "ArrowRight") return (current + 1 + count) % count;
    if (key === "ArrowUp" || key === "ArrowLeft") return (current - 1 + count) % count;
    return current;
  }
  return { MAX_RECENT_EVENTS, EVENT_STATE, createFleetState, foldFleet,
    filterRecent, outstandingNeeds, optionsFor, capabilityStatus,
    residentDirectory, moveFocus };
});
