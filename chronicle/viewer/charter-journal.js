"use strict";

/* Viewer-direct, read-only Steward identity projection. Nothing in here writes,
 * proxies through Burrow, or persists beyond the lifetime of this JS object. */
(function (root, factory) {
  const transport = typeof module === "object" && module.exports
    ? require("./routine-ledger.js") : root.BurrowRoutines;
  const api = factory(transport);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowIdentity = api;
})(typeof globalThis === "object" ? globalThis : this, function (transport) {
  const JOURNAL_LIMIT = 7;
  const MAX_DIAGNOSTICS = 40;
  const FETCH_TIMEOUT_MS = 10000;
  const MAX_REMOTE_RESIDENTS = 500;
  const MAX_REMOTE_ERRORS = 500;
  const MAX_DIAGNOSTIC_BYTES = 2048;
  const MAX_CHARTER_ITEMS = 100;
  const MAX_JOURNAL_CONCURRENCY = 4;
  const STEWARD_ID = /^[a-z0-9][a-z0-9-]*$/;
  const STEWARD_AGENT_ID = /^[a-z0-9][a-z0-9._-]*:[A-Za-z0-9._:-]+$/;
  const STEWARD_PROJECT = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

  function createState() {
    return { status: "unconfigured", lastSuccessAt: null, residents: new Map(), diagnostics: [] };
  }
  function safeText(value, maximum = 8000) {
    return typeof value === "string" && value.trim() && value.length <= maximum ? value : null;
  }
  function safeDiagnostic(value) {
    return transport.boundedRemoteText(value, MAX_DIAGNOSTIC_BYTES);
  }
  function makeDiagnostic(prefix, detail) {
    const candidate = detail ? `${prefix}: ${detail}` : prefix;
    return safeDiagnostic(candidate) || prefix;
  }
  function canonicalJson(value) {
    if (value === null) return "null";
    if (typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
    if (typeof value === "number") return Number.isFinite(value) ? JSON.stringify(value) : "invalid";
    if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
    if (value && typeof value === "object") return `{${Object.keys(value).sort().map(key =>
      `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
    return `invalid:${typeof value}`;
  }
  function localFingerprint(resident) { return canonicalJson(resident); }
  function validDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const parsed = new Date(`${value}T00:00:00Z`);
    return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value;
  }
  function parseEscalation(value) {
    if (safeText(value)) return { kind: "text", text: value };
    if (!value || typeof value !== "object" || Array.isArray(value) ||
        !Array.isArray(value.when) || !value.when.length || value.when.length > MAX_CHARTER_ITEMS ||
        value.when.some(item => !safeText(item)) || !safeText(value.how) ||
        (value.note !== null && value.note !== undefined &&
          (typeof value.note !== "string" || value.note.length > 8000))) return null;
    return { kind: "policy", when: value.when.slice(), how: value.how,
      note: value.note == null ? null : value.note };
  }
  function parseCharter(value) {
    if (!value || typeof value !== "object" || Array.isArray(value) ||
        !safeText(value.mission) || !Array.isArray(value.duties) || !value.duties.length ||
        value.duties.length > MAX_CHARTER_ITEMS ||
        value.duties.some(item => !safeText(item)) || !Array.isArray(value.rules) ||
        !value.rules.length || value.rules.length > MAX_CHARTER_ITEMS ||
        value.rules.some(item => !safeText(item))) return null;
    const escalation = parseEscalation(value.escalation);
    return escalation ? { mission: value.mission, duties: value.duties.slice(),
      rules: value.rules.slice(), escalation } : null;
  }
  function parseJournal(payload, expectedResident) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload) ||
        payload.resident !== expectedResident || !Array.isArray(payload.entries) ||
        payload.entries.length > 100) return null;
    const entries = [];
    for (const raw of payload.entries) {
      if (!raw || typeof raw !== "object" || Array.isArray(raw) || !validDate(raw.date) ||
          !safeText(raw.text, 20000) ||
          (raw.routine !== null && raw.routine !== undefined && !safeText(raw.routine)) ||
          (raw.resident !== null && raw.resident !== undefined &&
            (!safeText(raw.resident) || raw.resident !== expectedResident))) return null;
      entries.push({ date: raw.date, text: raw.text, routine: raw.routine || null,
        resident: raw.resident || null });
    }
    entries.sort((a, b) => b.date.localeCompare(a.date));
    return entries.slice(0, JOURNAL_LIMIT);
  }
  function localKey(resident) { return resident && safeText(resident.file); }
  function validIdentity(value, pattern) {
    return typeof value === "string" && pattern.test(value) ? value : null;
  }
  function identityEvidence(value) { return typeof value === "string" ? value : null; }
  function matchRemote(local, remotes, malformedEvidence = []) {
    const match = local && local.match || {};
    const collides = remote => match.agent_id ? remote.agent_id === match.agent_id :
      match.project ? remote.project === match.project : false;
    const candidates = remotes.filter(collides);
    const possible = Array.isArray(malformedEvidence) ? malformedEvidence.filter(collides) :
      malformedEvidence ? [{}] : [];
    return candidates.length === 1 && !possible.length ? { remote: candidates[0] } :
      candidates.length + possible.length > 1 || (candidates.length && possible.length) ?
      { error: "more than one Steward resident matches this declaration" } :
      possible.length ?
        { error: "Steward returned malformed resident candidates; absence cannot be established" } :
        { error: "no Steward resident matches this declaration", missing: true };
  }
  function assignRemotes(locals, remotes, malformedEvidence = []) {
    const assignments = new Map(), reserved = new Set();
    const eligible = (locals || []).filter(local => local && local.valid === true && localKey(local));
    function candidates(local) {
      const matched = matchRemote(local, remotes, malformedEvidence);
      if (!matched.remote) return matched;
      return { ...matched, remote: matched.remote };
    }
    const exact = eligible.filter(local => local.match && local.match.agent_id);
    // An exact declaration reserves every valid remote candidate before any
    // project fallback is considered, even when duplicate local claims or
    // malformed collision evidence make the exact association unusable.
    for (const local of exact) for (const remote of remotes) {
      if (remote.agent_id === local.match.agent_id) reserved.add(remote);
    }
    const exactCandidates = new Map(exact.map(local => [localKey(local), candidates(local)]));
    const exactClaims = new Map();
    for (const local of exact) {
      const matched = exactCandidates.get(localKey(local));
      if (!matched.remote) continue;
      const claims = exactClaims.get(matched.remote) || [];
      claims.push(local); exactClaims.set(matched.remote, claims);
    }
    for (const local of exact) {
      const key = localKey(local), matched = exactCandidates.get(key);
      if (!matched.remote) { assignments.set(key, matched); continue; }
      if (exactClaims.get(matched.remote).length !== 1) {
        assignments.set(key, { error: "more than one local resident claims this Steward identity" });
        continue;
      }
      assignments.set(key, matched);
    }

    const fallback = eligible.filter(local => local.match && !local.match.agent_id && local.match.project);
    const fallbackCandidates = new Map(fallback.map(local => [localKey(local), candidates(local)]));
    const fallbackClaims = new Map();
    for (const local of fallback) {
      const matched = fallbackCandidates.get(localKey(local));
      if (!matched.remote) continue;
      const claims = fallbackClaims.get(matched.remote) || [];
      claims.push(local); fallbackClaims.set(matched.remote, claims);
    }
    for (const local of fallback) {
      const key = localKey(local), matched = fallbackCandidates.get(key);
      if (!matched.remote) { assignments.set(key, matched); continue; }
      if (reserved.has(matched.remote)) {
        assignments.set(key, { error: "Steward identity is reserved by an exact agent_id declaration" });
      } else if (fallbackClaims.get(matched.remote).length !== 1) {
        assignments.set(key, { error: "more than one local resident claims this Steward identity" });
      } else {
        assignments.set(key, matched); reserved.add(matched.remote);
      }
    }
    return assignments;
  }
  const readJson = async (fetcher, config, path, timing) =>
    (await transport.requestJson(config, path, fetcher,
      { timeoutMs: timing.timeoutMs ?? FETCH_TIMEOUT_MS, ...timing })).body;
  function errorStatus(error) {
    if (error && error.aborted) return "aborted";
    if (error && error.kind === "authentication") return "authentication";
    if (error && error.status === 404 && error.code === "unknown_resident") return "missing";
    if (error && error.status === 404) return "error";
    if (error && error.kind === "conflict") return "malformed";
    if (error && error.kind === "http") return "error";
    return "unreachable";
  }
  function staleRecord(previous, status, diagnostic) {
    return { ...(previous || {}), status, diagnostic,
      stale: Boolean(previous && previous.lastSuccessAt),
      journal: previous && previous.journal ? { ...previous.journal, status,
        diagnostic, stale: Boolean(previous.journal.lastSuccessAt) } : null };
  }
  function degradedResidents(previous, locals, status, diagnostic) {
    return new Map((locals || []).filter(local => local && local.valid === true && localKey(local))
      .map(local => {
        const fingerprint = localFingerprint(local);
        const candidate = previous.residents.get(localKey(local));
        const old = candidate && candidate.localFingerprint === fingerprint ? candidate : null;
        return [localKey(local), old ? staleRecord(old, status, diagnostic) :
          { status, remoteId: null, charter: null, lastSuccessAt: null, stale: false,
            localFingerprint: fingerprint,
            diagnostic, journal: { status, entries: [], lastSuccessAt: null,
              stale: false, diagnostic, localFingerprint: fingerprint, remoteId: null } }];
      }));
  }
  function localFeedUnavailable(previous, locals) {
    const prior = previous || createState();
    const diagnostic = "Burrow resident manifest feed unavailable; identity cannot be revalidated";
    return { ...prior, status: "local-unavailable", diagnostics: [diagnostic],
      residents: degradedResidents(prior, locals, "local-unavailable", diagnostic) };
  }
  async function refresh(previous, config, locals, fetcher, now = Date.now(), timing = {}) {
    const prior = previous || createState();
    const localAvailable = () => typeof timing.localAvailable === "function" ?
      timing.localAvailable() : timing.localAvailable !== false;
    if (!localAvailable()) return localFeedUnavailable(prior, locals);
    let directory;
    try { directory = await readJson(fetcher, config, "/residents", timing); }
    catch (error) {
      if (error && error.aborted) return prior;
      // A 404 for the directory says nothing about any individual resident.
      // Only the resident-specific journal route may establish `missing`.
      const classified = errorStatus(error);
      const status = classified === "missing" ? "error" : classified;
      const message = safeDiagnostic(error && error.message) || "Steward returned an invalid diagnostic";
      const failure = makeDiagnostic("Steward resident read failed", message);
      return { ...prior, status, diagnostics: [failure],
        residents: degradedResidents(prior, locals, status, failure) };
    }
    if (!localAvailable()) return localFeedUnavailable(prior, locals);
    if (!directory || typeof directory !== "object" || Array.isArray(directory) ||
        !Array.isArray(directory.residents) || directory.residents.length > MAX_REMOTE_RESIDENTS ||
        !Array.isArray(directory.errors) || directory.errors.length > MAX_REMOTE_ERRORS ||
        directory.errors.some(item => !safeDiagnostic(item))) {
      const diagnostic = "Steward resident response is malformed";
      return { ...prior, status: "malformed", diagnostics: [diagnostic],
        residents: degradedResidents(prior, locals, "malformed", diagnostic) };
    }
    const remotes = [], diagnostics = directory.errors.slice(0, MAX_DIAGNOSTICS)
      .map(item => makeDiagnostic("Steward manifest", item));
    const malformedEvidence = [], remoteIdCounts = new Map();
    for (const item of directory.residents) {
      const object = item && typeof item === "object" && !Array.isArray(item);
      const rawRemoteId = object && identityEvidence(item.id);
      if (rawRemoteId) remoteIdCounts.set(rawRemoteId, (remoteIdCounts.get(rawRemoteId) || 0) + 1);
      const rawAgentId = object && identityEvidence(item.agent_id);
      const rawProject = object && identityEvidence(item.project);
      const remoteId = validIdentity(rawRemoteId, STEWARD_ID);
      const agentId = validIdentity(rawAgentId, STEWARD_AGENT_ID);
      const project = validIdentity(rawProject, STEWARD_PROJECT);
      const validAgent = object && Object.hasOwn(item, "agent_id") &&
        (item.agent_id === null || Boolean(agentId));
      const validProject = object && Object.hasOwn(item, "project") &&
        (item.project === null || Boolean(project));
      if (!object || !remoteId || !validAgent || !validProject || (!agentId && !project)) {
        malformedEvidence.push({ agent_id: rawAgentId, project: rawProject });
        diagnostics.push("Steward returned a malformed resident identity; matching authority is invalid");
        continue;
      }
      remotes.push({ ...item, id: remoteId, agent_id: agentId, project });
    }
    // Remote ids are the authority used for journal URLs and payload binding.
    // Validate uniqueness across the complete directory before matching any
    // local declaration; otherwise two locals can inherit one remote journal.
    const duplicatedRemoteIds = new Set([...remoteIdCounts]
      .filter(([, count]) => count > 1).map(([id]) => id));
    if (duplicatedRemoteIds.size) {
      const uniqueRemotes = [];
      for (const remote of remotes) {
        if (!duplicatedRemoteIds.has(remote.id)) { uniqueRemotes.push(remote); continue; }
        malformedEvidence.push({ agent_id: remote.agent_id, project: remote.project });
      }
      remotes.splice(0, remotes.length, ...uniqueRemotes);
      for (const id of duplicatedRemoteIds) diagnostics.push(
        makeDiagnostic("Steward returned a duplicated resident identity", id));
    }
    const assignments = assignRemotes(locals, remotes, malformedEvidence);
    const residents = new Map();
    const queue = (locals || []).filter(local => local && local.valid === true);
    let cursor = 0;
    async function worker() {
      while (cursor < queue.length) {
        const local = queue[cursor++];
        const key = localKey(local);
        if (!key) continue;
        const fingerprint = localFingerprint(local);
        const candidate = prior.residents.get(key);
        const old = candidate && candidate.localFingerprint === fingerprint ? candidate : null;
        // Steward omits invalid manifests from `residents` and reports them in
        // `errors`. An unmatched local declaration therefore cannot be called
        // absent while either malformed candidate evidence exists.
        const matched = assignments.get(key) ||
          { error: "local resident has no valid identity declaration" };
        if (!matched.remote && matched.missing && directory.errors.length) {
          delete matched.missing;
          matched.error = "Steward reported invalid manifests; absence cannot be established";
        }
        if (!matched.remote) {
          const oldJournal = old && old.journal && old.remoteId ? { ...old.journal,
            status: matched.missing ? "missing" : "malformed", stale: Boolean(old.journal.lastSuccessAt),
            diagnostic: matched.error, localFingerprint: fingerprint, remoteId: old.remoteId } : null;
          residents.set(key, { status: matched.missing ? "missing" : "invalid", remoteId: null,
            localFingerprint: fingerprint, charter: null, journal: oldJournal, lastSuccessAt: null,
            diagnostic: matched.error, stale: false });
          diagnostics.push(`${key}: ${matched.error}`); continue;
        }
        const remote = matched.remote, charter = parseCharter(remote.charter);
        const charterMissing = remote.charter === null || remote.charter === undefined;
        const base = { status: charter ? "configured" : charterMissing ? "missing" : "invalid",
          remoteId: remote.id, localFingerprint: fingerprint, charter,
          diagnostic: charter ? null : charterMissing ? "Steward resident declares no charter" :
            "Steward charter is malformed",
          lastSuccessAt: now, stale: false };
        if (!charter) diagnostics.push(`${key}: ${base.diagnostic}`);
        try {
          const payload = await readJson(fetcher, config,
            `/residents/${encodeURIComponent(remote.id)}/journal?limit=${JOURNAL_LIMIT}`, timing);
          if (!localAvailable() || localFingerprint(local) !== fingerprint ||
              (typeof timing.isCurrent === "function" && !timing.isCurrent(local, key, fingerprint))) continue;
          const entries = parseJournal(payload, remote.id);
          if (!entries) throw Object.assign(new Error("Steward journal response is malformed"),
            { malformed: true });
          residents.set(key, { ...base, journal: { status: "loaded", entries,
            lastSuccessAt: now, stale: false, diagnostic: null,
            localFingerprint: fingerprint, remoteId: remote.id } });
        } catch (error) {
          if (error && error.aborted) continue;
          if (!localAvailable() || localFingerprint(local) !== fingerprint ||
              (typeof timing.isCurrent === "function" && !timing.isCurrent(local, key, fingerprint))) continue;
          const status = error.malformed ? "malformed" : errorStatus(error);
          const labels = { authentication: "Steward authentication rejected", missing: "Steward resident missing",
            malformed: "Could not read journal", unreachable: "Steward journal unreachable" };
          const message = safeDiagnostic(error && error.message) || "Steward returned an invalid diagnostic";
          const failure = makeDiagnostic(labels[status] || "Could not read journal", message);
          const oldJournal = old && old.remoteId === remote.id &&
            old.localFingerprint === fingerprint ? old.journal : null;
          residents.set(key, { ...base, journal: { ...(oldJournal || { entries: [] }), status,
            stale: Boolean(oldJournal && oldJournal.lastSuccessAt), diagnostic: failure,
            localFingerprint: fingerprint, remoteId: remote.id } });
          diagnostics.push(makeDiagnostic(key, failure));
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(MAX_JOURNAL_CONCURRENCY, queue.length) }, worker));
    if (!localAvailable()) return localFeedUnavailable(prior, locals);
    return { status: "loaded", lastSuccessAt: now, residents,
      diagnostics: diagnostics.slice(-MAX_DIAGNOSTICS)
        .map(item => safeDiagnostic(item) || "An oversized Steward diagnostic was omitted") };
  }
  function recordFor(state, resident) {
    const record = state && state.residents instanceof Map ? state.residents.get(localKey(resident)) || null : null;
    return record && record.localFingerprint === localFingerprint(resident) ? record : null;
  }
  return { JOURNAL_LIMIT, MAX_DIAGNOSTICS, FETCH_TIMEOUT_MS, createState, parseCharter, parseJournal,
    MAX_JOURNAL_CONCURRENCY, MAX_DIAGNOSTIC_BYTES, localFingerprint, matchRemote, assignRemotes,
    refresh, recordFor, localFeedUnavailable };
});
