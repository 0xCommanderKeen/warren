"use strict";

(function (root, factory) {
  const lifecycle = typeof module === "object" && module.exports ?
    require("./routine-lifecycle.js") : root.BurrowRoutineLifecycle;
  const api = factory(lifecycle);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowRoutines = api;
})(typeof globalThis === "object" ? globalThis : this, function (lifecycle) {
  const TYPES = new Set(["routine_started", "routine_finished", "routine_failed"]);
  const MAX_RUNS = 20;
  const DEFAULT_STALE_MS = 30 * 60 * 1000;
  const DEFAULT_ACK_MS = 15 * 1000;
  const DEFAULT_FETCH_MS = 10 * 1000;
  const MAX_ACKNOWLEDGEMENTS = 200;
  const MAX_UINT64 = "18446744073709551615";
  const OBSERVABLE_TRANSPORTS = new Set(["live", "polling"]);
  const ACTIVE_ACK_STATES = new Set([
    "requesting", "pending", "unacknowledged", "uncertain", "running", "indeterminate",
  ]);

  function text(value) { return typeof value === "string" && value.trim() ? value : null; }
  function stewardSlug(value) { return typeof value === "string" && /^[a-z0-9][a-z0-9-]*$/.test(value); }
  function finite(value) { return typeof value === "number" && Number.isFinite(value) && value >= 0; }
  function validate(event, validateEvent) {
    if (validateEvent && validateEvent(event)) return null;
    if (!event || !TYPES.has(event.type) || event.source !== "steward" ||
        !text(event.agent_id) || !text(event.project) ||
        !text(event.ts) || !Number.isFinite(Date.parse(event.ts)) ||
        !event.payload || typeof event.payload !== "object" || Array.isArray(event.payload) ||
        !text(event.payload.routine) || !text(event.payload.run_id)) return null;
    const p = event.payload;
    if (event.type === "routine_started" && !["manual", "schedule"].includes(p.trigger)) return null;
    if (event.type === "routine_finished" && (!text(p.outcome) || !finite(p.duration_s) ||
        !Array.isArray(p.artifacts) || !p.artifacts.every(text))) return null;
    if (event.type === "routine_failed" && (!text(p.error) ||
        (p.duration_s !== undefined && !finite(p.duration_s)))) return null;
    return event;
  }

  function project(events, now = Date.now(), options = {}) {
    const staleMs = options.staleMs ?? DEFAULT_STALE_MS;
    const runs = new Map(), diagnostics = [];
    for (const event of events || []) {
      if (!event || !TYPES.has(event.type)) continue;
      const valid = validate(event, options.validateEvent);
      if (!valid) {
        diagnostics.push({ type: event && event.type || "routine", reason: "malformed routine payload skipped" });
        continue;
      }
      const key = `${event.agent_id}\0${event.payload.routine}\0${event.payload.run_id}`;
      let run = runs.get(key);
      if (!run) {
        run = { agent_id: event.agent_id, project: event.project, routine: event.payload.routine,
          run_id: event.payload.run_id, started_at: null, trigger: null, lifecycle: null,
          outcome: null, duration_s: null, artifacts: [], error: null, closed_at: null };
        runs.set(key, run);
      }
      const at = Date.parse(event.ts);
      if (event.type === "routine_started") {
        if (!run.start_event || lifecycle.preferStart(event, run.start_event)) {
          run.start_event = event;
          run.started_at = at; run.trigger = event.payload.trigger; run.project = event.project;
        }
      } else if (!run.terminal_event || lifecycle.preferTerminal(event, run.terminal_event)) {
        run.terminal_event = event;
        run.closed_at = at;
        run.lifecycle = event.type === "routine_failed" ? "failed" : "finished";
        run.outcome = event.payload.outcome || null;
        run.duration_s = event.payload.duration_s ?? null;
        run.artifacts = event.payload.artifacts || [];
        run.error = event.payload.error || null;
      }
    }
    const byRoutine = new Map();
    for (const run of runs.values()) {
      if (run.started_at === null) {
        diagnostics.push({ type: "routine", reason: "closing event without matching start skipped" });
        continue;
      }
      if (run.closed_at !== null && run.closed_at < run.started_at) {
        diagnostics.push({ type: "routine", reason: "closing event predates matching start; run left open" });
        run.closed_at = null; run.lifecycle = null; run.outcome = null; run.duration_s = null;
        run.artifacts = []; run.error = null;
      }
      run.state = run.lifecycle || (now - run.started_at > staleMs ? "stale" : "running");
      delete run.start_event; delete run.terminal_event;
      const key = `${run.agent_id}\0${run.routine}`;
      const list = byRoutine.get(key) || [];
      list.push(run); byRoutine.set(key, list);
    }
    for (const list of byRoutine.values()) {
      list.sort((a, b) => b.started_at - a.started_at || b.run_id.localeCompare(a.run_id));
      list.splice(MAX_RUNS);
    }
    return { byRoutine, diagnostics };
  }

  function historyFor(resident, routine, projection) {
    const match = resident.match || {};
    if (match.agent_id) return projection.byRoutine.get(`${match.agent_id}\0${routine.id}`) || [];
    const history = [];
    for (const runs of projection.byRoutine.values()) for (const run of runs) {
      if (run.project === match.project && run.routine === routine.id) history.push(run);
    }
    history.sort((a, b) => b.started_at - a.started_at || b.run_id.localeCompare(a.run_id));
    return history.slice(0, MAX_RUNS);
  }

  function declared(residents, projection) {
    const rows = [];
    for (const resident of residents || []) {
      if (!resident || resident.valid !== true) continue;
      for (const routine of resident.routines || []) {
        const history = historyFor(resident, routine, projection);
        rows.push({ resident, routine, history, latest: history[0] || null,
          agent_id: history[0] && history[0].agent_id || (resident.match || {}).agent_id || null,
          state: history[0] ? history[0].state : "never-observed" });
      }
    }
    return rows;
  }

  function correlationKey(correlation, routine) {
    const match = typeof correlation === "string" ? { agent_id: correlation } : correlation || {};
    return `${match.agent_id ? "agent:" + match.agent_id : "project:" + (match.project || "")}\0${routine}`;
  }

  function startIdentity(event) {
    const payload = event && event.payload || {};
    return [event && event.source, event && event.agent_id, event && event.project, payload.routine,
      payload.run_id].map(value => String(value ?? "")).join("\0");
  }

  function canRun(routine, stewardDeclaration) {
    return Boolean(routine && routine.enabled !== false &&
      (!stewardDeclaration ||
        (stewardDeclaration.enabled === true && stewardDeclaration.retired !== true)));
  }

  function canRunAuthoritatively(routine, stewardDeclaration, declarationState) {
    return declarationState === "loaded" && Boolean(stewardDeclaration) &&
      canRun(routine, stewardDeclaration);
  }

  function runDisabled(routine, stewardDeclaration, declarationState, acknowledgement, observed,
    telemetry = { ok: true }) {
    const authorityBlocks = declarationState !== "unconfigured" &&
      !canRunAuthoritatively(routine, stewardDeclaration, declarationState);
    const history = Array.isArray(observed) ? observed : observed ? [observed] : [];
    return telemetry.ok === false || authorityBlocks || !routine || routine.enabled === false ||
      history.some(run => ["running", "stale"].includes(run.state)) ||
      Boolean(acknowledgement && ACTIVE_ACK_STATES.has(acknowledgement.state));
  }

  function uint64(value) {
    if (!/^(?:0|[1-9]\d*)$/.test(value) || value.length > MAX_UINT64.length ||
        (value.length === MAX_UINT64.length && value > MAX_UINT64)) return null;
    return value;
  }

  function cursor(value) {
    if (typeof value !== "string") return null;
    const parts = value.split(":");
    if (parts.length !== 6 || parts[0] !== "v1" || !/^[0-9a-f]{32}$/.test(parts[1])) return null;
    const numbers = parts.slice(2).map(uint64);
    if (numbers.some(value => value === null)) return null;
    return { namespace: [parts[0], parts[1], ...numbers.slice(0, 3)].join(":"),
      offset: numbers[3] };
  }

  function offsetAfter(candidate, boundary) { return laterOffset(candidate, boundary); }

  function laterOffset(candidate, boundary) {
    return candidate.length > boundary.length ||
      (candidate.length === boundary.length && candidate > boundary);
  }

  function boundaryAvailability(value) {
    return cursor(value) ? { ok: true } : { ok: false, reason: "boundary",
      message: "Run Now unavailable: exact telemetry cursor is not available; no request was sent" };
  }

  function telemetryAvailability(transport, value) {
    if (!OBSERVABLE_TRANSPORTS.has(transport)) {
      return { ok: false, reason: "transport",
        message: `Run Now unavailable: telemetry is ${transport || "disconnected"}; no request was sent` };
    }
    return boundaryAvailability(value);
  }

  function createAcknowledgements(timeoutMs = DEFAULT_ACK_MS, maxEntries = MAX_ACKNOWLEDGEMENTS) {
    const pending = new Map();
    function normalize(correlation) {
      return typeof correlation === "string" ? { agent_id: correlation } : correlation || {};
    }
    function trim() {
      while (pending.size > maxEntries) {
        const victim = [...pending.entries()]
          .filter(([, item]) => !ACTIVE_ACK_STATES.has(item.state))
          .sort((a, b) => (a[1].updatedAt || a[1].requestedAt) -
            (b[1].updatedAt || b[1].requestedAt))[0];
        if (!victim) return false;
        pending.delete(victim[0]);
      }
      return true;
    }
    function availability(correlation, routine) {
      const current = pending.get(correlationKey(normalize(correlation), routine));
      if (current && ACTIVE_ACK_STATES.has(current.state)) {
        return { ok: false, reason: "active",
          message: "this routine already has an unresolved run request" };
      }
      if (pending.size >= maxEntries &&
          ![...pending.values()].some(item => !ACTIVE_ACK_STATES.has(item.state))) {
        return { ok: false, reason: "capacity",
          message: "run-request tracking is full of unresolved requests; no request was sent" };
      }
      return { ok: true };
    }
    function observation(value) {
      if (Array.isArray(value)) return { events: value, cursor: null };
      return value && typeof value === "object" ?
        { events: value.events || [], cursor: cursor(value.cursor), reset: value.reset === true } :
        { events: [], cursor: null, reset: false };
    }
    function afterBoundary(item, observed) {
      if (!item.boundary) return !observed.cursor;
      return Boolean(observed.cursor && !observed.reset &&
        observed.cursor.namespace === item.boundary.namespace &&
        laterOffset(observed.cursor.offset, item.boundary.offset));
    }
    function matches(item, event) {
      return event.payload.routine === item.routine && event.payload.trigger === "manual" &&
        (item.correlation.agent_id ? event.agent_id === item.correlation.agent_id :
          event.project === item.correlation.project);
    }
    function acknowledge(item, event) {
      item.state = "running"; item.run_id = event.payload.run_id;
      item.agent_id = event.agent_id; item.acknowledgedAt = Date.parse(event.ts);
      item.start = event; item.updatedAt = item.acknowledgedAt;
    }
    function promoteCandidate(item) {
      if (!item.candidate) return false;
      acknowledge(item, item.candidate);
      item.terminalEvidence = item.candidateTerminalEvidence ||
        rememberedTerminal(item, item.candidate) || item.terminalEvidence;
      if (item.terminalEvidence &&
          Date.parse(item.terminalEvidence.ts) >= item.acknowledgedAt) {
        close(item, item.terminalEvidence);
      } else markEvidenceUncertain(item);
      delete item.candidate;
      delete item.candidateTerminalEvidence;
      return true;
    }
    function markEvidenceUncertain(item) {
      const hasClosingEvidence = item.terminalEvidence &&
        Date.parse(item.terminalEvidence.ts) >= item.acknowledgedAt;
      if (!hasClosingEvidence && item.terminalEvidenceLoss &&
          item.acknowledgedAt <= item.terminalEvidenceLoss.through) {
        item.state = "indeterminate";
        item.updatedAt = Math.max(item.updatedAt, item.terminalEvidenceLoss.through);
      }
    }
    function close(item, event) {
      item.state = event.type === "routine_failed" ? "failed" : "completed";
      item.closedAt = Date.parse(event.ts); item.updatedAt = item.closedAt;
      item.terminal = event;
    }
    function terminalIdentity(event) {
      const payload = event && event.payload || {};
      return [event && event.agent_id, payload.routine, payload.run_id]
        .map(value => String(value ?? "")).join("\0");
    }
    function rememberTerminals(item, events) {
      if (!item.terminalCandidates) item.terminalCandidates = new Map();
      for (const event of events) {
        if (event.type === "routine_started" || event.payload.routine !== item.routine ||
            (item.correlation.agent_id ? event.agent_id !== item.correlation.agent_id :
              event.project !== item.correlation.project)) continue;
        const key = terminalIdentity(event), current = item.terminalCandidates.get(key);
        if (!current || lifecycle.preferTerminal(event, current)) {
          item.terminalCandidates.delete(key);
          item.terminalCandidates.set(key, event);
        }
      }
      while (item.terminalCandidates.size > MAX_RUNS) {
        const key = item.terminalCandidates.keys().next().value;
        const victim = item.terminalCandidates.get(key);
        item.terminalCandidates.delete(key);
        const lostAt = Date.parse(victim.ts);
        const loss = item.terminalEvidenceLoss || { count: 0, through: -Infinity };
        loss.count = Math.min(Number.MAX_SAFE_INTEGER, loss.count + 1);
        loss.through = Math.max(loss.through, lostAt);
        item.terminalEvidenceLoss = loss;
      }
    }
    function rememberedTerminal(item, start) {
      return item.terminalCandidates && item.terminalCandidates.get(terminalIdentity(start));
    }
    function terminalFor(events, item, start) {
      return lifecycle.selectTerminal(events, event => event.agent_id === start.agent_id &&
        event.payload.routine === start.payload.routine &&
        event.payload.run_id === start.payload.run_id &&
        Date.parse(event.ts) >= Date.parse(start.ts));
    }
    function request(correlation, routine, requestedAt = Date.now(), observed = {}, validateEvent) {
      const available = availability(correlation, routine);
      if (!available.ok) return available;
      if (!Array.isArray(observed) && Object.prototype.hasOwnProperty.call(observed, "cursor")) {
        const boundary = boundaryAvailability(observed.cursor);
        if (!boundary.ok) return boundary;
      }
      const match = normalize(correlation);
      const key = correlationKey(match, routine);
      const baseline = observation(observed);
      const item = { correlation: match, routine, requestedAt, updatedAt: requestedAt,
        state: "requesting", boundary: baseline.cursor };
      // Compatibility for non-browser callers without a production cursor:
      // retain only this request's bounded baseline, never a process-global set.
      if (!item.boundary) item.baseline = new Set(baseline.events.slice(-MAX_RUNS * 2)
        .map(event => validate(event, validateEvent)).filter(event => event &&
          event.type === "routine_started").map(startIdentity));
      pending.set(key, item);
      if (!trim()) {
        pending.delete(key);
        return { ok: false, reason: "capacity",
          message: "run-request tracking is full of unresolved requests; no request was sent" };
      }
      return { ok: true };
    }
    return {
      availability,
      request,
      requested(correlation, routine, requestedAt = Date.now(), observed = {}, validateEvent) {
        return request(correlation, routine, requestedAt, observed, validateEvent).ok;
      },
      accepted(correlation, routine, requestId, acceptedAt = Date.now()) {
        const key = correlationKey(correlation, routine);
        const item = pending.get(key) || { correlation: normalize(correlation),
          routine, requestedAt: acceptedAt };
        Object.assign(item, { requestId, acceptedAt, updatedAt: acceptedAt, state: "pending" });
        promoteCandidate(item);
        pending.set(key, item);
        if (!trim()) pending.delete(key);
      },
      failed(correlation, routine, error = "run request failed", failedAt = Date.now()) {
        const key = correlationKey(correlation, routine);
        const item = pending.get(key) || { correlation: typeof correlation === "string" ?
          { agent_id: correlation } : correlation, routine, requestedAt: failedAt };
        Object.assign(item, { state: "request-failed", error: text(error) || "run request failed",
          failedAt, updatedAt: failedAt });
        pending.set(key, item);
        if (!trim()) pending.delete(key);
      },
      uncertain(correlation, routine, error = "run request outcome is uncertain",
          failedAt = Date.now()) {
        const key = correlationKey(correlation, routine);
        const item = pending.get(key) || { correlation: normalize(correlation),
          routine, requestedAt: failedAt };
        // Transport/abort/body failures can happen after Steward accepted the
        // POST. Exact lifecycle evidence always outranks that ambiguity.
        if (["running", "indeterminate", "completed", "failed"].includes(item.state) ||
            promoteCandidate(item)) return;
        Object.assign(item, { state: "uncertain",
          error: text(error) || "run request outcome is uncertain",
          failedAt, updatedAt: failedAt });
        pending.set(key, item);
        if (!trim()) pending.delete(key);
      },
      observe(value, now = Date.now(), validateEvent) {
        const observed = observation(value);
        const validEvents = observed.events.map(event => validate(event, validateEvent)).filter(Boolean);
        for (const item of pending.values()) {
          // A reset publication is a replay/bootstrap snapshot. It establishes
          // the end of the new generation, but none of its contents can prove
          // which event followed this request. Later publications beyond this
          // rebased cursor are eligible normally.
          if (observed.reset && observed.cursor && item.boundary &&
              ["requesting", "pending", "unacknowledged", "uncertain"].includes(item.state)) {
            item.boundary = observed.cursor;
          }
          const eligible = afterBoundary(item, observed);
          const lifecycleEvents = eligible ? validEvents : [];
          const freshStarts = eligible ? validEvents.filter(event => {
            if (event.type !== "routine_started") return false;
            return !item.baseline || !item.baseline.has(startIdentity(event));
          }) : [];
          if (["requesting", "pending", "unacknowledged", "uncertain"].includes(item.state)) {
            rememberTerminals(item, lifecycleEvents);
          }
          if (item.state === "requesting") {
            const candidate = lifecycle.selectStart(freshStarts, event => matches(item, event));
            if (candidate && (!item.candidate ||
                (startIdentity(candidate) === startIdentity(item.candidate) &&
                  lifecycle.preferStart(candidate, item.candidate)))) item.candidate = candidate;
            if (item.candidate) {
              // Keep one bounded exact-run terminal even when it currently
              // predates the selected start. A delayed earlier duplicate start
              // can make that already-observed close valid before POST returns.
              const terminal = lifecycle.selectTerminal(lifecycleEvents, event =>
                event.agent_id === item.candidate.agent_id &&
                event.payload.routine === item.candidate.payload.routine &&
                event.payload.run_id === item.candidate.payload.run_id);
              if (terminal && lifecycle.preferTerminal(
                  terminal, item.candidateTerminalEvidence)) {
                item.candidateTerminalEvidence = terminal;
              }
            }
          }
          if (["pending", "unacknowledged", "uncertain"].includes(item.state)) {
            const match = lifecycle.selectStart(freshStarts, event => matches(item, event));
            if (match) {
              acknowledge(item, match);
              const terminal = lifecycle.selectTerminal([
                rememberedTerminal(item, match), terminalFor(lifecycleEvents, item, match),
              ].filter(Boolean), () => true);
              if (terminal) item.terminalEvidence = terminal;
              if (terminal && Date.parse(terminal.ts) >= item.acknowledgedAt) close(item, terminal);
              else markEvidenceUncertain(item);
            }
            else if (item.state === "pending" &&
                now - (item.acceptedAt ?? item.requestedAt) >= timeoutMs) {
              item.state = "unacknowledged"; item.updatedAt = now;
            }
          }
          if (["running", "indeterminate", "completed", "failed"].includes(item.state)) {
            // The request boundary identifies a fresh start. After that exact
            // run identity is confirmed, its terminal event remains valid
            // across cursor generation resets and reconnect replays.
            const exactStart = lifecycle.selectStart(validEvents, event =>
              event.agent_id === item.agent_id && event.payload.routine === item.routine &&
              event.payload.run_id === item.run_id);
            if (exactStart && lifecycle.preferStart(exactStart, item.start)) {
              item.start = exactStart; item.acknowledgedAt = Date.parse(exactStart.ts);
            }
            const evidence = lifecycle.selectTerminal(validEvents, event =>
              event.agent_id === item.agent_id && event.payload.routine === item.routine &&
              event.payload.run_id === item.run_id);
            if (evidence && lifecycle.preferTerminal(evidence, item.terminalEvidence)) {
              item.terminalEvidence = evidence;
            }
            if (item.terminalEvidence &&
                Date.parse(item.terminalEvidence.ts) >= item.acknowledgedAt &&
                lifecycle.preferTerminal(item.terminalEvidence, item.terminal)) {
              close(item, item.terminalEvidence);
            }
          }
        }
        trim();
      },
      get(correlation, routine) { return pending.get(correlationKey(correlation, routine)) || null; },
      size() { return pending.size; },
    };
  }

  function requestOptions(token, method = "GET") {
    return { method, headers: { Authorization: `Bearer ${token}` }, credentials: "omit" };
  }
  async function responseBody(response) { try { return await response.json(); } catch (_) { return {}; } }
  function requireConfig(config) {
    if (!text(config && config.url) || !text(config && config.token)) throw new Error("Steward URL and token are required");
    return config.url.replace(/\/+$/, "");
  }

  async function withDeadline(operation, timing = {}) {
    const timeoutMs = timing.timeoutMs ?? DEFAULT_FETCH_MS;
    const schedule = timing.setTimeout || setTimeout;
    const cancel = timing.clearTimeout || clearTimeout;
    const controller = new AbortController();
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = schedule(() => {
        controller.abort();
        reject(new Error("Steward request timed out"));
      }, timeoutMs);
    });
    try { return await Promise.race([operation(controller.signal), timeout]); }
    finally { if (timer !== undefined) cancel(timer); }
  }

  async function fetchDeclarations(config, fetchImpl = fetch, timing) {
    const base = requireConfig(config);
    const { response, body } = await withDeadline(async signal => {
      const options = { ...requestOptions(config.token), signal };
      const response = await fetchImpl(`${base}/routines`, options);
      return { response, body: await responseBody(response) };
    }, timing);
    if (response.status !== 200 || !Array.isArray(body.routines)) {
      throw new Error(`Steward routines unavailable (${response.status})`);
    }
    const byRoutine = new Map();
    for (const item of body.routines) {
      if (!item || !stewardSlug(item.resident) || !stewardSlug(item.routine) ||
          (item.next_fire !== null && (!text(item.next_fire) || !Number.isFinite(Date.parse(item.next_fire))))) continue;
      byRoutine.set(`${item.resident}\0${item.routine}`, {
        next_fire: item.next_fire, enabled: item.enabled === true, retired: item.retired === true,
      });
    }
    return byRoutine;
  }

  async function runNow(config, residentId, routineId, fetchImpl = fetch, timing) {
    const base = requireConfig(config);
    if (!stewardSlug(residentId) || !stewardSlug(routineId)) {
      throw new Error("Steward resident and routine must be lowercase slugs");
    }
    const { response, body } = await withDeadline(async signal => {
      const options = { ...requestOptions(config.token, "POST"), signal };
      const response = await fetchImpl(`${base}/residents/${encodeURIComponent(residentId)}/routines/${encodeURIComponent(routineId)}/run`, options);
      if (Number.isInteger(response && response.status) &&
          response.status >= 100 && response.status <= 599 && response.status !== 202) {
        const error = new Error(`Steward refused the request (${response.status})`);
        error.definitive = true;
        throw error;
      }
      if (!response || response.status !== 202 || typeof response.json !== "function") {
        throw new Error("Steward returned an invalid response; request outcome is uncertain");
      }
      return { response, body: await response.json() };
    }, timing);
    if (!text(body.request_id) || body.status !== "accepted") {
      throw new Error("Steward acceptance response was invalid; request outcome is uncertain");
    }
    return { request_id: body.request_id, status: body.status };
  }

  return { TYPES, MAX_RUNS, MAX_ACKNOWLEDGEMENTS, DEFAULT_STALE_MS, DEFAULT_ACK_MS,
    DEFAULT_FETCH_MS, validate, project, declared, canRun,
    canRunAuthoritatively, runDisabled, boundaryAvailability, telemetryAvailability,
    createAcknowledgements, fetchDeclarations, runNow, parseCursor: cursor, offsetAfter,
    requestOptions, requireConfig, withDeadline };
});
