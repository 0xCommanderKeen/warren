"use strict";

/* Shared routine lifecycle ordering. Producer time selects lifecycle truth;
 * ingestion order is deliberately absent and is reserved for retention only. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowRoutineLifecycle = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  const TYPES = new Set(["routine_started", "routine_finished", "routine_failed"]);
  function eventTime(event) { return Date.parse(event && event.ts); }
  /* Protocol identity is a JSON array, never delimiter-concatenated. Ordering
   * is fieldwise Unicode-scalar order and therefore agrees with Python. */
  function compareText(left, right) {
    const a = String(left), b = String(right);
    let ai = 0, bi = 0;
    while (ai < a.length && bi < b.length) {
      const ac = a.codePointAt(ai), bc = b.codePointAt(bi);
      if (ac !== bc) return ac < bc ? -1 : 1;
      ai += ac > 0xffff ? 2 : 1;
      bi += bc > 0xffff ? 2 : 1;
    }
    return ai === a.length ? (bi === b.length ? 0 : -1) : 1;
  }
  function compareFields(left, right) {
    const length = Math.max(left.length, right.length);
    for (let index = 0; index < length; index++) {
      if (index >= left.length) return -1;
      if (index >= right.length) return 1;
      const comparison = compareText(left[index], right[index]);
      if (comparison) return comparison;
    }
    return 0;
  }
  function identity(fields) { return JSON.stringify(fields); }
  function runFields(event) {
    const payload = event && event.payload || {};
    return [payload.routine, payload.run_id];
  }
  function runKey(event) { return identity(runFields(event)); }
  function agentRunKey(event) { return identity([event.agent_id, ...runFields(event)]); }
  function agentRoutineKey(agentId, routine) { return identity([agentId, routine]); }
  function startTie(event) {
    const payload = event.payload;
    return [event.source, event.agent_id, event.project, payload.routine,
      payload.run_id, payload.trigger];
  }
  function compareNumbers(left, right) {
    if (left === right) return 0;
    return left < right ? -1 : 1;
  }
  function compareTerminalTie(left, right) {
    const leftFailed = left.type === "routine_failed", rightFailed = right.type === "routine_failed";
    if (leftFailed !== rightFailed) return leftFailed ? 1 : -1;
    const a = left.payload, b = right.payload;
    if (leftFailed) {
      const error = compareText(a.error, b.error);
      if (error) return error;
      const aHas = a.duration_s !== undefined, bHas = b.duration_s !== undefined;
      if (aHas !== bHas) return aHas ? 1 : -1;
      return aHas ? compareNumbers(a.duration_s, b.duration_s) : 0;
    }
    const outcome = compareText(a.outcome, b.outcome);
    if (outcome) return outcome;
    const duration = compareNumbers(a.duration_s, b.duration_s);
    return duration || compareFields(a.artifacts, b.artifacts);
  }
  function preferStart(candidate, current) {
    if (!current) return true;
    const candidateAt = eventTime(candidate), currentAt = eventTime(current);
    return candidateAt < currentAt ||
      (candidateAt === currentAt && compareFields(startTie(candidate), startTie(current)) < 0);
  }
  function preferTerminal(candidate, current) {
    if (!current) return true;
    const candidateAt = eventTime(candidate), currentAt = eventTime(current);
    return candidateAt > currentAt ||
      (candidateAt === currentAt && compareTerminalTie(candidate, current) > 0);
  }
  function selectStart(events, predicate = () => true) {
    let selected = null;
    for (const event of events || []) {
      if (event.type === "routine_started" && predicate(event) &&
          preferStart(event, selected)) selected = event;
    }
    return selected;
  }
  function selectTerminal(events, predicate = () => true) {
    let selected = null;
    for (const event of events || []) {
      if ((event.type === "routine_finished" || event.type === "routine_failed") &&
          predicate(event) && preferTerminal(event, selected)) selected = event;
    }
    return selected;
  }
  function resolvePair(start, terminal) {
    return start ? { start, terminal: terminal && eventTime(terminal) >= eventTime(start) ? terminal : null } : null;
  }
  function resolveRun(events) { return resolvePair(selectStart(events), selectTerminal(events)); }
  function createAuthority() { return { runs: new Map(), nextOrdinal: 0 }; }
  function compareRunRetention(left, right) {
    if (Boolean(left.start) !== Boolean(right.start)) return left.start ? 1 : -1;
    const leftEvent = left.start || left.terminal, rightEvent = right.start || right.terminal;
    const time = eventTime(leftEvent) - eventTime(rightEvent);
    if (time) return time < 0 ? -1 : 1;
    return compareFields(left.fields, right.fields) || Math.sign(left.firstOrdinal - right.firstOrdinal);
  }
  function foldAuthority(authority, event, maximumRuns = 80) {
    const state = authority && authority.runs instanceof Map ? authority : createAuthority();
    if (!event || !TYPES.has(event.type)) return state;
    const key = runKey(event);
    let run = state.runs.get(key);
    if (!run) {
      run = { key, fields: runFields(event), start: null, terminal: null,
        firstOrdinal: state.nextOrdinal++ };
      state.runs.set(key, run);
    }
    if (event.type === "routine_started") {
      if (preferStart(event, run.start)) run.start = event;
    } else if (preferTerminal(event, run.terminal)) run.terminal = event;
    while (state.runs.size > maximumRuns) {
      let victim = null;
      for (const candidate of state.runs.values()) {
        if (!victim || compareRunRetention(candidate, victim) < 0) victim = candidate;
      }
      state.runs.delete(victim.key);
    }
    return state;
  }
  function authorityFrom(events, maximumRuns = 80) {
    const authority = createAuthority();
    for (const event of events || []) foldAuthority(authority, event, maximumRuns);
    return authority;
  }
  function selectCurrentAuthority(authority) {
    let selected = null;
    for (const run of authority && authority.runs instanceof Map ? authority.runs.values() : []) {
      const resolved = resolvePair(run.start, run.terminal);
      if (!resolved) continue;
      const candidate = { ...resolved, key: run.key, fields: run.fields,
        event: resolved.terminal || resolved.start };
      if (!selected || eventTime(candidate.start) > eventTime(selected.start) ||
          (eventTime(candidate.start) === eventTime(selected.start) &&
           compareFields(candidate.fields, selected.fields) > 0)) {
        selected = candidate;
      }
    }
    return selected;
  }
  function selectCurrent(events) { return selectCurrentAuthority(authorityFrom(events)); }
  return { eventTime, compareText, compareFields, compareTerminalTie, identity, runKey, agentRunKey,
    agentRoutineKey, preferStart, preferTerminal, selectStart, selectTerminal,
    resolveRun, createAuthority, foldAuthority, authorityFrom, selectCurrentAuthority,
    selectCurrent };
});
