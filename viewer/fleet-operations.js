"use strict";

/* Bounded, DOM-free operational projection. The browser runtime hands this the
 * same validated objects used by the village, so there is one event truth. */
(function (root, factory) {
  const lifecycle = typeof module === "object" && module.exports ?
    require("./routine-lifecycle.js") : root.BurrowRoutineLifecycle;
  const api = factory(lifecycle);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowFleet = api;
})(typeof globalThis === "object" ? globalThis : this, function (lifecycle) {
  const MAX_RECENT_EVENTS = 200;
  const MAX_ROUTINE_RUNS = 20;
  const MAX_ROUTINE_KEYS = 200;
  const MAX_OPEN_RUNS = 20;
  const MAX_ROUTINE_ORPHANS = 200;
  const DEFAULT_STALE_MS = 30 * 60 * 1000;
  const EVENT_STATE = {
    task_started: "working", tool_called: "working", artifact_produced: "working",
    heartbeat: "working", needs_human: "needs human", tool_failed: "failed",
    idle: "resting", session_ended: "ended",
  };
  function createFleetState() {
    return { recent: [], routineRecent: [], routineOrphans: [], routineKeys: [], malformed: 0,
      routineMalformed: 0, routineCapacityDropped: 0, routineSequence: 0 };
  }
  function routineKey(event) {
    return `${event.source}\0${event.agent_id}\0${event.payload.routine}`;
  }
  function runKey(event) { return `${routineKey(event)}\0${event.payload.run_id}`; }
  function nextRoutineSequence(state) {
    if (!Number.isSafeInteger(state.routineSequence) ||
        state.routineSequence >= Number.MAX_SAFE_INTEGER) {
      const retained = state.routineRecent.concat(state.routineOrphans || []);
      retained.sort((a, b) =>
        (a.lifecycleSequence || a.sequence || 0) -
        (b.lifecycleSequence || b.sequence || 0));
      retained.forEach((entry, index) => {
        entry.sequence = index + 1; entry.lifecycleSequence = index + 1;
      });
      state.routineSequence = retained.length;
    }
    return ++state.routineSequence;
  }
  function lifecycleGroups(entries) {
    const groups = new Map();
    for (const entry of entries) {
      const key = runKey(entry.event), group = groups.get(key) ||
        { start: null, close: null, newestSequence: 0 };
      group.newestSequence = Math.max(group.newestSequence,
        entry.lifecycleSequence || entry.sequence);
      if (entry.event.type === "routine_started") {
        if (!group.start || lifecycle.preferStart(entry.event, group.start.event)) group.start = entry;
      } else if (!group.close || lifecycle.preferTerminal(entry.event, group.close.event)) {
        group.close = entry;
      }
      groups.set(key, group);
    }
    return groups;
  }
  function compactRoutineEvents(entries, now = null, staleMs = DEFAULT_STALE_MS) {
    const groups = lifecycleGroups(entries);
    const byRoutine = new Map(), orphan = [];
    for (const group of groups.values()) {
      if (!group.start) { orphan.push(group); continue; }
      const sample = group.start || group.close, key = routineKey(sample.event);
      const list = byRoutine.get(key) || [];
      list.push(group); byRoutine.set(key, list);
    }
    const candidates = [];
    for (const groupsForRoutine of byRoutine.values()) {
      const complete = [], open = [], stale = [];
      for (const group of groupsForRoutine) {
        if (group.start && group.close && group.close.ts >= group.start.ts) complete.push(group);
        else if (group.start && now !== null && now - group.start.ts > staleMs) stale.push(group);
        else open.push(group);
      }
      const newest = group => group.newestSequence;
      complete.sort((a, b) => newest(b) - newest(a));
      open.sort((a, b) => newest(b) - newest(a));
      stale.sort((a, b) => newest(b) - newest(a));
      // Close-only evidence is staged outside this renderable/key-bounded set.
      candidates.push({ groups: open.slice(0, MAX_OPEN_RUNS)
        .concat(complete.slice(0, MAX_ROUTINE_RUNS), stale.slice(0, MAX_OPEN_RUNS)),
      key: routineKey((groupsForRoutine[0].start || groupsForRoutine[0].close).event),
      rank: open.length ? 3 : complete.length ? 2 : stale.length ? 1 : 0,
      newest: Math.max(...groupsForRoutine.map(newest)),
      dropped: Math.max(0, open.length - MAX_OPEN_RUNS) +
        Math.max(0, complete.length - MAX_ROUTINE_RUNS) +
        Math.max(0, stale.length - MAX_OPEN_RUNS) });
    }
    // Current lifecycles and completed truth outrank start-only evidence that
    // has crossed the same 30-minute stale window used by the ledger. Some
    // stale groups remain as bounded staging so a late close can still pair.
    candidates.sort((a, b) => b.rank - a.rank || b.newest - a.newest ||
      a.key.localeCompare(b.key));
    const selected = candidates.slice(0, MAX_ROUTINE_KEYS), kept = [];
    let dropped = candidates.slice(MAX_ROUTINE_KEYS)
      .reduce((count, item) => count + item.groups.length, 0);
    for (const item of selected) {
      dropped += item.dropped;
      for (const group of item.groups) {
        // Preserve the latest ingestion evidence even when the selected
        // terminal is an earlier-delivered event with the newer timestamp.
        if (group.start) {
          group.start.lifecycleSequence = group.newestSequence;
          kept.push(group.start);
        }
        if (group.close) {
          group.close.lifecycleSequence = group.newestSequence;
          kept.push(group.close);
        }
      }
    }
    kept.sort((a, b) => b.ts - a.ts);
    // Terminals without starts are non-renderable correlation evidence. Keep
    // them in a separate globally bounded recency stage so they neither consume
    // nor displace any of the 200 renderable routine keys. lifecycleGroups has
    // already deduplicated conflicts with the shared terminal comparator.
    orphan.sort((a, b) => b.newestSequence - a.newestSequence ||
      runKey((a.close || a.start).event).localeCompare(runKey((b.close || b.start).event)));
    dropped += Math.max(0, orphan.length - MAX_ROUTINE_ORPHANS);
    const orphans = orphan.slice(0, MAX_ROUTINE_ORPHANS).map(group => group.close);
    return { entries: kept, orphans, dropped };
  }
  function foldFleet(state, events, rejected = 0, routineRejected = 0, now = null) {
    state.malformed += Math.max(0, Number(rejected) || 0);
    state.routineMalformed += Math.max(0, Number(routineRejected) || 0);
    let receivedRoutine = false;
    for (const event of events) {
      const routine = event.type.startsWith("routine_");
      const target = routine ? state.routineRecent : state.recent;
      target.push({ event, ts: Date.parse(event.ts) || 0, agent_id: event.agent_id,
        project: event.project, source: event.source,
        state: EVENT_STATE[event.type] || "unknown",
        sequence: routine ? nextRoutineSequence(state) : 0 });
      if (routine) {
        receivedRoutine = true;
      }
    }
    if (receivedRoutine) {
      // A poll batch is one ingestion unit. Compact only after all of its
      // evidence is admitted so a close-before-start pair is evaluated as a
      // complete lifecycle even when the retained ledger is already full.
      const compacted = compactRoutineEvents(
        state.routineRecent.concat(state.routineOrphans || []), now);
      state.routineRecent = compacted.entries;
      state.routineOrphans = compacted.orphans;
      state.routineCapacityDropped += compacted.dropped;
      state.routineKeys = [...new Set(state.routineRecent.map(entry => routineKey(entry.event)))];
    }
    for (const target of [state.recent]) {
      target.sort((a, b) => b.ts - a.ts);
      if (target.length > MAX_RECENT_EVENTS) target.length = MAX_RECENT_EVENTS;
    }
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
  return { MAX_RECENT_EVENTS, MAX_ROUTINE_RUNS, MAX_ROUTINE_KEYS, MAX_OPEN_RUNS,
    MAX_ROUTINE_ORPHANS,
    DEFAULT_STALE_MS,
    EVENT_STATE, createFleetState, foldFleet,
    filterRecent, outstandingNeeds, optionsFor, capabilityStatus,
    residentDirectory, moveFocus };
});
