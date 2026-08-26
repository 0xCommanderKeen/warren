"use strict";

(function (root, factory) {
  const transport = typeof module === "object" && module.exports
    ? require("./routine-ledger.js") : root.BurrowRoutines;
  const api = factory(transport);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowJobs = api;
})(typeof globalThis === "object" ? globalThis : this, function (transport) {
  const retention = typeof module === "object" && module.exports
    ? require("./retention-policy.js") : BurrowRetentionPolicy;
  const TYPES = new Set(["task_posted", "task_claimed", "task_done", "task_failed"]);
  const MAX_TASKS = retention.tasks;
  const MAX_DIAGNOSTICS = 40;
  const DONE_RECENCY_MS = 15 * 60 * 1000;
  const DEFAULT_ACK_MS = 15 * 1000;
  const MAX_ACKNOWLEDGEMENTS = 40;

  const text = value => typeof value === "string" && value.trim() ? value.trim() : null;
  const reopens = event => event && event.type === "task_failed" &&
    text(event.payload && event.payload.reason) === "lease_expired";
  const terminal = event => event && (event.type === "task_done" ||
    (event.type === "task_failed" && !reopens(event)));
  function eventIdentity(event) {
    return [event.type, event.ts, event.agent_id, event.project,
      event.payload && event.payload.task_id, event.payload && event.payload.title,
      event.payload && event.payload.claimant,
      JSON.stringify(event.payload && event.payload.required_skills || []),
      JSON.stringify(event.payload && event.payload.artifacts || []),
      event.payload && event.payload.reason,
      event.payload && event.payload.parent_task_id]
      .map(value => String(value ?? "")).join("\0");
  }
  function compareText(left, right) {
    const a = [...String(left)].map(value => value.codePointAt(0));
    const b = [...String(right)].map(value => value.codePointAt(0));
    for (let index = 0; index < Math.min(a.length, b.length); index++) {
      if (a[index] !== b[index]) return a[index] > b[index] ? 1 : -1;
    }
    return a.length === b.length ? 0 : a.length > b.length ? 1 : -1;
  }
  function tieRank(event) {
    // Steward's ordering-sensitive same-millisecond hand-off is its lease
    // sweep followed immediately by a successful re-claim. A total semantic
    // order makes that transition deterministic without retaining an
    // unbounded set of every identity observed at that millisecond.
    if (event.type === "task_posted") return 0;
    if (reopens(event)) return 1;
    if (event.type === "task_claimed") return 2;
    if (event.type === "task_failed") return 3;
    return 4; // task_done
  }
  const valid = (event, validateEvent) => typeof validateEvent === "function" &&
    validateEvent(event) === null && TYPES.has(event.type);
  function later(candidate, current) {
    const candidateTime = Date.parse(candidate.ts), currentTime = Date.parse(current.ts);
    if (candidateTime !== currentTime) return candidateTime > currentTime;
    const candidateRank = tieRank(candidate), currentRank = tieRank(current);
    if (candidateRank !== currentRank) return candidateRank > currentRank;
    // Same-kind conflicts use one stable final comparison. Exact replay
    // compares equal; distinct events converge regardless of batching, reset
    // replay, or duplicate arrival, with constant memory.
    return compareText(eventIdentity(candidate), eventIdentity(current)) > 0;
  }
  function rememberLatest(task, slot, event) {
    const current = task[slot];
    if (!current || later(event, current)) task[slot] = event;
  }
  function createState() {
    return { tasks: new Map(), diagnostics: [], malformed: 0, capacityDropped: 0 };
  }
  function diagnose(state, event, reason) {
    state.malformed += 1;
    state.diagnostics.push({ type: event && event.type || "task", reason });
    if (state.diagnostics.length > MAX_DIAGNOSTICS) state.diagnostics.shift();
  }
  function enforceCapacity(state) {
    if (state.tasks.size <= MAX_TASKS) return;
    const ordered = [...state.tasks.values()].sort((a, b) => {
      const aDone = terminal(a.transition || a.posted) ? 1 : 0;
      const bDone = terminal(b.transition || b.posted) ? 1 : 0;
      if (aDone !== bDone) return aDone - bDone;
      const aTime = Date.parse((a.transition || a.posted).ts);
      const bTime = Date.parse((b.transition || b.posted).ts);
      return bTime - aTime || compareText(b.id, a.id);
    });
    for (const task of ordered.slice(MAX_TASKS)) state.tasks.delete(task.id);
    state.capacityDropped += ordered.length - MAX_TASKS;
  }
  function foldTrusted(state, batch, options = {}) {
    if (options.reset) {
      state.tasks.clear(); state.diagnostics.length = 0;
      state.malformed = 0; state.capacityDropped = 0;
    }
    for (const rejection of options.rejections || []) {
      diagnose(state, rejection, rejection.reason || "malformed task event skipped");
    }
    for (const event of batch || []) {
      if (!TYPES.has(event.type)) continue;
      const id = event.payload.task_id.trim();
      let task = state.tasks.get(id);
      if (!task) {
        task = { id, posted: null, transition: null };
        state.tasks.set(id, task);
      }
      if (event.type === "task_posted") rememberLatest(task, "posted", event);
      rememberLatest(task, "transition", event);
      // Capacity is an ingestion rule, not a batch-final presentation rule.
      // Applying it per event keeps one-event SSE folds identical to grouped
      // bootstrap/reset replay and to serve.py's rotation projection.
      enforceCapacity(state);
    }
    return state;
  }
  function foldValidated(state, batch, options = {}) {
    if (typeof options.isValidatedBatch !== "function" ||
        !options.isValidatedBatch(batch)) {
      throw new TypeError("foldValidated requires the shared strict validated batch");
    }
    return foldTrusted(state, batch, options);
  }
  function fold(state, batch, options = {}) {
    const validated = [], rejections = [];
    for (let event of batch || []) {
      if (typeof event === "string") {
        try { event = JSON.parse(event); } catch { continue; }
      }
      if (!event || !TYPES.has(event.type)) continue;
      const reason = typeof options.validateEvent === "function" ?
        options.validateEvent(event) : "shared strict validator is required";
      if (reason !== null) {
        rejections.push({ type: event.type,
          reason: reason || "malformed task event skipped" });
      } else validated.push(event);
    }
    return foldTrusted(state, validated, { ...options,
      rejections: [...(options.rejections || []), ...rejections] });
  }
  function rows(state, now = Date.now(), doneRecencyMs = DONE_RECENCY_MS) {
    if (Array.isArray(state.authoritativeRows)) return state.authoritativeRows.map(task => ({
      id: task.id, title: task.title, required_skills: task.required_skills,
      posted_at: null, updated_at: Date.parse(task.updated_at), state: task.state,
      claimant: task.claimant, previous_claimant: null, reason: task.reason || null,
    }));
    const result = [];
    for (const task of state.tasks.values()) {
      const event = task.transition || task.posted;
      if (!event) continue;
      const closedAt = terminal(event) ? Date.parse(event.ts) : null;
      if (closedAt !== null && now - closedAt > doneRecencyMs) continue;
      const posted = task.posted;
      result.push({
        id: task.id,
        title: posted ? posted.payload.title.trim() : event.payload.title.trim(),
        required_skills: posted ? posted.payload.required_skills.slice() : null,
        // A claim or terminal transition does not prove when a task was
        // posted. Keep that absence explicit instead of giving the transition
        // a misleading "posted" age.
        posted_at: posted ? Date.parse(posted.ts) : null,
        updated_at: Date.parse(event.ts),
        state: event.type === "task_done" ? "done" :
          event.type === "task_failed" && !reopens(event) ? "failed" :
          event.type === "task_claimed" ? "claimed" : "open",
        claimant: event.type === "task_posted" || reopens(event) ? null : event.payload.claimant.trim(),
        previous_claimant: reopens(event) ? event.payload.claimant.trim() : null,
        reason: event.type === "task_failed" ? event.payload.reason.trim() : null,
      });
    }
    const terminalRank = row => ["done", "failed"].includes(row.state) ? 1 : 0;
    result.sort((a, b) => {
      const terminalOrder = terminalRank(a) - terminalRank(b);
      if (terminalOrder) return terminalOrder;
      const aPosted = a.posted_at !== null, bPosted = b.posted_at !== null;
      // Known post chronology is authoritative. Transition-only records are
      // consistently placed after it and use their explicitly labelled update
      // time only to order one unknown-post record against another.
      if (aPosted !== bPosted) return aPosted ? -1 : 1;
      if (aPosted && a.posted_at !== b.posted_at) return b.posted_at - a.posted_at;
      if (!aPosted && a.updated_at !== b.updated_at) return b.updated_at - a.updated_at;
      return b.updated_at - a.updated_at || compareText(b.id, a.id);
    });
    return result.slice(0, MAX_TASKS);
  }

  function createAcknowledgements(options = {}) {
    const timeoutMs = options.timeoutMs ?? DEFAULT_ACK_MS;
    const maxEntries = options.maxEntries ?? MAX_ACKNOWLEDGEMENTS;
    const schedule = options.setTimeout || setTimeout;
    const cancel = options.clearTimeout || clearTimeout;
    const clock = options.now || Date.now;
    const onChange = options.onChange || (() => {});
    const entries = new Map();
    let sequence = 0;
    const active = item => ["requesting", "pending"].includes(item.state);
    const blocksSubmission = item => item.httpPending || active(item) ||
      ["ambiguous", "indeterminate"].includes(item.state) ||
      (item.state === "timeout" && (item.task_id || item.acceptanceKnown));
    function finish(item, state, message) {
      item.state = state; item.message = message; item.updatedAt = clock();
      if (item.timer !== undefined) cancel(item.timer);
      delete item.timer;
      onChange(item);
    }
    function expire(item, at = clock()) {
      if (active(item) && at >= item.deadlineAt) {
        item.deadlineElapsedAt = at;
        finish(item, "timeout",
          "No matching task_posted event arrived before the acknowledgement timeout.");
      }
      return item;
    }
    function acknowledge(item, event) {
      item.event = event;
      finish(item, "acknowledged", item.deadlineElapsedAt === undefined ?
        "Posted — confirmed by the matching task_posted event." :
        "Posted — confirmed by the matching task_posted event after the acknowledgement deadline had elapsed.");
    }
    function request(boundaryCursor) {
      if ([...entries.values()].some(blocksSubmission)) return { ok: false,
        message: "A previous post is still unresolved; no duplicate request was sent." };
      if (entries.size >= maxEntries &&
          ![...entries.values()].some(item => !active(item))) {
        return { ok: false, message: "post tracking is full; no request was sent" };
      }
      for (const [key, item] of entries) {
        if (!active(item)) { entries.delete(key); break; }
      }
      const boundary = transport.parseCursor(boundaryCursor);
      if (!boundary) return { ok: false,
        message: "Post unavailable: exact telemetry cursor is not available; no request was sent" };
      const id = `post-${++sequence}`;
      const requestedAt = clock();
      const item = { id, state: "requesting", task_id: null, boundary, candidates: new Map(),
        requestedAt, deadlineAt: requestedAt + timeoutMs, updatedAt: requestedAt,
        httpPending: true };
      item.timer = schedule(() => expire(item, item.deadlineAt),
        Math.max(0, item.deadlineAt - clock()));
      entries.set(id, item);
      onChange(item);
      return { ok: true, id, item };
    }
    function accepted(id, taskId, requestId) {
      const item = entries.get(id);
      if (item) expire(item);
      if (!item) return null;
      item.httpPending = false;
      if (!text(taskId) || !text(requestId)) {
        finish(item, "ambiguous", "Steward returned an invalid acceptance; the post outcome is ambiguous.");
        return item;
      }
      item.task_id = taskId.trim(); item.request_id = requestId.trim(); item.acceptanceKnown = true;
      const candidate = item.candidates.get(item.task_id);
      if (candidate) {
        acknowledge(item, candidate);
      } else if (active(item)) {
        item.state = "pending"; item.updatedAt = clock(); onChange(item);
      } else {
        // A response after timeout, reset ambiguity, or bounded evidence loss
        // supplies correlation metadata, not timely proof. Preserve the prior
        // state until an exact post-boundary event supplies that proof.
        onChange(item);
      }
      return item;
    }
    function failed(id, message, definitive, taskId) {
      const item = entries.get(id);
      if (!item) return null;
      expire(item);
      item.httpPending = false;
      if (!definitive && text(taskId)) item.task_id = taskId.trim();
      const candidate = item.task_id && item.candidates.get(item.task_id);
      if (candidate) {
        acknowledge(item, candidate);
        return item;
      }
      if (!active(item)) {
        if (definitive && ["timeout", "ambiguous", "indeterminate"].includes(item.state)) {
          finish(item, "failed", message);
        } else if (!definitive && item.state === "timeout") {
          finish(item, "ambiguous", message);
        } else {
          onChange(item);
        }
        return item;
      }
      finish(item, definitive ? "failed" : "ambiguous", message);
      return item;
    }
    function observe(observation, validateEvent) {
      const parsed = transport.parseCursor(observation && observation.cursor);
      for (const item of entries.values()) {
        expire(item);
        const reconcilable = item.httpPending || active(item) ||
          (["ambiguous", "timeout", "indeterminate"].includes(item.state) &&
            Boolean(item.task_id));
        if (!reconcilable) continue;
        if (observation && observation.reset) {
          // A reset baseline cannot prove a post after the old request cursor.
          // Discard staged old-generation evidence even if the HTTP response
          // has not yet supplied the identity that would select it.
          item.candidates.clear();
          if (active(item)) finish(item, "ambiguous",
            "Telemetry reset while awaiting acknowledgement; the post outcome is ambiguous.");
          continue;
        }
        if (!parsed || parsed.namespace !== item.boundary.namespace ||
            !transport.offsetAfter(parsed.offset, item.boundary.offset)) continue;
        for (const event of observation.events || []) {
          if (event.type !== "task_posted" || !valid(event, validateEvent)) continue;
          if (item.task_id) {
            if (item.task_id === event.payload.task_id) {
              acknowledge(item, event);
              break;
            }
            continue;
          }
          item.candidates.set(event.payload.task_id, event);
          if (item.candidates.size > MAX_TASKS) {
            // Before Steward's response supplies task_id, no candidate is
            // safely distinguishable from the requested post. Evicting one
            // would turn lost proof into a false timeout, so stop the timer and
            // state the bounded evidence loss explicitly.
            item.candidates.delete(item.candidates.keys().next().value);
            finish(item, "indeterminate",
              "Too many task posts arrived before Steward identified this request; exact acknowledgement is indeterminate and retry is unavailable.");
            break;
          }
        }
      }
    }
    return { request, accepted, failed, observe, get: id => entries.get(id) || null,
      latest: () => [...entries.values()].at(-1) || null, size: () => entries.size,
      hasActive: () => [...entries.values()].some(item => item.httpPending || active(item)),
      blocksSubmission: () => [...entries.values()].some(blocksSubmission) };
  }

  function normalizeSkills(value) {
    const result = [];
    for (const item of String(value || "").split(",")) {
      const skill = item.trim();
      if (skill && !result.includes(skill)) result.push(skill);
    }
    return result;
  }
  async function postJob(config, job, fetchImpl = fetch, timing) {
    const base = transport.requireConfig(config);
    const title = text(job && job.title);
    if (!title) { const error = new Error("A job title is required."); error.definitive = true; throw error; }
    const body = { title, detail: text(job.detail) || "", required_skills: normalizeSkills(job.required_skills) };
    const { response, payload } = await transport.withDeadline(async signal => {
      const response = await fetchImpl(`${base}/jobs`, {
        ...transport.requestOptions(config.token, "POST"), signal,
        headers: { ...transport.requestOptions(config.token, "POST").headers,
          "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (Number.isInteger(response && response.status) && response.status !== 202) {
        const definitive = response.status === 401 || response.status === 422;
        const error = new Error(definitive ?
          `Steward refused the job (${response.status}).` :
          `Steward returned ${response.status} after the job may have been recorded; the outcome is ambiguous.`);
        error.definitive = definitive;
        error.authRejected = definitive && response.status === 401;
        if (!definitive && typeof response.json === "function") {
          try {
            const refusal = await response.json();
            if (refusal && typeof refusal === "object" && !Array.isArray(refusal) &&
                text(refusal.task_id)) error.taskId = refusal.task_id.trim();
          } catch { /* the status already carries the safe ambiguity classification */ }
        }
        throw error;
      }
      if (!response || response.status !== 202 || typeof response.json !== "function")
        throw new Error("Steward returned an invalid response; the post outcome is ambiguous.");
      let payload;
      try { payload = await response.json(); }
      catch { throw new Error("Steward returned invalid JSON; the post outcome is ambiguous."); }
      return { response, payload };
    }, timing);
    if (!payload || typeof payload !== "object" || Array.isArray(payload) ||
        !text(payload.task_id) || !text(payload.request_id) || payload.status !== "accepted") {
      const error = new Error("Steward acceptance was invalid; the post outcome is ambiguous.");
      error.definitive = false;
      if (payload && typeof payload === "object" && !Array.isArray(payload) &&
          text(payload.task_id)) error.taskId = payload.task_id.trim();
      throw error;
    }
    return { task_id: payload.task_id.trim(), request_id: payload.request_id.trim(), status: payload.status };
  }

  return { TYPES, MAX_TASKS, DONE_RECENCY_MS, DEFAULT_ACK_MS, MAX_ACKNOWLEDGEMENTS,
    createState, fold, foldValidated, rows, valid, createAcknowledgements,
    normalizeSkills, postJob };
});
