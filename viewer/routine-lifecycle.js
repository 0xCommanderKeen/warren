"use strict";

/* Shared routine lifecycle ordering. Producer time selects lifecycle truth;
 * ingestion order is deliberately absent and is reserved for retention only. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowRoutineLifecycle = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  function eventTime(event) { return Date.parse(event && event.ts); }
  function startTie(event) {
    const payload = event.payload;
    return [event.source, event.agent_id, event.project, payload.routine,
      payload.run_id, payload.trigger].join("\0");
  }
  function terminalTie(event) {
    const payload = event.payload;
    // Equal producer timestamps prefer the conservative failed claim, then
    // validated terminal fields in stable schema order.
    return event.type === "routine_failed" ?
      `1\0${payload.error}\0${payload.duration_s ?? ""}` :
      `0\0${payload.outcome}\0${payload.duration_s}\0${payload.artifacts.join("\0")}`;
  }
  function preferStart(candidate, current) {
    if (!current) return true;
    const candidateAt = eventTime(candidate), currentAt = eventTime(current);
    return candidateAt < currentAt ||
      (candidateAt === currentAt && startTie(candidate) < startTie(current));
  }
  function preferTerminal(candidate, current) {
    if (!current) return true;
    const candidateAt = eventTime(candidate), currentAt = eventTime(current);
    return candidateAt > currentAt ||
      (candidateAt === currentAt && terminalTie(candidate) > terminalTie(current));
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
  return { eventTime, preferStart, preferTerminal, selectStart, selectTerminal };
});
