"use strict";

/* Operational mood is a deterministic instrument reading over retained v0
 * protocol evidence. It is deliberately independent of wall time, manifests,
 * presence and the DOM: the greatest timestamp for each agent is its anchor. */
(function (root, factory) {
  const projection = typeof module === "object" && module.exports
    ? require("./projection.js") : { parseEvents, isValidatedBatch, validatedSelection,
      moodAuthority, moodAuthorityState, withMoodAuthority, moodAuthorityCapsuleByteLength,
      capsuleIdentityEqual };
  const approvals = typeof module === "object" && module.exports
    ? require("./approval-knocks.js") : root.BurrowApprovals;
  const api = factory(projection, approvals);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowMoods = api;
})(typeof globalThis === "object" ? globalThis : this, function (projection, approvals) {
  const retention = typeof module === "object" && module.exports
    ? require("./retention-policy.js") : BurrowRetentionPolicy;
  const HOUR = 60 * 60 * 1000;
  const QUARTER = 15 * 60 * 1000;
  const TERMINAL = new Set(["tool_failed", "routine_failed", "task_failed",
    "heartbeat", "routine_finished", "task_done"]);
  const FAILURES = new Set(["tool_failed", "routine_failed", "task_failed"]);
  const SUCCESSES = new Set(["heartbeat", "routine_finished", "task_done"]);
  const ORDINARY_SUPERSEDERS = new Set(["task_started", "tool_called", "tool_failed",
    "artifact_produced", "heartbeat", "needs_human", "idle", "session_ended"]);
  const WORK = new Map([["task_started", 3], ["task_claimed", 3],
    ["routine_started", 3], ["artifact_produced", 2], ["journal_written", 2],
    ["tool_called", 1], ["heartbeat", 1]]);
  const MAX_RETAINED_PER_AGENT = retention.mood_retained_per_agent;
  // Fixed-space authority is deliberately conservative. Once more distinct
  // immutable approval/root facts exist than can be represented, exact future
  // invalidation is unknowable; the reducer reports uncertainty instead of
  // silently choosing a suffix and presenting it as truth.
  const MAX_AUTHORITY_EVENTS = retention.mood_authority_events;
  const MAX_AUTHORITY_BYTES = retention.mood_authority_bytes;
  const EFFECTIVE_BATCHES = new WeakSet();

  function emptyOverflow(events) {
    const clean = projection.parseEvents([...events]);
    const result = projection.withMoodAuthority ? projection.withMoodAuthority(
      clean, [...clean], [], [], { ordinals: [], rawOrdinals: [], rawIndexes: [],
        rawCount: "0000000000000000", overflow: true,
        observed: MAX_AUTHORITY_EVENTS + 1 }) : clean;
    EFFECTIVE_BATCHES.add(result);
    return result;
  }

  function isAuthorityEvent(event) {
    const payload = event.payload || {};
    return (event.type === "needs_human" && approvals.classify(event).kind === "structured") ||
      event.type === "needs_human_resolved" ||
      (event.type === "task_started" && ["claude-code", "codex"].includes(event.source) &&
        !Object.hasOwn(payload, "parent_agent_id"));
  }

  function requiredMoodRawIndexes(rawEvents, authorityEvents = []) {
    const rawSet = new Set(rawEvents), required = new Set();
    const authorityProof = authorityEvents.map(event => ({ ...event }));
    let witnessOverflow = false;
    for (const input of [rawEvents, [...authorityProof, ...rawEvents]]) {
      const proof = retainMoodWitnesses(projection.parseEvents([...input]), true);
      witnessOverflow = witnessOverflow || proof.witnessOverflow;
      for (const event of proof.events) if (rawSet.has(event)) required.add(event);
    }
    const indexes = [];
    rawEvents.forEach((event, index) => { if (required.has(event)) indexes.push(index); });
    return { indexes, witnessOverflow };
  }

  function effectiveEvents(batch) {
    const events = projection.parseEvents(batch || []);
    if (EFFECTIVE_BATCHES.has(events)) return events;
    const state = projection.moodAuthorityState ? projection.moodAuthorityState(events) :
      { events: projection.moodAuthority ? projection.moodAuthority(events) : [],
        ordinals: [], copies: [], rawOrdinals: [], overflow: false, observed: 0 };
    const copies = new Set(state.copies);
    const rawOrdinalByIndex = new Map((state.rawIndexes || []).map((rawIndex, index) =>
      [Number(rawIndex), Number(state.rawOrdinals[index])]));
    const capsuleRawCount = Number(state.rawCount || 0);
    const sparseCompleteEpoch = !state.overflow && state.events.length > 0;
    let maximumOrdinal = -1;
    for (const ordinal of state.ordinals) maximumOrdinal = Math.max(maximumOrdinal, Number(ordinal));
    for (const ordinal of state.rawOrdinals) maximumOrdinal = Math.max(maximumOrdinal, Number(ordinal));
    const requiredAllocations = sparseCompleteEpoch ?
      Math.max(0, events.length - capsuleRawCount) : events.length;
    // Never form MAX_SAFE_INTEGER + 1: the overflow marker is the only exact,
    // durable representation once a new raw/authority coordinate cannot be
    // allocated. A capsule-only fold needs no allocation and remains exact.
    if (maximumOrdinal > Number.MAX_SAFE_INTEGER - requiredAllocations) {
      return emptyOverflow(events);
    }
    let nextOrdinal = requiredAllocations ? maximumOrdinal + 1 : 0;

    if (sparseCompleteEpoch) {
      /* The sparse list is only a transport optimization, never an assertion
       * we trust. Re-run the exact bounded witness selector over all raw epoch
       * records plus the capsule's irreducible authority. If any event that
       * selector needs is absent from the manifest, reject the capsule as one
       * atomic unit and preserve every raw record. Co-retained records that do
       * not affect Mood remain legitimately sparse. */
      const rawEpoch = events.slice(0, capsuleRawCount);
      const manifestedRaw = new Set((state.rawIndexes || []).map(Number));
      // Raw-only is a conservative proof (capsule authority can only replace
      // approval/root witnesses); the combined fold additionally proves the
      // actual lifecycle interaction with the capsule authority.
      const proof = requiredMoodRawIndexes(rawEpoch, state.events);
      const expectedIndexes = new Set(proof.indexes);
      const complete = exactCapsuleAuthority(state, rawEpoch) &&
        !proof.witnessOverflow &&
        expectedIndexes.size === manifestedRaw.size &&
        [...expectedIndexes].every(index => manifestedRaw.has(index));
      if (!complete) {
        const clean = projection.parseEvents([...events]);
        EFFECTIVE_BATCHES.add(clean);
        return clean;
      }
    }
    const merged = [];
    state.events.forEach((event, index) => {
      const ordinal = Number(state.ordinals[index]);
      if (!copies.has(state.ordinals[index])) {
        merged.push({ event, ordinal, tie: index });
      }
    });
    events.forEach((event, index) => {
      // A rotation capsule's sparse manifest is the complete Mood raw view for
      // its source epoch. Other retained records belong to independent
      // projections; only genuinely appended records after that epoch enter.
      if (sparseCompleteEpoch && index < capsuleRawCount && !rawOrdinalByIndex.has(index)) return;
      const ordinal = rawOrdinalByIndex.has(index) ? rawOrdinalByIndex.get(index) : nextOrdinal++;
      merged.push({ event, ordinal,
        tie: state.events.length + index });
    });
    merged.sort((left, right) => left.ordinal - right.ordinal || left.tie - right.tie);
    const parsed = projection.parseEvents(merged.map(item => item.event));
    const observed = state.observed || 0;
    const result = projection.withMoodAuthority ? projection.withMoodAuthority(
      parsed, [...parsed], [], [], { overflow: state.overflow || observed > MAX_AUTHORITY_EVENTS,
        observed }) : parsed;
    EFFECTIVE_BATCHES.add(result);
    return result;
  }

  function combineMoodEvidence(...batches) {
    const effective = batches.map(effectiveEvents);
    const parsed = projection.parseEvents(effective.flatMap(batch => [...batch]));
    const overflow = effective.some(batch => projection.moodAuthorityState &&
      projection.moodAuthorityState(batch).overflow);
    const result = projection.withMoodAuthority ? projection.withMoodAuthority(parsed,
      [...parsed], [], [], { observed: overflow ? MAX_AUTHORITY_EVENTS + 1 : 0, overflow }) : parsed;
    EFFECTIVE_BATCHES.add(result);
    return result;
  }

  const age = (anchor, event) => Math.max(0, anchor - Date.parse(event.ts));
  const labelAge = milliseconds => milliseconds <= 6 * HOUR ? "recent" :
    milliseconds <= 24 * HOUR ? "aging" : "old";
  const needAge = milliseconds => milliseconds <= HOUR ? "≤1h" :
    milliseconds <= 6 * HOUR ? ">1–6h" : ">6h";
  const eventKey = (event, ordinal) => `${ordinal}\0${event.ts}\0${event.type}`;
  function contributingWitnesses(groups) {
    const witnesses = new Map();
    for (const entry of groups.flat()) {
      witnesses.set(eventKey(entry.event, entry.ordinal), entry);
    }
    return [...witnesses.values()].sort((left, right) => left.ordinal - right.ordinal);
  }
  function thresholdWitnesses(contributors) {
    if (contributors.length <= 6) return contributors;
    const byTime = [...contributors].sort((left, right) =>
      Date.parse(left.event.ts) - Date.parse(right.event.ts) || left.ordinal - right.ordinal);
    const selected = new Map();
    for (const entry of [byTime[0], byTime.at(-1), ...contributors.slice().reverse()]) {
      selected.set(eventKey(entry.event, entry.ordinal), entry);
      if (selected.size === 6) break;
    }
    return [...selected.values()].sort((left, right) => left.ordinal - right.ordinal);
  }

  /* One reverse fold identifies every plain/fallback knock with no later
   * ordinary superseder. Reusing it in derivation and retention keeps long
   * legacy logs linear rather than scanning an append suffix per knock. */
  function unresolvedPlainEntries(entries) {
    const unresolved = [];
    let ordinaryAfter = false;
    for (let index = entries.length - 1; index >= 0; index--) {
      const entry = entries[index], event = entry.event;
      if (event.type === "needs_human" &&
          approvals.classify(event).kind !== "structured" && !ordinaryAfter) {
        unresolved.push(entry);
      }
      if (ORDINARY_SUPERSEDERS.has(event.type)) ordinaryAfter = true;
    }
    unresolved.reverse();
    return unresolved;
  }

  function approvalAuthority(events) {
    // Mood consumes complete canonical approval truth. The approval panel's
    // presentation capacity must never hide an older unresolved request.
    const state = approvals.createState({ maxRequests: null });
    approvals.foldValidated(state, events, { isValidatedBatch: projection.isValidatedBatch });
    return state;
  }

  /* Mood treats each first append of an incompatible immutable request as its
   * own canonical knock. The approval panel must quarantine the shared ID,
   * but collapsing the collisions into one presentation record would erase
   * the colliding agent's unresolved human need. */
  function moodApprovalAuthority(events) {
    const groups = new Map();
    const byKnock = new Map();
    const byResolution = new Map();
    const resolutionDependencies = new Map();
    for (let ordinal = 0; ordinal < events.length; ordinal++) {
      const event = events[ordinal];
      if (event.type === "needs_human") {
        const shape = approvals.classify(event);
        if (shape.kind !== "structured") continue;
        let group = groups.get(shape.request_id);
        if (!group) {
          group = { request_id: shape.request_id, candidates: [], byLifecycle: new Map(),
            collided: false };
          groups.set(shape.request_id, group);
        }
        const lifecycle = approvals.lifecycleIdentity(event, shape);
        if (group.byLifecycle.has(lifecycle)) continue;
        const candidate = { knock: event, ordinal, lifecycle,
          resolutionIdentity: approvals.resolutionIdentity(event, shape), resolution: null,
          resolutionOrdinal: null, group };
        group.candidates.push(candidate); group.byLifecycle.set(lifecycle, candidate);
        byKnock.set(event, candidate);
        if (group.candidates.length > 1) {
          group.collided = true;
          // Collision quarantine is permanent for the retained source epoch:
          // every candidate stays unresolved and every close is ignored.
          for (const member of group.candidates) {
            if (member.resolution) {
              byResolution.delete(member.resolution);
              resolutionDependencies.delete(member.resolution);
            }
            member.resolution = null; member.resolutionOrdinal = null;
          }
        }
        continue;
      }
      if (event.type !== "needs_human_resolved") continue;
      const resolutionIdentity = approvals.resolutionIdentity(event);
      if (!resolutionIdentity) continue;
      const requestId = event.payload && event.payload.request_id;
      const group = groups.get(requestId);
      if (!group || group.collided) continue;
      const matches = group.candidates.filter(candidate => !candidate.resolution &&
        candidate.resolutionIdentity === resolutionIdentity);
      // If this close is retained as an anchor or another generic witness, two
      // matching open lifecycles are the bounded proof that it stayed
      // ambiguous rather than becoming an invented exact decision.
      if (matches.length) resolutionDependencies.set(event, matches.slice(0, 2));
      if (matches.length === 1) {
        matches[0].resolution = event; matches[0].resolutionOrdinal = ordinal;
        byResolution.set(event, matches[0]);
      }
    }
    return { groups, byKnock, byResolution, resolutionDependencies,
      candidates: [...groups.values()].flatMap(group => group.candidates) };
  }

  function completeCandidate(candidate) {
    if (!candidate) return [];
    const canonical = candidate.group.candidates[0];
    const members = [canonical, candidate];
    // If the canonical request itself was selected, one incompatible canonical
    // knock is the bounded witness that reconstructs collision quarantine.
    if (candidate === canonical && candidate.group.candidates.length > 1) {
      members.push(candidate.group.candidates[1]);
    }
    return [...new Set(members)];
  }

  function compactAuthoritySet(events, moodAuthority) {
    const authoritySet = new Set();
    const latestRootByAgent = new Map();
    for (const event of events) {
      const payload = event.payload || {};
      if (event.type === "task_started" && ["claude-code", "codex"].includes(event.source) &&
          !Object.hasOwn(payload, "parent_agent_id")) latestRootByAgent.set(event.agent_id, event);
    }
    for (const event of latestRootByAgent.values()) authoritySet.add(event);
    for (const candidate of moodAuthority.candidates) {
      authoritySet.add(candidate.knock);
      if (candidate.resolution) authoritySet.add(candidate.resolution);
    }
    for (const [resolution, dependencies] of moodAuthority.resolutionDependencies) {
      authoritySet.add(resolution);
      for (const candidate of dependencies) authoritySet.add(candidate.knock);
    }
    return authoritySet;
  }

  /* Structured candidates and root owners are monotonic irreducible facts:
   * later closes can add/remove close witnesses and newer roots can replace an
   * older root, but neither can remove a distinct lifecycle or root owner.  A
   * 257th such fact therefore proves overflow without constructing the rest of
   * an arbitrarily large approval graph.  Keeping only the first 257
   * identities also bounds preflight memory while the ordinary event scan
   * remains necessarily linear in the supplied batch. */
  function authorityCertainlyOverflows(events) {
    const roots = new Set(), lifecyclesByRequest = new Map();
    let irreducible = 0;
    for (const event of events) {
      const payload = event.payload || {};
      if (event.type === "task_started" && ["claude-code", "codex"].includes(event.source) &&
          !Object.hasOwn(payload, "parent_agent_id")) {
        if (!roots.has(event.agent_id)) {
          roots.add(event.agent_id); irreducible++;
        }
      } else if (event.type === "needs_human") {
        const shape = approvals.classify(event);
        if (shape.kind !== "structured") continue;
        let lifecycles = lifecyclesByRequest.get(shape.request_id);
        if (!lifecycles) {
          lifecycles = new Set(); lifecyclesByRequest.set(shape.request_id, lifecycles);
        }
        const lifecycle = approvals.lifecycleIdentity(event, shape);
        if (!lifecycles.has(lifecycle)) {
          lifecycles.add(lifecycle); irreducible++;
        }
      }
      if (irreducible > MAX_AUTHORITY_EVENTS) return true;
    }
    return false;
  }

  /* A capsule is a claimed canonical fold, not an authority source in its own
   * right. Reconstruct the declared source epoch in its SAFE ordinal order,
   * fold it with the production approval/root rules, and require its complete
   * graph (including ordinals) to be exactly that irreducible result. */
  function exactCapsuleAuthority(state, rawEpoch) {
    const copies = new Set(state.copies);
    const ordered = [];
    for (let index = 0; index < state.events.length; index++) {
      if (!copies.has(state.ordinals[index])) {
        ordered.push({ event: state.events[index], ordinal: Number(state.ordinals[index]) });
      }
    }
    for (let index = 0; index < state.rawIndexes.length; index++) {
      const event = rawEpoch[Number(state.rawIndexes[index])];
      if (event && isAuthorityEvent(event)) {
        ordered.push({ event, ordinal: Number(state.rawOrdinals[index]) });
      }
    }
    ordered.sort((left, right) => left.ordinal - right.ordinal);
    for (let index = 1; index < ordered.length; index++) {
      if (ordered[index - 1].ordinal === ordered[index].ordinal) return false;
    }
    const reconstructed = projection.parseEvents(ordered.map(item => item.event));
    const authority = moodApprovalAuthority(reconstructed);
    const canonical = compactAuthoritySet(reconstructed, authority);
    const folded = ordered.filter((item, index) => canonical.has(reconstructed[index]));
    if (folded.length !== state.events.length) return false;
    for (let index = 0; index < folded.length; index++) {
      if (folded[index].ordinal !== Number(state.ordinals[index]) ||
          (folded[index].event !== state.events[index] &&
            !projection.capsuleIdentityEqual(folded[index].event, state.events[index]))) return false;
    }
    return true;
  }

  function deriveOne(agentId, entries, approvalState, moodAuthority, authorityComplete) {
    let anchorMs = -Infinity;
    for (const { event } of entries) anchorMs = Math.max(anchorMs, Date.parse(event.ts));
    const anchor = new Date(anchorMs).toISOString();
    if (!authorityComplete) {
      return { agent_id: agentId, anchor, anchorMs, glyph: "?",
        status: "authority history uncertain", enoughEvidence: false,
        authority: { complete: false }, score: null,
        evidence: { count: 0, spanMs: 0 },
        signals: {
          failure: { observed: false, streak: null, failures: null,
            failuresLabel: "authority history uncertain" },
          workload: { observed: false, density: null,
            level: "authority history uncertain", buckets: [] },
          interaction: { observed: false, level: "authority history uncertain",
            logAgeMs: null, kind: null },
          unresolvedNeed: { observed: false, state: "authority history uncertain",
            logAgeMs: null, kind: null, request_id: null },
        } };
    }
    const outcomes = entries.filter(({ event }) => TERMINAL.has(event.type) &&
      age(anchorMs, event) < 24 * HOUR);
    const failureCount = Math.min(3, outcomes.filter(({ event }) =>
      FAILURES.has(event.type)).length);
    let streak = 0;
    for (let index = outcomes.length - 1; index >= 0; index--) {
      const type = outcomes[index].event.type;
      if (SUCCESSES.has(type)) break;
      if (FAILURES.has(type)) streak = Math.min(3, streak + 1);
    }
    const failureObserved = outcomes.length > 0;
    const failure = { observed: failureObserved,
      streak: failureObserved ? streak : null,
      failures: failureObserved ? failureCount : null,
      failuresLabel: !failureObserved ? "unobserved" : failureCount >= 3 ? "3+" : String(failureCount) };

    const bucketBase = Math.floor(anchorMs / QUARTER);
    const buckets = new Map();
    const workloadWitnesses = [];
    for (const entry of entries) {
      const weight = WORK.get(entry.event.type);
      if (!weight) continue;
      const bucket = Math.floor(Date.parse(entry.event.ts) / QUARTER);
      if (bucket < bucketBase - 7 || bucket > bucketBase) continue;
      const current = buckets.get(bucket);
      if (!current || weight >= current.weight) buckets.set(bucket, { ...entry, weight });
    }
    for (const item of buckets.values()) workloadWitnesses.push(item);
    const density = workloadWitnesses.reduce((sum, item) => sum + item.weight, 0);
    const workloadObserved = workloadWitnesses.length > 0;
    const workloadLevel = !workloadObserved ? "unobserved" : density <= 6 ? "light" :
      density <= 14 ? "active" : density <= 20 ? "heavy" : "saturated";
    const workload = { observed: workloadObserved, density: workloadObserved ? density : null,
      level: workloadLevel, buckets: [...buckets].sort((a, b) => a[0] - b[0])
        .map(([bucket, item]) => ({ bucket, weight: item.weight })) };

    const ordinalByEvent = new Map();
    for (const entry of entries) {
      if (!ordinalByEvent.has(entry.event)) ordinalByEvent.set(entry.event, entry.ordinal);
    }
    const humanCandidates = [];
    for (const record of approvalState.requests.values()) {
      if (record && !record.collided && record.resolution && record.knock.agent_id === agentId) {
        const ordinal = ordinalByEvent.get(record.resolution);
        if (ordinal !== undefined) humanCandidates.push({ event: record.resolution, ordinal });
      }
    }
    for (const entry of entries) {
      const event = entry.event, payload = event.payload || {};
      if (event.type === "task_started" && ["claude-code", "codex"].includes(event.source) &&
          !Object.hasOwn(payload, "parent_agent_id")) humanCandidates.push(entry);
    }
    humanCandidates.sort((left, right) => left.ordinal - right.ordinal);
    const latestHuman = humanCandidates.at(-1) || null;
    const interactionAge = latestHuman ? age(anchorMs, latestHuman.event) : null;
    const interaction = { observed: Boolean(latestHuman),
      level: latestHuman ? labelAge(interactionAge) : "unobserved",
      logAgeMs: interactionAge,
      kind: !latestHuman ? null : latestHuman.event.type === "needs_human_resolved" ?
        "approval decision" : "root prompt" };

    const unresolved = [];
    for (const candidate of moodAuthority.candidates) {
      if (candidate.knock.agent_id !== agentId || candidate.resolution) continue;
      const ordinal = ordinalByEvent.get(candidate.knock);
      if (ordinal !== undefined) unresolved.push({ event: candidate.knock, ordinal,
        kind: candidate.group.candidates.length > 1 ?
          "structured collision" : "structured request" });
    }
    for (const entry of unresolvedPlainEntries(entries)) {
      const shape = approvals.classify(entry.event);
      unresolved.push({ ...entry,
        kind: shape.kind === "malformed" ? "fallback knock" : "plain knock" });
    }
    unresolved.sort((left, right) => {
      const difference = age(anchorMs, right.event) - age(anchorMs, left.event);
      return difference || left.ordinal - right.ordinal;
    });
    const oldestNeed = unresolved[0] || null;
    const unresolvedAge = oldestNeed ? age(anchorMs, oldestNeed.event) : null;
    const need = { observed: Boolean(oldestNeed), state: oldestNeed ? needAge(unresolvedAge) : "none observed in retained authority",
      logAgeMs: unresolvedAge, kind: oldestNeed && oldestNeed.kind || null,
      request_id: oldestNeed && approvals.classify(oldestNeed.event).kind === "structured" ?
        approvals.classify(oldestNeed.event).request_id : null };

    const witnesses = contributingWitnesses([outcomes, workloadWitnesses,
      latestHuman ? [latestHuman] : [], unresolved]);
    let witnessMinimum = Infinity, witnessMaximum = -Infinity;
    for (const entry of witnesses) {
      const timestamp = Date.parse(entry.event.ts);
      witnessMinimum = Math.min(witnessMinimum, timestamp);
      witnessMaximum = Math.max(witnessMaximum, timestamp);
    }
    const witnessSpan = witnesses.length ? witnessMaximum - witnessMinimum : 0;
    const observedSignals = [failureObserved, workloadObserved, Boolean(latestHuman)].filter(Boolean).length;
    const enough = Boolean(oldestNeed) || streak >= 2 || (witnesses.length >= 6 &&
      witnessSpan >= 30 * 60 * 1000 &&
      observedSignals >= 2);
    const failurePoints = !failureObserved ? 0 : [2, -1, -3, -5][streak];
    const workloadPoints = workloadLevel === "active" ? 2 : workloadLevel === "saturated" ? -2 : 0;
    const interactionPoints = interaction.level === "recent" ? 1 : interaction.level === "old" ? -1 : 0;
    const needPoints = !oldestNeed ? 0 : unresolvedAge <= HOUR ? -1 :
      unresolvedAge <= 6 * HOUR ? -3 : -5;
    const score = failurePoints + workloadPoints + interactionPoints + needPoints;
    let glyph = "?", status = "not enough observed";
    if (enough && oldestNeed && unresolvedAge > 6 * HOUR) { glyph = "!"; status = "blocked"; }
    else if (enough && streak === 3) { glyph = "×"; status = "repeated failures"; }
    else if (enough && workloadLevel === "saturated") { glyph = "▲"; status = "overloaded"; }
    else if (enough && score >= 4) { glyph = "●"; status = "steady"; }
    else if (enough && score >= 1) { glyph = "◆"; status = "active"; }
    else if (enough) { glyph = "◇"; status = "watchful"; }
    return { agent_id: agentId, anchor, anchorMs, glyph, status,
      enoughEvidence: authorityComplete && enough,
      authority: { complete: authorityComplete },
      score, evidence: { count: Math.min(6, witnesses.length),
        spanMs: Math.min(30 * 60 * 1000, witnessSpan) },
      signals: { failure, workload, interaction, unresolvedNeed: need } };
  }

  /* Find an oversized terminal frontier without materialising an unbounded
   * per-agent history. The fixed ring is evidence attachment only: once the
   * 161st canonical witness is observed, Mood is globally uncertain and no
   * partial signal will be presented as exact. */
  function terminalFrontierScan(events) {
    const anchors = new Map(), latest = new Map();
    events.forEach((event, ordinal) => {
      const timestamp = Date.parse(event.ts), old = anchors.get(event.agent_id);
      latest.set(event.agent_id, { event, ordinal, timestamp });
      if (!old || timestamp > old.timestamp ||
          (timestamp === old.timestamp && ordinal > old.ordinal)) {
        anchors.set(event.agent_id, { event, ordinal, timestamp });
      }
    });
    const frontiers = new Map();
    let witnessOverflow = false;
    events.forEach(event => {
      if (!TERMINAL.has(event.type)) return;
      const anchor = anchors.get(event.agent_id);
      if (!anchor || age(anchor.timestamp, event) >= 24 * HOUR) return;
      let frontier = frontiers.get(event.agent_id);
      if (!frontier) {
        frontier = { events: [], next: 0, observed: 0 };
        frontiers.set(event.agent_id, frontier);
      }
      frontier.observed = Math.min(MAX_RETAINED_PER_AGENT + 1, frontier.observed + 1);
      if (frontier.events.length < MAX_RETAINED_PER_AGENT) frontier.events.push(event);
      else {
        frontier.events[frontier.next] = event;
        frontier.next = (frontier.next + 1) % MAX_RETAINED_PER_AGENT;
      }
      if (frontier.observed > MAX_RETAINED_PER_AGENT) witnessOverflow = true;
    });
    const bounded = new Set();
    if (witnessOverflow) {
      for (const [agentId, anchor] of anchors) {
        bounded.add(anchor.event);
        bounded.add(latest.get(agentId).event);
      }
    }
    return { anchors, latest, witnessOverflow, bounded };
  }

  function deriveMoods(validatedAppendOrderedEvents) {
    const events = effectiveEvents(validatedAppendOrderedEvents);
    // Derive over the same bounded exact projection used for transport. This
    // makes a huge irrelevant/raw history pay only one selector pass and keeps
    // the subsequent per-agent reducer bounded by the 160-witness contract.
    const retained = retainMoodWitnesses(events);
    const retainedState = projection.moodAuthorityState ?
      projection.moodAuthorityState(retained) : { overflow: false };
    const authorityComplete = !retainedState.overflow;
    if (!authorityComplete) {
      const { anchors } = terminalFrontierScan(events);
      const emptyApproval = { requests: new Map() };
      const emptyMoodAuthority = { candidates: [] };
      return new Map([...anchors].map(([agentId, anchor]) => [agentId,
        deriveOne(agentId, [{ event: anchor.event, ordinal: anchor.ordinal }],
          emptyApproval, emptyMoodAuthority, false)]));
    }
    const analysisEvents = effectiveEvents(retained);
    const approvalState = approvalAuthority(analysisEvents);
    const moodAuthority = moodApprovalAuthority(analysisEvents);
    const byAgent = new Map();
    analysisEvents.forEach((event, ordinal) => {
      const entries = byAgent.get(event.agent_id) || [];
      entries.push({ event, ordinal }); byAgent.set(event.agent_id, entries);
    });
    return new Map([...byAgent].map(([agentId, entries]) =>
      [agentId, deriveOne(agentId, entries, approvalState, moodAuthority, authorityComplete)]));
  }

  /* Independent bounded authority for incremental browser ingestion. It keeps
   * the exact witnesses needed by deriveMoods and never implies presence. */
  function retainMoodWitnesses(validatedAppendOrderedEvents, proofOnly = false) {
    const events = effectiveEvents(validatedAppendOrderedEvents);
    const incomingState = projection.moodAuthorityState ? projection.moodAuthorityState(events) :
      { overflow: false, observed: 0 };
    const frontierScan = terminalFrontierScan(events);
    if (incomingState.overflow) {
      const keep = new Set();
      for (const [agentId, anchor] of frontierScan.anchors) {
        keep.add(anchor.event);
        keep.add(frontierScan.latest.get(agentId).event);
      }
      const selected = events.filter(event => keep.has(event));
      if (proofOnly) return { events: projection.validatedSelection(events, selected),
        witnessOverflow: true };
      return projection.withMoodAuthority ? projection.withMoodAuthority(
        events, selected, [], [], { ordinals: [], rawOrdinals: [], rawIndexes: [],
          rawCount: "0000000000000000", overflow: true,
          observed: MAX_AUTHORITY_EVENTS + 1 }) :
        projection.validatedSelection(events, selected);
    }
    if (frontierScan.witnessOverflow) {
      const selected = events.filter(event => frontierScan.bounded.has(event));
      if (proofOnly) return { events: projection.validatedSelection(events, selected),
        witnessOverflow: true };
      return projection.withMoodAuthority ? projection.withMoodAuthority(
        events, selected, [], [], { ordinals: [], rawOrdinals: [], rawIndexes: [],
          rawCount: "0000000000000000", overflow: true,
          observed: MAX_AUTHORITY_EVENTS + 1 }) :
        projection.validatedSelection(events, selected);
    }
    if (authorityCertainlyOverflows(events)) {
      // A structured-only history has no terminal-overflow attachment set, so
      // retain the same bounded anchor/latest evidence used by durable incoming
      // overflow. This metadata never manufactures presence or partial Mood.
      const keep = new Set();
      for (const [agentId, anchor] of frontierScan.anchors) {
        keep.add(anchor.event);
        keep.add(frontierScan.latest.get(agentId).event);
      }
      const bounded = events.filter(event => keep.has(event));
      if (proofOnly) return { events: projection.validatedSelection(events, bounded),
        witnessOverflow: true };
      return projection.withMoodAuthority ? projection.withMoodAuthority(
        events, bounded, [], [], { ordinals: [], rawOrdinals: [], rawIndexes: [],
          rawCount: "0000000000000000", overflow: true,
          observed: MAX_AUTHORITY_EVENTS + 1 }) :
        projection.validatedSelection(events, bounded);
    }
    const authority = approvalAuthority(events);
    const moodAuthority = moodApprovalAuthority(events);
    const authoritySet = compactAuthoritySet(events, moodAuthority);
    let overflow = incomingState.overflow || authoritySet.size > MAX_AUTHORITY_EVENTS;
    const authoritativeCloses = new Set();
    for (const record of authority.requests.values()) {
      if (!record || !record.knock) continue;
      if (record.resolution && !record.collided) {
        authoritativeCloses.add(record.resolution);
      }
    }
    const byAgent = new Map();
    events.forEach((event, ordinal) => {
      const list = byAgent.get(event.agent_id) || [];
      list.push({ event, ordinal }); byAgent.set(event.agent_id, list);
    });
    const moodApprovalEvents = new Set();
    for (const [agentId, entries] of byAgent) {
      let anchor = -Infinity;
      for (const item of entries) anchor = Math.max(anchor, Date.parse(item.event.ts));
      const records = moodAuthority.candidates.filter(candidate =>
        candidate.knock.agent_id === agentId);
      const oldestStructured = records.filter(candidate => !candidate.resolution)
        .map(candidate => ({ candidate, ordinal: candidate.ordinal }))
        .sort((left, right) => {
          const difference = age(anchor, right.candidate.knock) - age(anchor, left.candidate.knock);
          return difference || left.ordinal - right.ordinal;
        })[0] || null;
      // The internal authority capsule owns resolved predecessors without a
      // finite fallback cap. Raw retention needs only today's oldest open
      // lifecycle; a future append is folded against the capsule first.
      for (const selected of [oldestStructured]) {
        if (!selected) continue;
        for (const candidate of completeCandidate(selected.candidate)) {
          moodApprovalEvents.add(candidate.knock);
          if (candidate.resolution) moodApprovalEvents.add(candidate.resolution);
        }
      }
    }
    const keep = new Set(), minimalKeep = new Set();
    for (const [agentId, entries] of byAgent) {
      let anchor = -Infinity;
      for (const item of entries) anchor = Math.max(anchor, Date.parse(item.event.ts));
      // Producer timestamps are not append ordered. Every outcome in the
      // current lower-open frontier is required: a future timestamp can expire
      // an append-later success and expose any earlier retained outcome.
      const terminalFrontier = entries.filter(item => TERMINAL.has(item.event.type) &&
        age(anchor, item.event) < 24 * HOUR);
      const bucketBase = Math.floor(anchor / QUARTER), bucketBest = new Map();
      for (const item of entries) {
        const weight = WORK.get(item.event.type), bucket = Math.floor(Date.parse(item.event.ts) / QUARTER);
        if (!weight || bucket < bucketBase - 7 || bucket > bucketBase) continue;
        const old = bucketBest.get(bucket);
        if (!old || weight >= old.weight) bucketBest.set(bucket, { ...item, weight });
      }
      const humans = entries.filter(item => authoritativeCloses.has(item.event) ||
        (item.event.type === "task_started" && ["claude-code", "codex"].includes(item.event.source) &&
          !Object.hasOwn(item.event.payload || {}, "parent_agent_id"))).slice(-1);
      const approvalsForAgent = entries.filter(item => moodApprovalEvents.has(item.event));
      const openPlain = unresolvedPlainEntries(entries);
      if (openPlain.length) approvalsForAgent.push(openPlain.at(-1));
      const anchorWitness = entries.reduce((best, item) => Date.parse(item.event.ts) >=
        Date.parse(best.event.ts) ? item : best);
      minimalKeep.add(anchorWitness.event);
      minimalKeep.add(entries.at(-1).event);
      const retainedOutcomes = terminalFrontier;
      const unresolvedOrdinals = new Set();
      for (const candidate of moodAuthority.candidates) {
        if (candidate.knock.agent_id !== agentId || candidate.resolution) continue;
        unresolvedOrdinals.add(candidate.ordinal);
      }
      for (const item of openPlain) unresolvedOrdinals.add(item.ordinal);
      const unresolvedEntries = entries.filter(item => unresolvedOrdinals.has(item.ordinal));
      const threshold = thresholdWitnesses(contributingWitnesses([retainedOutcomes,
        [...bucketBest.values()], humans, unresolvedEntries]));
      // An unresolved need is a contributor today but a later ordinary event
      // or exact close can remove it. Preserve a second six-witness proof that
      // excludes every unresolved lifecycle so future folding cannot fall
      // below the evidence threshold merely because the need was superseded.
      const futureSafeThreshold = thresholdWitnesses(contributingWitnesses([
        retainedOutcomes, [...bucketBest.values()], humans]));
      const candidates = [...terminalFrontier, ...bucketBest.values(), ...humans, ...approvalsForAgent,
        ...threshold, ...futureSafeThreshold, anchorWitness];
      candidates.sort((a, b) => a.ordinal - b.ordinal);
      const selected = [...new Map(candidates.map(item => [item.ordinal, item])).values()]
        .sort((a, b) => a.ordinal - b.ordinal);
      // A retained plain/fallback knock may be the timestamp anchor even though
      // append-later ordinary evidence closed it. Preserve one such boundary so
      // rotation cannot resurrect the knock when producer timestamps disagree
      // with append order.
      for (const item of [...selected]) {
        if (item.event.type !== "needs_human" ||
            approvals.classify(item.event).kind === "structured") continue;
        const boundary = entries.slice().reverse().find(later =>
          later.ordinal > item.ordinal && ORDINARY_SUPERSEDERS.has(later.event.type));
        if (boundary) selected.push(boundary);
      }
      const canonical = [...new Map(selected.map(item => [item.ordinal, item])).values()]
        .sort((a, b) => a.ordinal - b.ordinal);
      if (canonical.length > MAX_RETAINED_PER_AGENT) overflow = true;
      let bounded = canonical.slice(-MAX_RETAINED_PER_AGENT);
      if (canonical.length > MAX_RETAINED_PER_AGENT && !bounded.includes(anchorWitness)) {
        bounded = [...bounded.slice(1), anchorWitness].sort((a, b) => a.ordinal - b.ordinal);
      }
      for (const item of bounded) keep.add(item.event);
    }
    // Preserve the strict per-owner selection before lifecycle completion.
    // If global authority is already uncertain, exact cross-agent attachment
    // is neither knowable nor useful and must not escape the 160-record bound.
    const boundedKeep = new Set(keep);
    // A selected structured knock is not safe in isolation. Complete its
    // canonical/collision group (including cross-agent members and exact
    // closes) after per-agent selection so timestamp anchors cannot resurrect
    // a closed lifecycle and a collision keeps its true owner after rotation.
    if (!overflow) {
      for (const event of [...keep]) {
        const selected = moodAuthority.byKnock.get(event) ||
          moodAuthority.byResolution.get(event);
        const dependencies = moodAuthority.resolutionDependencies.get(event) || [];
        for (const candidate of [selected, ...dependencies].filter(Boolean)) {
          for (const member of completeCandidate(candidate)) {
            keep.add(member.knock);
            if (member.resolution) keep.add(member.resolution);
          }
        }
      }
      const retainedByOwner = new Map();
      for (const event of keep) {
        const count = (retainedByOwner.get(event.agent_id) || 0) + 1;
        retainedByOwner.set(event.agent_id, count);
        if (count > MAX_RETAINED_PER_AGENT) overflow = true;
      }
      if (overflow) {
        keep.clear();
        for (const event of boundedKeep) keep.add(event);
      }
    }
    let selected = events.filter(event => (overflow ? minimalKeep : keep).has(event));
    // Completeness proofs need only the exact selector result. Stopping here
    // avoids recursively constructing and sizing another transport capsule.
    if (proofOnly) return { events: projection.validatedSelection(events, selected),
      witnessOverflow: overflow };
    // Orphan resolutions are terminal diagnostics: append-prefix folding has
    // already proven they cannot bind to a future request, so they need no
    // capsule space and cannot create quadratic incremental growth.
    // Once overflow is reached no finite subset can prove exact behavior for
    // unrestricted future IDs, so retain no misleading partial authority.
    const ordinalByEvent = new Map();
    events.forEach((event, index) => {
      if (!ordinalByEvent.has(event)) ordinalByEvent.set(event, String(index));
    });
    let authorityEvents = overflow ? [] : events.filter(event => authoritySet.has(event));
    let ordinals = authorityEvents.map(event => ordinalByEvent.get(event));
    const manifestProof = requiredMoodRawIndexes(selected, authorityEvents);
    const requiredIndexes = new Set(manifestProof.indexes);
    overflow = overflow || manifestProof.witnessOverflow;
    const manifested = selected.map((event, index) => ({ event, index }))
      .filter(item => requiredIndexes.has(item.index));
    let copies = manifested.filter(item => authoritySet.has(item.event)).map(item =>
      ordinalByEvent.get(item.event));
    let rawOrdinals = manifested.map(item => ordinalByEvent.get(item.event));
    let rawIndexes = manifested.map(item => String(item.index).padStart(16, "0"));
    const rawCount = String(selected.length).padStart(16, "0");
    if (!overflow && projection.moodAuthorityCapsuleByteLength &&
        projection.moodAuthorityCapsuleByteLength(authorityEvents, copies,
          { ordinals, rawOrdinals, rawIndexes, rawCount, overflow: false,
            observed: authorityEvents.length }) >
          MAX_AUTHORITY_BYTES) overflow = true;
    // A byte-budget overflow can be discovered only after the canonical raw
    // manifest is known. At that point discard any dependency expansion and
    // return to the already-bounded per-owner selection; the durable overflow
    // capsule truthfully says exact attachment authority is unavailable.
    if (overflow) selected = events.filter(event => minimalKeep.has(event));
    if (overflow) { authorityEvents = []; ordinals = []; copies = []; rawOrdinals = []; rawIndexes = []; }
    return projection.withMoodAuthority ?
      projection.withMoodAuthority(events, selected, authorityEvents,
        overflow ? [] : copies, { ordinals, rawOrdinals, rawIndexes,
          rawCount: overflow ? String(0).padStart(16, "0") : rawCount, overflow,
          observed: overflow ? MAX_AUTHORITY_EVENTS + 1 : authorityEvents.length }) :
      projection.validatedSelection(events, selected);
  }

  return { HOUR, QUARTER, MAX_RETAINED_PER_AGENT, MAX_AUTHORITY_EVENTS,
    MAX_AUTHORITY_BYTES, deriveMoods,
    retainMoodWitnesses, requiredMoodRawIndexes, combineMoodEvidence };
});
