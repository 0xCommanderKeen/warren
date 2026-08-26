"use strict";

/* Structured approval projection and direct Steward write adapter. The event
 * log remains the only authority for what resolved: an HTTP response can at
 * most move a request into "awaiting evidence". */
(function (root, factory) {
  const transport = typeof module === "object" && module.exports
    ? require("./routine-ledger.js") : root.BurrowRoutines;
  const typedJSON = typeof module === "object" && module.exports
    ? require("./typed-json.js") : root.BurrowTypedJSON;
  const api = factory(transport, typedJSON);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowApprovals = api;
})(typeof globalThis === "object" ? globalThis : this, function (transport, typedJSON) {
  const TYPES = new Set(["needs_human", "needs_human_resolved"]);
  const DECISIONS = new Set(["approve", "deny", "edit"]);
  const ACTION = /^[a-z0-9][a-z0-9_-]*$/;
  const MAX_REQUESTS = 40;
  const MAX_DIAGNOSTICS = 40;
  const DEFAULT_ACK_MS = 15 * 1000;
  const MAX_ACKNOWLEDGEMENTS = 40;
  const MAX_ACK_EVIDENCE = 40;
  const MAX_CONFIRMATIONS = 5;

  // Wire identities are exact strings. Whitespace-only values are invalid, but
  // surrounding whitespace in an otherwise valid protocol string is data and
  // must not be silently rewritten before correlation.
  const exactText = value => typeof value === "string" && value.trim() ? value : null;
  const trimmedText = value => typeof value === "string" && value.trim() ? value.trim() : null;
  const plainObject = value => value !== null && typeof value === "object" &&
    !Array.isArray(value);
  const canonicalJson = value => typedJSON.identity(value);
  function identity(event) {
    return canonicalJson({ type: event && event.type, ts: event && event.ts,
      source: event && event.source, agent_id: event && event.agent_id,
      project: event && event.project, payload: event && event.payload || {} });
  }
  function exactSame(left, right) { return identity(left) === identity(right); }
  function resolutionIdentity(event, shape = null) {
    const payload = event && event.payload || {};
    const requestId = shape && shape.request_id || exactText(payload.request_id);
    const action = shape && shape.action || exactText(payload.action);
    const agentId = exactText(event && event.agent_id);
    const project = exactText(event && event.project);
    if (!requestId || !action || !agentId || !project) return null;
    return canonicalJson([requestId, agentId, project, action]);
  }
  function lifecycleIdentity(event, shape = null) {
    if (!event || event.type !== "needs_human") return null;
    const payload = event.payload || {};
    const classified = shape || classify(event);
    if (!classified || classified.kind !== "structured") return null;
    return canonicalJson({
      request_id: classified.request_id,
      agent_id: event.agent_id,
      project: event.project,
      action: classified.action,
      detail: classified.detail,
      options: classified.options,
      message: payload.message,
      expires_at: { present: Object.hasOwn(payload, "expires_at"),
        value: Object.hasOwn(payload, "expires_at") ? payload.expires_at : null },
    });
  }
  function sameLifecycle(record, event, shape = null) {
    return Boolean(record && record.resolutionIdentity &&
      record.resolutionIdentity === resolutionIdentity(event, shape));
  }

  /* A legacy message with no approval fields is plain. Once an emitter tries
   * any structured field, all four are required; broken attempts remain plain
   * knocks and acquire a diagnostic rather than dead controls. */
  function classify(event) {
    if (!event || event.type !== "needs_human") return { kind: "other" };
    const payload = event.payload || {};
    const fields = ["action", "detail", "options", "request_id"];
    if (!fields.some(field => Object.hasOwn(payload, field))) return { kind: "plain" };
    const action = exactText(payload.action), requestId = exactText(payload.request_id);
    if (!action || !ACTION.test(action)) return { kind: "malformed",
      reason: "structured knock action must be a lowercase action slug" };
    if (!requestId) return { kind: "malformed", reason: "structured knock has no request_id" };
    if (!Object.hasOwn(payload, "detail") ||
        !(payload.detail === null || plainObject(payload.detail))) {
      return { kind: "malformed", reason: "structured knock detail must be an object or null" };
    }
    if (!Array.isArray(payload.options) || !payload.options.length) {
      return { kind: "malformed", reason: "structured knock options must be non-empty" };
    }
    const options = [];
    for (const option of payload.options) {
      if (!exactText(option) || !DECISIONS.has(option)) {
        return { kind: "malformed",
          reason: "structured knock options must be approve, deny, or edit values" };
      }
      options.push(option);
    }
    return { kind: "structured", request_id: requestId, action,
      detail: payload.detail, options };
  }

  function createState(options = {}) {
    const state = { requests: new Map(), diagnostics: [], malformed: 0,
      capacityDropped: 0, sequence: "0", ordinals: new WeakMap(), classify };
    const maxRequests = Object.hasOwn(options, "maxRequests") ? options.maxRequests : MAX_REQUESTS;
    if (maxRequests !== null && (!Number.isInteger(maxRequests) || maxRequests < 0)) {
      throw new TypeError("approval maxRequests must be a non-negative integer or null");
    }
    // BigInt prevents precision loss, while only canonical decimal ordinals
    // escape into projection records. Keeping the counter non-enumerable makes
    // snapshots and villager data safe for ordinary JSON tooling.
    Object.defineProperty(state, "appendSequence", { value: 0n, writable: true });
    Object.defineProperty(state, "maxRequests", { value: maxRequests });
    state.recordForEvent = event => recordForEvent(state, event);
    state.ordinalForEvent = event => ordinalForEvent(state, event);
    return state;
  }
  function diagnose(state, reason, event, key = null) {
    const diagnosticKey = key || `${reason}\0${identity(event || {})}`;
    if (state.diagnostics.some(item => item.key === diagnosticKey)) return;
    state.diagnostics.push({ key: diagnosticKey, reason,
      type: event && event.type || "approval", request_id: exactText(event && event.payload &&
        event.payload.request_id), agent_id: exactText(event && event.agent_id) });
    if (state.diagnostics.length > MAX_DIAGNOSTICS) state.diagnostics.shift();
  }
  function applyResolution(state, record, event, ordinal) {
    if (!sameLifecycle(record, event)) {
      diagnose(state, "decision identity does not match its request and was ignored", event,
        `mismatch\0${record.id}\0${identity(event)}`);
      return;
    }
    if (record.resolution && exactSame(record.resolution, event)) {
      diagnose(state, "exact decision replay ignored; request remains resolved once", event,
        `decision-replay\0${record.lifecycle}\0${identity(event)}`);
      return;
    }
    if (record.resolution) {
      diagnose(state, "request was already resolved; later or conflicting decision ignored", event,
        `already-resolved\0${record.lifecycle}\0${identity(event)}`);
      return;
    }
    // The log is authoritative. Producer timestamps are descriptive data and
    // cannot reorder decisions: the first matching append closes the request.
    record.resolution = event;
    record.resolutionOrdinal = ordinal;
    record.sequence = ordinal;
  }
  /* Append position is the approval projection's ordering authority. Keep its
   * wire-safe representation and comparison here so every consumer agrees on
   * huge positions without passing through Number. Invalid/missing values
   * deterministically precede the first real append at the canonical zero. */
  function normalizeOrdinal(value) {
    if (typeof value === "bigint") return value >= 0n ? value.toString() : "0";
    return typeof value === "string" && /^(?:0|[1-9]\d*)$/.test(value) ? value : "0";
  }
  function compareOrdinal(left, right) {
    const a = normalizeOrdinal(left), b = normalizeOrdinal(right);
    return a.length === b.length ? (a === b ? 0 : a > b ? 1 : -1) :
      a.length > b.length ? 1 : -1;
  }
  function enforceCapacity(state) {
    if (state.maxRequests === null || state.requests.size <= state.maxRequests) return;
    const ranked = [...state.requests.values()].sort((a, b) => {
      const aPending = a.resolution ? 0 : 1, bPending = b.resolution ? 0 : 1;
      if (aPending !== bPending) return bPending - aPending;
      return compareOrdinal(b.sequence, a.sequence);
    });
    for (const record of ranked.slice(state.maxRequests)) state.requests.delete(record.id);
    state.capacityDropped += ranked.length - state.maxRequests;
  }
  function foldTrusted(state, batch, options = {}) {
    if (options.reset) {
      state.requests.clear(); state.diagnostics.length = 0;
      state.malformed = 0; state.capacityDropped = 0; state.sequence = "0";
      state.appendSequence = 0n;
      state.ordinals = new WeakMap();
    }
    const ordinalBatch = options.ordinalBatch || null;
    const ordinalForEvent = options.ordinalForEvent;
    if (ordinalBatch) {
      for (const event of ordinalBatch) {
        const ordinal = normalizeOrdinal(ordinalForEvent(event));
        if (ordinal !== "0") state.ordinals.set(event, ordinal);
        if (event.type === "needs_human") {
          const shape = classify(event);
          if (shape.kind === "malformed") {
            state.malformed += 1;
            diagnose(state, shape.reason, event);
          }
        }
      }
      const sequence = normalizeOrdinal(options.sequence);
      if (compareOrdinal(sequence, state.sequence) > 0) {
        state.sequence = sequence;
        state.appendSequence = BigInt(sequence);
      }
    }
    for (const rejection of options.rejections || []) {
      state.malformed += 1;
      diagnose(state, rejection.reason || "malformed approval resolution skipped", rejection);
    }
    for (const event of batch || []) {
      let ordinal = ordinalBatch ? state.ordinals.get(event) : null;
      if (!ordinal) {
        state.appendSequence += 1n;
        ordinal = state.appendSequence.toString();
        state.sequence = ordinal;
        state.ordinals.set(event, ordinal);
      }
      if (!TYPES.has(event.type)) continue;
      if (event.type === "needs_human") {
        const shape = classify(event);
        if (shape.kind === "malformed") {
          if (!ordinalBatch) {
            state.malformed += 1; diagnose(state, shape.reason, event);
          }
          continue;
        }
        if (shape.kind !== "structured") continue;
        let record = state.requests.get(shape.request_id);
        if (!record) {
          record = { id: shape.request_id, knock: event, shape, resolution: null,
            lifecycle: lifecycleIdentity(event, shape),
            resolutionIdentity: resolutionIdentity(event, shape), collided: false,
            knockOrdinal: ordinal, resolutionOrdinal: null, sequence: ordinal };
          state.requests.set(record.id, record);
        } else if (record.lifecycle !== lifecycleIdentity(event, shape)) {
          // Steward owns request_id as a global primary key. If a combined or
          // corrupt log reuses it for any incompatible immutable request shape,
          // neither question is safe to send through the ID-only HTTP endpoint.
          record.collided = true;
          if (!record.collision) record.collision = event;
          diagnose(state, "request_id collision has an incompatible immutable approval request; decision controls disabled", event,
            `collision\0${record.id}\0${lifecycleIdentity(event, shape) || identity(event)}`);
        } else if (!exactSame(event, record.knock)) {
          diagnose(state, "duplicate structured knock ignored", event,
            `duplicate-knock\0${record.lifecycle}\0${identity(event)}`);
        }
        enforceCapacity(state);
        continue;
      }
      const id = exactText(event.payload && event.payload.request_id);
      const record = id && state.requests.get(id);
      const eventLifecycle = resolutionIdentity(event);
      if (!record || !sameLifecycle(record, event)) {
        diagnose(state, record ? "decision identity does not match its request and was ignored" :
          "decision for unknown request_id ignored", event,
          `orphan\0${eventLifecycle || id || ""}\0${identity(event)}`);
        continue;
      }
      if (record.collided) {
        diagnose(state, "decision for collided request_id ignored", event,
          `collided-decision\0${record.id}\0${identity(event)}`);
        continue;
      }
      applyResolution(state, record, event, ordinal);
    }
    return state;
  }
  function foldValidated(state, batch, options = {}) {
    if (typeof options.isValidatedBatch !== "function" ||
        !options.isValidatedBatch(batch)) {
      throw new TypeError("foldValidated requires the shared strict validated batch");
    }
    if (options.ordinalBatch && (!options.isValidatedBatch(options.ordinalBatch) ||
        typeof options.ordinalForEvent !== "function")) {
      throw new TypeError("approval ordinal authority requires a shared validated batch and provider");
    }
    return foldTrusted(state, batch, options);
  }
  function pendingForAgent(state, agentId) {
    return [...state.requests.values()].filter(record =>
      !record.resolution && !record.collided && record.knock.agent_id === agentId)
      .sort((a, b) => compareOrdinal(b.knockOrdinal, a.knockOrdinal));
  }
  function recentConfirmations(state, agentId = null) {
    if (!state || !(state.requests instanceof Map)) return [];
    return [...state.requests.values()].filter(record =>
      record && record.resolution && !record.collided && record.knock &&
      (agentId === null || record.knock.agent_id === agentId))
      .sort((a, b) => compareOrdinal(b.resolutionOrdinal, a.resolutionOrdinal))
      .slice(0, MAX_CONFIRMATIONS)
      .map(record => ({ request_id: record.id, record,
        resolution: record.resolution }));
  }
  function recordFor(state, requestId) { return state.requests.get(requestId) || null; }
  function recordForEvent(state, event) {
    if (!state || !(state.requests instanceof Map) || !event) return null;
    const shape = classify(event);
    if (shape.kind !== "structured") return null;
    const record = state.requests.get(shape.request_id) || null;
    return sameLifecycle(record, event, shape) ? record : null;
  }
  function ordinalForEvent(state, event) {
    return event && state && state.ordinals instanceof WeakMap ?
      state.ordinals.get(event) || null : null;
  }

  /* Build the approval consumer's bounded grouped-response window. The normal
   * newest raw tail remains the default. For every agent with a retained
   * journal observation, reserve enough slots for each exact lifecycle selected
   * by this module's own fold, whether the request precedes or follows the
   * journal. Pending requests override later ordinary evidence; terminal and
   * collided lifecycles must remain whole so reset cannot resurrect them. */
  function lifecycleWindow(batch, limit, journalRecords = [], options = {}) {
    if (!Array.isArray(batch) || !Number.isInteger(limit) || limit < 0) {
      throw new TypeError("lifecycleWindow requires a validated array and a non-negative limit");
    }
    if (typeof options.isValidatedBatch !== "function" || !options.isValidatedBatch(batch) ||
        typeof options.validatedSelection !== "function") {
      throw new TypeError("lifecycleWindow requires the shared strict validated batch");
    }
    if (limit === 0) return options.validatedSelection(batch, []);
    const temporary = createState();
    foldTrusted(temporary, batch);
    const positions = new Map(batch.map((event, index) => [event, index]));
    const journalAgents = new Set();
    for (const record of journalRecords || []) {
      const event = record && record.event;
      if (!event || positions.get(event) === undefined) continue;
      journalAgents.add(event.agent_id);
    }
    const eligible = [...temporary.requests.values()].filter(record => {
      return positions.get(record.knock) !== undefined &&
        journalAgents.has(record.knock.agent_id);
    }).sort((left, right) => compareOrdinal(right.sequence, left.sequence));
    const required = new Set();
    for (const record of eligible) {
      const lifecycle = [record.knock, record.resolution, record.collision].filter(Boolean);
      const additions = lifecycle.filter(event => !required.has(event));
      if (required.size + additions.length > limit) continue;
      for (const event of additions) required.add(event);
    }
    const tailStart = Math.max(0, batch.length - limit);
    const selected = new Set(batch.slice(tailStart));
    for (const event of required) selected.add(event);
    let ordered = batch.filter(event => selected.has(event));
    if (ordered.length > limit) {
      // Required lifecycle facts displace the oldest ordinary tail facts first.
      // If pressure is entirely approval evidence, preserve required facts and
      // retain the newest remaining evidence within the same global bound.
      const removable = ordered.filter(event => !required.has(event));
      const remove = new Set(removable.slice(0, ordered.length - limit));
      ordered = ordered.filter(event => !remove.has(event));
    }
    if (ordered.length > limit) ordered = ordered.slice(ordered.length - limit);
    return options.validatedSelection(batch, ordered);
  }

  function createAcknowledgements(options = {}) {
    const timeoutMs = options.timeoutMs ?? DEFAULT_ACK_MS;
    const schedule = options.setTimeout || setTimeout;
    const cancel = options.clearTimeout || clearTimeout;
    const clock = options.now || Date.now;
    const onChange = options.onChange || (() => {});
    const maxEntries = options.maxEntries ?? MAX_ACKNOWLEDGEMENTS;
    const entries = new Map();
    const active = item => ["requesting", "pending"].includes(item.state);
    const blocks = item => active(item) || ["ambiguous", "timeout", "indeterminate"].includes(item.state);
    function finish(item, state, message) {
      item.state = state; item.message = message; item.updatedAt = clock();
      if (item.timer !== undefined) cancel(item.timer);
      delete item.timer; onChange(item); return item;
    }
    function expire(item, at = clock()) {
      if (active(item) && at >= item.deadlineAt) finish(item, "timeout",
        "No exact needs_human_resolved event arrived before the acknowledgement timeout; the knock remains open and retry is disabled to prevent a duplicate decision.");
      return item;
    }
    function request(requestId, decision, edit, boundaryCursor, expected = {}) {
      const id = exactText(requestId);
      if (!id || !DECISIONS.has(decision)) return { ok: false,
        message: "This approval option is invalid; no request was sent." };
      const exactExpected = { agent_id: exactText(expected.agent_id),
        project: exactText(expected.project), action: exactText(expected.action),
        lifecycle: exactText(expected.lifecycle) };
      if (!exactExpected.agent_id || !exactExpected.project || !exactExpected.action ||
          !exactExpected.lifecycle)
        return { ok: false,
          message: "Approval unavailable: exact immutable request identity is missing; no request was sent." };
      const current = entries.get(id);
      if (current && blocks(expire(current))) return { ok: false,
        message: "This decision may already have been sent; retry is disabled until exact closing evidence arrives." };
      const boundary = transport.parseCursor(boundaryCursor);
      if (!boundary) return { ok: false,
        message: "Approval unavailable: exact telemetry cursor is not available; no request was sent." };
      if (!entries.has(id) && entries.size >= maxEntries) {
        const removable = [...entries.entries()].find(([, item]) => !blocks(expire(item)));
        if (removable) entries.delete(removable[0]);
        else return { ok: false,
          message: "Approval tracking is full of unresolved decisions; no request was sent." };
      }
      const now = clock();
      const item = { request_id: id, decision, edit, boundary, state: "requesting",
        expected: exactExpected, httpPending: true,
        requestedAt: now, updatedAt: now, deadlineAt: now + timeoutMs };
      item.timer = schedule(() => expire(item, item.deadlineAt),
        Math.max(0, item.deadlineAt - clock()));
      entries.set(id, item); onChange(item);
      return { ok: true, item };
    }
    function accepted(requestId, result) {
      const item = entries.get(requestId); if (!item) return null;
      expire(item); item.httpPending = false;
      if (item.state === "acknowledged") { onChange(item); return item; }
      if (result && result.replay === true) return finish(item, "indeterminate",
        "Steward says this request was already recorded, but a replay emits no new closing event. The knock remains until exact log evidence arrives; retry is disabled.");
      if (active(item)) { item.state = "pending"; item.updatedAt = clock(); onChange(item); }
      return item;
    }
    function failed(requestId, message, definitive) {
      const item = entries.get(requestId); if (!item) return null;
      expire(item); item.httpPending = false;
      if (!active(item)) {
        if (definitive && ["timeout", "ambiguous", "indeterminate"].includes(item.state)) {
          return finish(item, "failed", message);
        }
        onChange(item); return item;
      }
      return finish(item, definitive ? "failed" : "ambiguous", message);
    }
    function exactLifecycleRecord(item, approvalState) {
      const record = approvalState && approvalState.requests instanceof Map ?
        approvalState.requests.get(item.request_id) : null;
      if (!record || record.collided ||
          record.knock.agent_id !== item.expected.agent_id ||
          record.knock.project !== item.expected.project ||
          record.shape.action !== item.expected.action ||
          record.lifecycle !== item.expected.lifecycle) return null;
      return record;
    }
    function authoritativeRecord(item, approvalState) {
      const record = exactLifecycleRecord(item, approvalState);
      return record && record.resolution ? record : null;
    }
    function observe(observation) {
      const parsed = transport.parseCursor(observation && observation.cursor);
      for (const item of entries.values()) {
        expire(item);
        if (observation && observation.reset) {
          if (item.state === "failed") continue;
          // A reset starts a new evidence generation. Rebase the boundary and
          // discard staged closes so replayed history cannot masquerade as a
          // response to this click. An already-confirmed close survives only
          // when the replay projects the exact same lifecycle and close bytes.
          item.generationCandidates = new Map();
          if (parsed) item.boundary = parsed;
          const lifecycle = exactLifecycleRecord(item, observation.approvalState);
          const replayed = lifecycle && lifecycle.resolution;
          if (item.confirmedFingerprint && replayed &&
              identity(replayed) === item.confirmedFingerprint) {
            item.event = replayed;
            finish(item, "acknowledged",
              `Decision ${replayed.payload.decision} — confirmed by the authoritative approval lifecycle after replay.`);
          } else {
            const detail = !lifecycle ? "the exact lifecycle is missing or collided" :
              !replayed ? "the exact lifecycle is pending" :
                "the replay selected a different closing decision";
            finish(item, active(item) ? "ambiguous" : "indeterminate",
              `Telemetry reset and ${detail}; retry is disabled until the exact authoritative close is observed again.`);
          }
          continue;
        }
        if (!parsed || parsed.namespace !== item.boundary.namespace ||
            !transport.offsetAfter(parsed.offset, item.boundary.offset)) continue;
        const record = authoritativeRecord(item, observation.approvalState);
        const event = record && record.resolution;
        const eventFingerprint = event && identity(event);
        for (const observed of observation.events || []) {
          if (eventFingerprint && observed.type === "needs_human_resolved" &&
              identity(observed) === eventFingerprint) {
            item.generationCandidates ||= new Map();
            if (item.generationCandidates.has(eventFingerprint) ||
                item.generationCandidates.size < MAX_ACK_EVIDENCE) {
              item.generationCandidates.set(eventFingerprint, observed);
            }
          }
        }
        const observedExactly = event && (item.generationCandidates || new Map())
          .has(identity(event));
        const preservesConfirmation = !item.confirmedFingerprint ||
          item.confirmedFingerprint === eventFingerprint;
        if (observedExactly && preservesConfirmation &&
            (item.state !== "acknowledged" || !item.event || !exactSame(item.event, event))) {
          item.event = event;
          item.confirmedFingerprint = eventFingerprint;
          finish(item, "acknowledged", `Decision ${event.payload.decision} — confirmed by the authoritative approval lifecycle.`);
        } else if (event && item.confirmedFingerprint &&
                   item.confirmedFingerprint !== eventFingerprint) {
          finish(item, "indeterminate",
            `The authoritative approval lifecycle selects a different decision ${event.payload.decision}; retry remains disabled until the previously confirmed close is authoritative again.`);
        } else if (!record && item.state === "acknowledged") {
          finish(item, "indeterminate",
            "The authoritative approval lifecycle no longer has this exact safe identity; retry remains disabled.");
        }
      }
    }
    return { request, accepted, failed, observe,
      get: requestId => { const item = entries.get(requestId); return item ? expire(item) : null; },
      blocks: requestId => { const item = entries.get(requestId); return Boolean(item && blocks(expire(item))); } };
  }

  async function decide(config, requestId, decision, editText, fetchImpl = fetch, timing) {
    const base = transport.requireConfig(config);
    const id = exactText(requestId);
    if (!id || !DECISIONS.has(decision)) {
      const error = new Error("Invalid approval request."); error.definitive = true; throw error;
    }
    const edit = decision === "edit" ? trimmedText(editText) : null;
    if (decision === "edit" && !edit) {
      const error = new Error("Write the edited detail before sending edit.");
      error.definitive = true; error.local = true; throw error;
    }
    const body = { decision };
    if (decision === "edit") body.edit = { note: edit };
    return transport.withDeadline(async signal => {
      let response;
      try {
        const auth = transport.requestOptions(config.token, "POST");
        response = await fetchImpl(`${base}/approvals/${encodeURIComponent(id)}`, {
          ...auth, signal, headers: { ...auth.headers, "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } catch (error) {
        if (error && error.definitive) throw error;
        const wrapped = new Error(error && error.message || "Steward could not be reached; the decision may have been delivered.");
        wrapped.definitive = false; throw wrapped;
      }
      const status = response && response.status;
      if (![200, 202].includes(status)) {
        let expired = false;
        if (status === 409) {
          try {
            const envelope = await response.json();
            const topKeys = plainObject(envelope) ? Object.keys(envelope).sort() : [];
            const detail = plainObject(envelope) ? envelope.detail : null;
            const detailKeys = plainObject(detail) ? Object.keys(detail).sort() : [];
            expired = topKeys.length === 1 && topKeys[0] === "detail" &&
              detailKeys.length === 2 && detailKeys[0] === "error" &&
              detailKeys[1] === "message" && detail.error === "approval_expired" &&
              typeof detail.message === "string";
          } catch {}
        }
        const definitive = [401, 404, 422].includes(status) || expired;
        const error = new Error(expired ? "Steward reports this approval expired and denies by default." :
          definitive ? `Steward refused the decision (${status}).` :
            `Steward returned ${status || "an invalid response"} after the decision may have been recorded.`);
        error.definitive = definitive; error.authRejected = status === 401; throw error;
      }
      let payload;
      try { payload = await response.json(); }
      catch {
        const error = new Error("Steward returned invalid JSON after the decision may have been recorded.");
        error.definitive = false; throw error;
      }
      if (!plainObject(payload) || exactText(payload.request_id) !== id ||
          payload.status !== "recorded" || !DECISIONS.has(payload.decision)) {
        const error = new Error("Steward returned an invalid decision receipt; delivery is ambiguous.");
        error.definitive = false; throw error;
      }
      return { request_id: id, decision: payload.decision, replay: status === 200 };
    }, timing);
  }

  return { TYPES, DECISIONS, MAX_REQUESTS, MAX_DIAGNOSTICS,
    DEFAULT_ACK_MS, MAX_ACKNOWLEDGEMENTS, MAX_ACK_EVIDENCE, MAX_CONFIRMATIONS, classify,
    lifecycleIdentity, resolutionIdentity, sameLifecycle,
    createState, foldValidated, pendingForAgent, recentConfirmations,
    recordFor, recordForEvent,
    ordinalForEvent, normalizeOrdinal, compareOrdinal,
    lifecycleWindow,
    createAcknowledgements, decide };
});
