"use strict";

/* The nursery is a direct browser-to-Steward request tracker. HTTP may prove
 * that Steward accepted a declaration, but only a later Burrow protocol event
 * proves that the resident woke. */
(function (root, factory) {
  const transport = typeof module === "object" && module.exports
    ? require("./routine-ledger.js") : root.BurrowRoutines;
  const sprites = typeof module === "object" && module.exports
    ? require("./sprites.js") : root.BurrowSprites;
  const api = factory(transport, sprites);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowNursery = api;
})(typeof globalThis === "object" ? globalThis : this, function (transport, sprites) {
  if (!sprites || !Array.isArray(sprites.CHARS) || !Object.isFrozen(sprites.CHARS)) {
    throw new Error("Burrow's frozen sprite authority must load before the nursery.");
  }
  const CHARS = sprites.CHARS;
  const RUNNERS = new Set(["claude", "codex"]);
  const ID = /^[a-z0-9][a-z0-9-]*$/;
  const ACCENT = /^#[0-9a-fA-F]{6}$/;
  const DEFAULT_WAIT_MS = 10 * 60 * 1000;
  const MAX_TRACKED = 8;
  const MAX_REMOTE_ERROR = 4096;
  const AMBIGUOUS_STATES = new Set(["ambiguous", "timeout", "unreachable", "cancelled"]);

  const text = value => typeof value === "string" && value.trim() ? value.trim() : null;
  function slug(name) {
    return String(name || "").trim().toLowerCase().normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "").replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
  }
  function lines(value) {
    return String(value || "").split(/\r?\n/).map(item => item.trim()).filter(Boolean);
  }
  function skills(value) {
    return [...new Set(String(value || "").split(/[\n,]/).map(item => item.trim()).filter(Boolean))];
  }
  function validate(draft) {
    const errors = {};
    const id = slug(draft && draft.name);
    if (!text(draft && draft.name)) errors.name = "Name is required.";
    else if (!ID.test(id)) errors.name = "Name must contain a letter or number that can form a resident ID.";
    if (!CHARS.includes(draft && draft.char)) errors.char = "Choose a character from Burrow's known sprite set.";
    if (!ACCENT.test(String(draft && draft.accent || ""))) errors.accent = "Accent must be a six-digit hex colour such as #4f7ea6.";
    if (!text(draft && draft.role)) errors.role = "Role is required.";
    if (!text(draft && draft.mission)) errors.mission = "Mission is required.";
    if (!lines(draft && draft.duties).length) errors.duties = "Add at least one duty, one per line.";
    if (!lines(draft && draft.rules).length) errors.rules = "Add at least one rule, one per line.";
    if (!text(draft && draft.escalation)) errors.escalation = "Escalation guidance is required.";
    if (!RUNNERS.has(draft && draft.runner)) errors.runner = "Choose Claude or Codex.";
    const badSkill = skills(draft && draft.skills).find(item => !ID.test(item));
    if (badSkill) errors.skills = `Skill ${badSkill} must be a lowercase slug.`;
    return { ok: !Object.keys(errors).length, errors, id };
  }
  function requestBody(draft) {
    const checked = validate(draft);
    if (!checked.ok) {
      const error = new Error("Fix the highlighted nursery fields before sending.");
      error.validation = checked; throw error;
    }
    return { id: checked.id, name: draft.name.trim(), char: draft.char,
      accent: draft.accent, role: draft.role.trim(), charter: {
        mission: draft.mission.trim(), duties: lines(draft.duties), rules: lines(draft.rules),
        escalation: draft.escalation.trim(),
      }, skills: skills(draft.skills), runner: { kind: draft.runner }, deploy: true };
  }
  function expectedRunnerSource(body) {
    return body.runner.kind === "claude" ? "claude-code" : "codex";
  }
  function expectedAgentId(body) {
    return `${expectedRunnerSource(body)}:${body.id}`;
  }
  function immutableBody(draft) {
    const body = requestBody(draft);
    const freeze = value => {
      if (!value || typeof value !== "object" || Object.isFrozen(value)) return value;
      for (const child of Object.values(value)) freeze(child);
      return Object.freeze(value);
    };
    return freeze(body);
  }
  function boundedRemote(value) {
    const message = typeof value === "string" ? value : "";
    if (message.length <= MAX_REMOTE_ERROR) return { text: message, truncated: false };
    return { text: message.slice(0, MAX_REMOTE_ERROR), truncated: true };
  }
  function responseError(body, status) {
    const detail = body && typeof body === "object" && !Array.isArray(body) ? body.detail : null;
    const record = detail && typeof detail === "object" && !Array.isArray(detail) ? detail : {};
    const exact = typeof detail === "string" ? detail : Array.isArray(detail) ? JSON.stringify(detail) :
      typeof record.message === "string" ? record.message : "";
    const remote = boundedRemote(exact);
    const code = boundedRemote(record.error);
    const definitive = [400, 401, 409, 422].includes(status);
    const responseLabel = status ? `HTTP ${status}` : "an invalid HTTP response";
    const uncertain = `Steward returned ${responseLabel}${remote.text ? `: ${remote.text}` : ""}. ` +
      "The resident may have been created; the outcome is unknown. " +
      "Retry is available only with the exact original declaration.";
    const error = new Error(definitive ?
      remote.text || `Steward rejected the resident (HTTP ${status}).` : uncertain);
    error.remote = Boolean(remote.text); error.truncated = remote.truncated || code.truncated;
    error.code = text(code.text); error.status = status;
    error.definitive = definitive;
    error.authRejected = status === 401;
    error.kind = status === 401 ? "authentication" : status === 409 ? "rejected" :
      status === 400 || status === 422 ? "validation" : "ambiguous";
    return error;
  }
  function transportFailure(error) {
    const kind = error && error.kind === "timeout" ? "timeout" :
      error && (error.kind === "cancelled" || error.aborted) ? "cancelled" : "unreachable";
    const messages = {
      timeout: "Steward timed out. The resident may have been created; retry is available only with the exact original declaration.",
      cancelled: "Steward request was cancelled. The resident may have been created; retry is available only with the exact original declaration.",
      unreachable: "Steward is unreachable. The request outcome is unknown; retry is available only with the exact original declaration.",
    };
    const ambiguous = new Error(messages[kind]);
    ambiguous.kind = kind; ambiguous.definitive = false; ambiguous.nurseryTransport = true;
    return ambiguous;
  }
  async function createResident(config, body, fetchImpl = fetch, timing = {}) {
    const base = transport.requireConfig(config);
    try {
      return await transport.withDeadline(async signal => {
        let response;
        try {
          const auth = transport.requestOptions(config.token, "POST");
          response = await fetchImpl(`${base}/residents`, { ...auth, signal,
            headers: { ...auth.headers, "Content-Type": "application/json" },
            body: JSON.stringify(body) });
        } catch (error) {
          if (signal.aborted) throw error;
          throw transportFailure(error);
        }
        let payload = null;
        try { payload = await response.json(); } catch {}
        if (!response || response.status !== 201) {
          throw responseError(payload, response && response.status || 0);
        }
        if (!payload || payload.status !== "accepted" || !text(payload.request_id) ||
            payload.id !== body.id || typeof payload.changed !== "boolean" ||
            !payload.declare || typeof payload.declare.written !== "boolean" ||
            !payload.register || typeof payload.register.ok !== "boolean" ||
            !Array.isArray(payload.register.problems) ||
            !payload.register.problems.every(problem => typeof problem === "string")) {
          const invalid = new Error("Steward returned an invalid acceptance. The resident may have been created; retry is available only with the exact original declaration.");
          invalid.kind = "ambiguous"; invalid.definitive = false; throw invalid;
        }
        const problems = payload.register.problems.slice();
        const fullMessage = payload.register.ok === false && problems.length ?
          `${text(payload.message) || "Steward's deployment or schedule check failed."} register.problems: ${JSON.stringify(problems)}` :
          text(payload.message) || "Steward accepted the resident.";
        const remote = boundedRemote(fullMessage);
        return { request_id: payload.request_id.trim(), resident_id: body.id,
          agent_id: expectedAgentId(body), name: body.name, changed: payload.changed,
          declaration_written: payload.declare.written, register_ok: payload.register.ok,
          register_problems: problems, message: remote.text, truncated: remote.truncated, body };
      }, timing);
    } catch (error) {
      if (error && (error.validation || error.definitive || error.kind === "ambiguous" ||
          error.nurseryTransport)) throw error;
      throw transportFailure(error);
    }
  }

  function createTracker(options = {}) {
    const now = options.now || Date.now, schedule = options.setTimeout || setTimeout;
    const cancel = options.clearTimeout || clearTimeout, onChange = options.onChange || (() => {});
    const waitMs = options.waitMs ?? DEFAULT_WAIT_MS, entries = new Map();
    let sequence = 0;
    function notify(item) { item.updatedAt = now(); onChange(item); return item; }
    function finish(item, state, message) {
      item.state = state; item.message = message; item.httpPending = false;
      if (item.timer !== undefined) cancel(item.timer); delete item.timer;
      return notify(item);
    }
    function itemBusy(item) {
      return Boolean(item && (item.httpPending || item.state === "pending"));
    }
    function canReconcileWake(item) {
      return Boolean(item && ["requesting", "pending", "silent", ...AMBIGUOUS_STATES]
        .includes(item.state));
    }
    function blocks() { return [...entries.values()].some(item =>
      ["requesting", "pending", ...AMBIGUOUS_STATES].includes(item.state)); }
    function busy() { return [...entries.values()].some(itemBusy); }
    function capacityVictims() {
      const count = Math.max(0, entries.size - MAX_TRACKED + 1);
      if (!count) return [];
      const victims = [...entries.values()].filter(item => !canReconcileWake(item)).slice(0, count);
      return victims.length === count ? victims : null;
    }
    function makeRoom(victims) {
      for (const victim of victims) {
        if (victim.timer !== undefined) cancel(victim.timer);
        entries.delete(victim.key);
      }
    }
    function begin(boundaryCursor, draft) {
      const checked = validate(draft);
      if (!checked.ok) return { ok: false, validation: checked };
      const victims = capacityVictims();
      if (!victims) return { ok: false, reason: "capacity",
        message: `Nursery history is full: all ${MAX_TRACKED} tracked declarations still await exact wake evidence; no request was sent.` };
      if (blocks()) return { ok: false, message: "A previous nursery request is unresolved; no duplicate was sent." };
      const boundary = transport.parseCursor(boundaryCursor);
      if (!boundary) return { ok: false, message: "Nursery unavailable: exact telemetry cursor is missing; no request was sent." };
      const body = immutableBody(draft);
      makeRoom(victims);
      const item = { key: `nursery-${++sequence}`, resident_id: body.id, name: body.name,
        agent_id: expectedAgentId(body), runner_source: expectedRunnerSource(body), body,
        state: "requesting", boundary,
        candidates: new Map(), httpPending: true, attempt: 1, resetDuringRequest: false,
        requestedAt: now(), updatedAt: now() };
      entries.set(item.key, item); notify(item); return { ok: true, item };
    }
    function retry(key, boundaryCursor) {
      const item = entries.get(key), boundary = transport.parseCursor(boundaryCursor);
      if (!item || !AMBIGUOUS_STATES.has(item.state) || item.httpPending) return { ok: false,
        message: "Only an ambiguous nursery request can be retried unchanged." };
      if (!boundary) return { ok: false,
        message: "Nursery retry unavailable: exact telemetry cursor is missing; no request was sent." };
      item.boundary = boundary; item.candidates.clear(); item.state = "requesting";
      item.httpPending = true; item.resetDuringRequest = false; item.attempt += 1;
      item.requestedAt = now(); item.message = "Retrying the exact original declaration.";
      delete item.code; delete item.remote; delete item.truncated;
      notify(item); return { ok: true, item };
    }
    function accepted(key, receipt) {
      const item = entries.get(key); if (!item || item.state === "alive") return item || null;
      item.httpPending = false; item.request_id = receipt.request_id;
      item.acceptedAt = now(); item.changed = receipt.changed;
      item.declaration_written = receipt.declaration_written;
      item.register_ok = receipt.register_ok; item.register_problems = receipt.register_problems;
      item.truncated = receipt.truncated;
      if (receipt.register_ok === false) return finish(item, "deployment-failed", receipt.message);
      if (receipt.declaration_written === false) return finish(item,
        "converged", `${item.name} already has this exact declaration in Steward; no new wake is claimed.`);
      const evidence = item.candidates.get(item.agent_id);
      if (evidence) {
        item.event = evidence;
        return finish(item, "alive", `${item.name} woke — confirmed by their first real event.`);
      }
      if (item.resetDuringRequest) return finish(item, "ambiguous",
        "Telemetry reset while Steward was answering. Steward accepted the declaration, but a wake inside the reset boundary cannot be proven; only a later exact event can reconcile it.");
      item.state = "pending";
      item.message = `Steward accepted; waiting for ${item.name} to wake.`;
      item.deadlineAt = item.acceptedAt + waitMs;
      item.timer = schedule(() => {
        if (item.state === "pending") finish(item, "silent",
          `${item.name} was created but never seen within ${Math.round(waitMs / 60000)} minutes. Check Steward provisioning and logs.`);
      }, waitMs);
      return notify(item);
    }
    function failed(key, error) {
      const item = entries.get(key); if (!item) return null;
      const kind = error && error.kind || (error && error.definitive ? "rejected" : "ambiguous");
      if (error && error.definitive) item.candidates.clear();
      else if (item.candidates.has(item.agent_id)) {
        item.event = item.candidates.get(item.agent_id);
        return finish(item, "alive", `${item.name} woke — confirmed by their first real event after an ambiguous request outcome.`);
      }
      item.remote = Boolean(error && error.remote); item.truncated = Boolean(error && error.truncated);
      item.code = error && error.code || null;
      return finish(item, kind, error && error.message || "The nursery request failed.");
    }
    function observe(observation, validateEvent) {
      const cursor = transport.parseCursor(observation && observation.cursor);
      for (const item of entries.values()) {
        if (observation && observation.reset) {
          item.candidates.clear();
          if (!cursor) {
            if (item.state === "requesting") {
              item.resetDuringRequest = true; item.state = "ambiguous";
              item.message = "Telemetry reset without a validated ending cursor while Steward was answering. The request remains ambiguous.";
              notify(item);
            } else if (item.state === "pending") finish(item, "ambiguous",
              "Telemetry reset without a validated ending cursor. The resident may have woken inside an unobservable boundary.");
            continue;
          }
          item.boundary = cursor;
          if (item.state === "requesting") {
            item.resetDuringRequest = true; item.state = "ambiguous";
            item.message = "Telemetry reset while Steward was answering. The request remains ambiguous until exact post-reset evidence or its response arrives.";
            notify(item);
          }
          continue;
        }
        if (!canReconcileWake(item) ||
            !cursor || cursor.namespace !== item.boundary.namespace ||
            !transport.offsetAfter(cursor.offset, item.boundary.offset)) continue;
        for (const event of observation.events || []) {
          if (!event || (typeof validateEvent === "function" && validateEvent(event) !== null)) continue;
          const agentId = typeof event.agent_id === "string" ? event.agent_id : null;
          const source = typeof event.source === "string" ? event.source : null;
          if (agentId !== item.agent_id || source !== item.runner_source) continue;
          item.candidates.set(agentId, event);
          if (item.state === "pending" || item.state === "silent" ||
              (AMBIGUOUS_STATES.has(item.state) && !item.httpPending)) {
            item.event = event;
            finish(item, "alive", `${item.name} woke — confirmed by their first real event.`);
            break;
          }
        }
      }
    }
    return { begin, retry, accepted, failed, observe, blocks, busy,
      latest: () => [...entries.values()].at(-1) || null, entries };
  }

  return { CHARS, RUNNERS, DEFAULT_WAIT_MS, MAX_TRACKED, MAX_REMOTE_ERROR, slug, lines, skills,
    validate, requestBody, immutableBody, expectedAgentId, createResident, createTracker };
});
