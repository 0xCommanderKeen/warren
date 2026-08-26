"use strict";

/* ``journal_written`` is durable observation, not session liveness and not
 * journal content.  This module is the sole browser fold for identity,
 * append-order ownership, diagnostics, retention, reset, and recency. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowJournals = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  const MAX_DAYS = 40;
  const MAX_MALFORMED_DIAGNOSTICS = 40;
  // Kept as a compatibility alias for callers that used the old shared bound.
  const MAX_DIAGNOSTICS = MAX_MALFORMED_DIAGNOSTICS;
  const ACTIVE_MS = 60 * 1000;
  const ROUTINE = /^[a-z0-9][a-z0-9-]{0,127}$/;
  const DAY = /^\d{4}-\d{2}-\d{2}$/;
  const CONTROL = /[\x00-\x1f\x7f]/;
  const EDGE_WHITESPACE = new Set([
    0x0009,0x000a,0x000b,0x000c,0x000d,0x0020,0x0085,0x00a0,0x1680,
    0x2000,0x2001,0x2002,0x2003,0x2004,0x2005,0x2006,0x2007,0x2008,
    0x2009,0x200a,0x2028,0x2029,0x202f,0x205f,0x3000,
  ]);

  function codePointLength(value) { return [...value].length; }
  function hasEdgeWhitespace(value) {
    const points = [...value];
    return points.length > 0 && (EDGE_WHITESPACE.has(points[0].codePointAt(0)) ||
      EDGE_WHITESPACE.has(points.at(-1).codePointAt(0)));
  }
  function hasUnpairedSurrogate(value) {
    return [...value].some(character => {
      const point = character.codePointAt(0);
      return point >= 0xd800 && point <= 0xdfff;
    });
  }

  function validate(event) {
    if (!event || event.type !== "journal_written") return "not a journal observation";
    if (event.source !== "steward") return "journal observations require source steward";
    const payload = event.payload;
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return "payload must be an object";
    if (typeof payload.routine !== "string" || !ROUTINE.test(payload.routine)) return "invalid payload.routine";
    if (typeof payload.day !== "string" || !DAY.test(payload.day)) return "invalid payload.day";
    const parsed = new Date(`${payload.day}T00:00:00.000Z`);
    if (!Number.isFinite(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== payload.day ||
        payload.day.slice(0, 4) === "0000") return "invalid payload.day";
    if (typeof payload.path !== "string" || codePointLength(payload.path) < 1 ||
        codePointLength(payload.path) > 2048 || hasEdgeWhitespace(payload.path) ||
        hasUnpairedSurrogate(payload.path) ||
        CONTROL.test(payload.path)) return "invalid payload.path";
    if (payload.path.split(/[\\/]/).at(-1) !== `${payload.day}.md`) {
      return "payload.path must end with payload.day.md";
    }
    return null;
  }
  const keyFor = event => `${event.agent_id}\0${event.payload.day}`;
  const immutableIdentity = event => JSON.stringify([
    event.project, event.payload.routine, event.payload.path,
  ]);
  function compareScalars(left, right) {
    const a = [...left], b = [...right];
    for (let index = 0; index < Math.min(a.length, b.length); index++) {
      const difference = a[index].codePointAt(0) - b[index].codePointAt(0);
      if (difference) return difference < 0 ? -1 : 1;
    }
    return a.length === b.length ? 0 : a.length < b.length ? -1 : 1;
  }
  function compareKeys(left, right) {
    const day = compareScalars(left.event.payload.day, right.event.payload.day);
    return day || compareScalars(left.event.agent_id, right.event.agent_id);
  }
  function compareOrdinal(left, right) {
    const a = BigInt(left), b = BigInt(right);
    return a === b ? 0 : a < b ? -1 : 1;
  }
  function createState() {
    const state = { records: new Map(), malformedDiagnostics: [], malformed: 0,
      capacityDropped: 0, frontier: null, sequence: "0", ordinals: new WeakMap(),
      owners: new Map(), ownershipDiagnostics: [], evictedAgentIds: [] };
    // Collision evidence belongs to the retained canonical record. Deriving it
    // here makes one diagnostic per conflicted key authoritative by construction:
    // malformed pressure cannot displace it, while capacity eviction cannot leave
    // a stale collision behind.
    Object.defineProperty(state, "collisionDiagnostics", {
      enumerable: true,
      get: () => collisionDiagnostics(state),
    });
    Object.defineProperty(state, "diagnostics", {
      enumerable: true,
      get: () => state.collisionDiagnostics.concat(state.malformedDiagnostics),
    });
    Object.defineProperty(state, "appendSequence", { value: 0n, writable: true });
    state.ordinalForEvent = event => state.ordinals.get(event) || null;
    state.recordForEvent = event => {
      if (!event || event.type !== "journal_written") return null;
      const record = state.records.get(keyFor(event));
      return record && record.event === event ? record : null;
    };
    return state;
  }
  function diagnoseMalformed(state, reason, event, key = null) {
    const diagnosticKey = key || `${reason}\0${event && event.agent_id || ""}\0${event && event.ts || ""}`;
    if (state.malformedDiagnostics.some(item => item.key === diagnosticKey)) return;
    state.malformedDiagnostics.push({ key: diagnosticKey, reason,
      agent_id: event && event.agent_id || null,
      day: event && event.payload && event.payload.day || null });
    if (state.malformedDiagnostics.length > MAX_MALFORMED_DIAGNOSTICS) {
      state.malformedDiagnostics.shift();
    }
  }
  function collisionDiagnostics(state) {
    return records(state).filter(record => record.conflict)
      .sort(compareKeys)
      .map(record => ({ key: `collision\0${record.key}`,
        reason: "journal day collision ignored; first valid append remains canonical",
        agent_id: record.event.agent_id, day: record.event.payload.day }));
  }
  function minimumRecord(state) {
    let minimum = null;
    for (const record of state.records.values()) {
      if (!minimum || compareKeys(record, minimum) < 0) minimum = record;
    }
    return minimum;
  }
  function enforceCapacity(state) {
    while (state.records.size > MAX_DAYS) {
      const oldest = minimumRecord(state);
      state.records.delete(oldest.key);
      state.capacityDropped += 1;
    }
    state.frontier = state.records.size === MAX_DAYS ? minimumRecord(state).key : null;
  }
  function reset(state) {
    state.records.clear(); state.malformedDiagnostics.length = 0; state.malformed = 0;
    state.capacityDropped = 0; state.frontier = null;
    state.sequence = "0"; state.appendSequence = 0n;
    state.ordinals = new WeakMap(); state.owners.clear();
    state.ownershipDiagnostics.length = 0;
  }
  function foldTrusted(state, batch, options = {}) {
    // Projection only ever needs to revisit records which were retained before
    // this fold and are not retained afterwards.  Capturing that bounded set
    // avoids scanning an agent map whose ordinary history may be much larger.
    const previouslyRetained = records(state);
    if (options.reset) reset(state);
    for (const rejection of options.rejections || []) {
      state.malformed += 1;
      diagnoseMalformed(state, rejection.reason || "malformed journal observation skipped", rejection,
        `malformed\0${state.malformed}\0${rejection.reason || ""}`);
    }
    for (const event of batch || []) {
      state.appendSequence += 1n;
      const ordinal = state.appendSequence.toString();
      state.sequence = ordinal; state.ordinals.set(event, ordinal);
      if (event.type !== "journal_written") continue;
      const key = keyFor(event), current = state.records.get(key);
      if (!current) {
        const candidate = { key, event, conflict: null, ordinal,
          identity: immutableIdentity(event) };
        const frontier = state.records.size === MAX_DAYS ? minimumRecord(state) : null;
        if (frontier && compareKeys(candidate, frontier) <= 0) continue;
        state.records.set(key, candidate);
        enforceCapacity(state); continue;
      }
      if (current.identity === immutableIdentity(event)) continue;
      if (!current.conflict) {
        current.conflict = { event, ordinal };
      }
    }
    state.evictedAgentIds = [...new Set(previouslyRetained
      .filter(record => !state.recordForEvent(record.event))
      .map(record => record.event.agent_id))];
    return state;
  }
  function foldValidated(state, batch, options = {}) {
    if (typeof options.isValidatedBatch !== "function" || !options.isValidatedBatch(batch)) {
      throw new TypeError("foldValidated requires the shared strict validated batch");
    }
    return foldTrusted(state, batch, options);
  }
  function records(state) {
    return state && state.records instanceof Map ? [...state.records.values()] : [];
  }
  function evictedAgentIds(state) {
    return state && Array.isArray(state.evictedAgentIds) ? state.evictedAgentIds.slice() : [];
  }
  function forAgent(state, agentId) {
    return records(state).filter(record => record.event.agent_id === agentId)
      .sort((a, b) => compareOrdinal(b.ordinal, a.ordinal));
  }
  function latestForAgent(state, agentId) { return forAgent(state, agentId)[0] || null; }
  function active(record, now) {
    const at = record && Date.parse(record.event.ts);
    return Number.isFinite(at) && now >= at && now - at < ACTIVE_MS;
  }
  function latestActiveForAgent(state, agentId, now) {
    return forAgent(state, agentId).find(record => active(record, now)) || null;
  }
  function latestOwnedActiveForAgent(state, agentId, now) {
    return forAgent(state, agentId).find(record => active(record, now) &&
      ownerFor(state, record)) || null;
  }
  function sentence(event) {
    return `wrote the journal for ${event.payload.day} after ${event.payload.routine}`;
  }
  function activityEntries(state) {
    return records(state).map(record => ({ event: record.event,
      ts: Date.parse(record.event.ts) || 0, agent_id: record.event.agent_id,
      project: record.event.project, source: record.event.source,
      state: "journal observed", ordinal: record.ordinal }));
  }
  function validResident(resident) {
    return Boolean(resident && resident.valid === true &&
      resident.manifest_version === 1 && Number.isInteger(resident.home));
  }
  function residentMatch(resident) { return resident && (resident.match || resident.meta) || {}; }
  function lineageFor(event, agents) {
    const direct = event && event.payload && event.payload.parent_agent_id;
    if (direct !== undefined && direct !== null && String(direct)) return String(direct);
    const agent = agents instanceof Map ? agents.get(event && event.agent_id) : null;
    return agent && agent.parentAgentId ? String(agent.parentAgentId) : null;
  }
  function resolveOwnership(state, residents = [], agents = new Map()) {
    state.owners.clear(); state.ownershipDiagnostics.length = 0;
    const exact = new Map(), projects = new Map();
    for (const resident of residents || []) {
      if (!validResident(resident)) continue;
      const match = residentMatch(resident);
      if (match.agent_id) {
        const list = exact.get(match.agent_id) || [];
        list.push(resident); exact.set(match.agent_id, list);
      }
      if (match.project) {
        const list = projects.get(match.project) || [];
        list.push(resident); projects.set(match.project, list);
      }
    }
    for (const record of records(state)) {
      const event = record.event;
      const exactMatches = exact.get(event.agent_id) || [];
      let owner = null, reason = null, kind = null;
      if (exactMatches.length === 1) {
        owner = exactMatches[0]; kind = "exact-agent";
      } else if (exactMatches.length > 1) {
        reason = "ambiguous exact resident declarations; journal observation remains Fleet-only";
      } else {
        const parent = lineageFor(event, agents);
        const projectMatches = projects.get(event.project) || [];
        if (parent) {
          reason = `visitor child of ${parent}; shared project cannot grant journal ownership`;
        } else if (projectMatches.length === 1) {
          owner = projectMatches[0]; kind = "project-root";
        } else if (projectMatches.length > 1) {
          reason = "ambiguous project resident declarations; journal observation remains Fleet-only";
        } else {
          reason = "unmatched resident; journal observation remains Fleet-only";
        }
      }
      if (owner) state.owners.set(record.key, { resident: owner, kind });
      else state.ownershipDiagnostics.push({ key: record.key, agent_id: event.agent_id,
        day: event.payload.day, reason });
    }
    return state;
  }
  function ownershipFor(state, record) {
    if (!state || !record || !(state.owners instanceof Map)) return null;
    return state.owners.get(record.key) || null;
  }
  function ownerFor(state, record) {
    const ownership = ownershipFor(state, record);
    return ownership && ownership.resident || null;
  }
  return { MAX_DAYS, MAX_DIAGNOSTICS, MAX_MALFORMED_DIAGNOSTICS, ACTIVE_MS, validate, createState,
    foldTrusted, foldValidated, keyFor, immutableIdentity, records, evictedAgentIds, forAgent,
    latestForAgent, latestActiveForAgent, latestOwnedActiveForAgent, active, sentence, activityEntries,
    compareKeys, compareOrdinal, resolveOwnership, ownershipFor, ownerFor,
    validResident, lineageFor };
});
