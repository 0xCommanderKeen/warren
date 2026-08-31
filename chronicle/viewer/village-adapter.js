"use strict";
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowVillageAdapter = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  // Compatibility adapter for the existing village UI. New clients consume the
  // authoritative snapshot directly and must not copy these presentation shapes.
  function adapt(snapshot) {
    const souls = snapshot.residents.filter(item => item && item.valid).map(item =>
      ({ ...item, meta: { ...(item.meta || item.soul || {}) }, match: item.match || {} }));
    const soulByFile = new Map(souls.map(soul => [soul.file, soul]));
    const requests = new Map(snapshot.approvals.map(item => [item.request_id, {
      id: item.request_id, shape: { kind: "structured", request_id: item.request_id,
        action: item.action, detail: item.detail, options: item.options || [] },
      knock: { agent_id: item.agent_id, ts: item.opened_at, payload: { message: item.message,
        request_id: item.request_id, action: item.action, detail: item.detail, options: item.options || [] } },
      resolution: item.state === "resolved" ? { ts: item.resolved_at,
        payload: { request_id: item.request_id, decision: item.decision } } : null,
      collided: item.state === "collision",
    }]));
    const approvalState = { requests, diagnostics: snapshot.diagnostics.filter(item =>
      String(item.kind).startsWith("approval")), capacityDropped: 0 };
    const villagers = snapshot.villagers.map(item => {
      const soul = item.resident_file ? soulByFile.get(item.resident_file) || null : null;
      const pending = (item.pending_approval_ids || []).map(id => requests.get(id)).filter(Boolean);
      return { ...item, lastTs: Date.parse(item.last_ts), lastLine: item.last_line,
        events: item.history || [], soul, base: item.base === "lodge" ? "visitor-lodge" : item.base,
        doing: item.state === "working" ? item.last_line : "", place: item.place || null,
        knock: pending.length ? { message: pending[0].knock.payload.message,
          ts: Date.parse(pending[0].knock.ts), structured: pending[0].shape,
          request_id: pending[0].id, queue: pending } : null,
        approvals: [...requests.values()].filter(record => record.knock.agent_id === item.id && record.resolution) };
    });
    const recent = villagers.flatMap(v => v.events.map((event, sequence) =>
      ({ event, agent_id: v.id, sequence, ts: Date.parse(event.ts) }))).sort((a, b) => b.ts - a.ts).slice(0, 200);
    const residentByFile = new Map(souls.map(soul => [soul.file, soul]));
    const villagerByAgent = new Map(snapshot.villagers.map(item => [item.id, item]));
    const journalRecords = new Map(snapshot.journals.map((item, ordinal) => {
      const event = { v: 0, ts: item.observed_at, source: item.source || "steward",
        agent_id: item.agent_id, project: item.project, type: "journal_written",
        payload: { day: item.day, routine: item.routine, path: item.path } };
      const key = `${item.agent_id}\0${item.day}`;
      return [key, { key, event, conflict: null, ordinal: String(ordinal + 1) }];
    }));
    const journalOwners = new Map();
    for (const record of journalRecords.values()) {
      const villager = villagerByAgent.get(record.event.agent_id);
      const resident = villager && residentByFile.get(villager.resident_file);
      if (resident) journalOwners.set(record.key, { resident, kind: "authoritative-snapshot" });
    }
    const journalState = { records: journalRecords, owners: journalOwners, diagnostics: [],
      ownershipDiagnostics: [], collisionDiagnostics: [], malformedDiagnostics: [], malformed: 0,
      capacityDropped: 0 };
    const routineRecent = snapshot.routines.flatMap(item => {
      const base = { v: 0, source: item.source || "steward", agent_id: item.agent_id,
        project: item.project };
      const start = { ...base, ts: item.started_at, type: "routine_started",
        payload: { routine: item.routine, run_id: item.run_id, trigger: item.trigger } };
      const entries = [{ event: start, agent_id: item.agent_id }];
      if (item.state !== "running") entries.push({ event: { ...base, ts: item.updated_at,
        type: item.state === "failed" ? "routine_failed" : "routine_finished",
        payload: { routine: item.routine, run_id: item.run_id, outcome: item.outcome,
          duration_s: item.duration_s, artifacts: item.artifacts || [], error: item.error } },
        agent_id: item.agent_id });
      return entries;
    });
    return { ...snapshot, villagers, artifacts: snapshot.artifacts, souls,
      residents: snapshot.residents, diagnosticResidents: snapshot.diagnostic_residents || [],
      directoryAvailable: true, state: { recent, routineRecent, routineOrphans: [],
        routineMalformed: 0, routineCapacityDropped: 0 },
      jobState: { authoritativeRows: snapshot.tasks, tasks: new Map(), diagnostics: [], malformed: 0,
        capacityDropped: 0 }, approvalState, journalState, routineBatch: [], taskEvidence: [],
      approvalEvidence: [], eventEvidence: [], now: Date.parse(snapshot.evaluated_at) };
  }
  return { adapt };
});
