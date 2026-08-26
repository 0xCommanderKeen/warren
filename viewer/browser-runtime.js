"use strict";

/* The browser's deep runtime module. EventSource, fetch and wall time are the
 * true external seams; both the page and tests use this same interface. */
(function (root, factory) {
  const projection = typeof module === "object" && module.exports
    ? require("./projection.js")
    : { validateEvent, parseEvents, parseEventWindows, isValidatedBatch, validatedSelection,
      projectionWitnesses,
      moodAuthority, moodAuthorityCopies, moodAuthorityState, withMoodAuthority, canonicalIdentity,
      capsuleIdentityEqual,
      moodAuthorityCapsuleByteLength,
      routineRejections, taskRejections,
      approvalRejections, journalRejections,
      foldEvents, foldArtifacts, reduce };
  const fleet = typeof module === "object" && module.exports
    ? require("./fleet-operations.js") : root.BurrowFleet;
  const jobs = typeof module === "object" && module.exports
    ? require("./job-board.js") : root.BurrowJobs;
  const routines = typeof module === "object" && module.exports
    ? require("./routine-ledger.js") : root.BurrowRoutines;
  const approvals = typeof module === "object" && module.exports
    ? require("./approval-knocks.js") : root.BurrowApprovals;
  const journals = typeof module === "object" && module.exports
    ? require("./journal-observations.js") : root.BurrowJournals;
  const moods = typeof module === "object" && module.exports
    ? require("./moods.js") : root.BurrowMoods;
  const runtime = factory(projection, fleet, jobs, routines, approvals, journals, moods);
  if (typeof module === "object" && module.exports) module.exports = runtime;
  else root.BurrowBrowser = runtime;
})(typeof globalThis === "object" ? globalThis : this, function (projection, fleet, jobs, routines, approvals, journals, moods) {
  const MAX_TRANSPORT_EVENTS = 4000;

  function createBrowserRuntime(options) {
    const now = options.now;
    const fetch = options.fetch;
    const EventSource = options.EventSource;
    const setTimeout = options.setTimeout;
    const clearTimeout = options.clearTimeout;
    const onProjection = options.onProjection || (() => {});
    const onTransport = options.onTransport || (() => {});
    const onTransportStatus = options.onTransportStatus || (() => {});
    const onFleet = options.onFleet || (() => {});
    const warn = options.warn || (() => {});

    let souls = [];
    let residentSouls = [];
    let legacySouls = [];
    let eventCursor = 0;
    let agents = new Map();
    let artifacts = [];
    let villagers = [];
    let pollPromise = null;
    let residentPromise = null;
    let eventStream = null;
    let streamReady = false;
    let reconnectTimer = null;
    let transport = "disconnected";
    let transportStatus = null;
    let transportStatusPromise = null;
    let reportedResidentDiagnostics = new Set();
    let fleetState = fleet.createFleetState();
    let jobState = jobs.createState();
    let approvalState = approvals.createState();
    let journalState = journals.createState();
    let moodSequence = 0;
    let moodOrdinals = new Map();
    let moodEvidenceByAgent = new Map();
    let moodApprovalsByRequest = new Map();
    let moodAuthorityState = { events: [], ordinals: [], copies: [], rawOrdinals: [],
      overflow: false, observed: 0 };
    let residentReport = { residents: [], diagnosticResidents: [], diagnostics: [], available: false };

    function publishFleet(at = now(), routineBatch = [], reset = false,
        cursor = eventCursor, taskEvidence = [], approvalEvidence = [], eventEvidence = []) {
      onFleet({ state: fleetState, residents: residentReport.residents,
        diagnosticResidents: residentReport.diagnosticResidents,
        diagnostics: residentReport.diagnostics, routineBatch, taskEvidence, approvalEvidence,
        jobState, approvalState, journalState, cursor, reset, eventEvidence,
        directoryAvailable: residentReport.available, villagers, transport, now: at });
    }

    function setTransport(next) {
      if (next === transport) return;
      transport = next;
      onTransport(next);
      publishFleet();
    }

    function combineSouls() {
      souls = residentSouls.concat(legacySouls);
    }

    function project(lines, reset = false, publishRoutineEvidence = true, grouped = false,
        deriveMood = true) {
      if (reset) { agents = new Map(); artifacts = []; fleetState = fleet.createFleetState();
        jobState = jobs.createState(); approvalState = approvals.createState();
        journalState = journals.createState(); moodSequence = 0; moodOrdinals = new Map();
        moodEvidenceByAgent = new Map(); moodApprovalsByRequest = new Map();
        moodAuthorityState = { events: [], ordinals: [], copies: [], rawOrdinals: [],
          overflow: false, observed: 0 }; }
      const windows = grouped ? projection.parseEventWindows(lines, MAX_TRANSPORT_EVENTS) : null;
      const batch = windows ? windows.tail : projection.parseEvents(lines);
      const journalBatch = windows ? windows.full : batch;
      const moodBatch = windows ? journalBatch : batch;
      updateMoodEvidence(moodBatch, Boolean(windows));
      // The strict v0 adapter owns parsing and validation for every browser
      // consumer. Passing its validated batch and task-only rejection metadata
      // keeps boards/approvals diagnostic without a second, weaker raw parser.
      jobs.foldValidated(jobState, batch, { isValidatedBatch: projection.isValidatedBatch,
        rejections: projection.taskRejections(journalBatch) });
      journals.foldValidated(journalState, journalBatch, {
        isValidatedBatch: projection.isValidatedBatch,
        rejections: projection.journalRejections(journalBatch),
      });
      const approvalBatch = windows ? approvals.lifecycleWindow(journalBatch,
        MAX_TRANSPORT_EVENTS, journals.records(journalState), {
          isValidatedBatch: projection.isValidatedBatch,
          validatedSelection: projection.validatedSelection,
        }) : batch;
      approvals.foldValidated(approvalState, approvalBatch, {
        isValidatedBatch: projection.isValidatedBatch,
        rejections: projection.approvalRejections(journalBatch),
        ordinalBatch: windows ? journalBatch : null,
        ordinalForEvent: windows ? event => journalState.ordinalForEvent(event) : null,
        sequence: windows ? journalState.sequence : null,
      });
      if (windows) {
        const retainedBeforeWindow = new Set(journals.records(journalState)
          .map(record => record.event));
        for (const event of approvalBatch) {
          const record = event.type === "needs_human" ?
            approvalState.recordForEvent(event) : null;
          if (record && !record.resolution && !record.collided) {
            retainedBeforeWindow.add(event);
          }
        }
        // A retained journal/knock must carry a later terminal ordinary fact:
        // after an incremental close it is what prevents resurrection.
        const specialAgents = new Set([...retainedBeforeWindow].map(event => event.agent_id));
        const latestOrdinary = new Map();
        for (const event of journalBatch) {
          if (specialAgents.has(event.agent_id) && event.type !== "journal_written" &&
              event.type !== "needs_human_resolved" &&
              !["task_posted", "task_claimed", "task_done", "task_failed"].includes(event.type)) {
            latestOrdinary.set(event.agent_id, event);
          }
        }
        for (const event of latestOrdinary.values()) {
          if (event.type === "session_ended") retainedBeforeWindow.add(event);
        }
        const protectedEvidence = projection.validatedSelection(journalBatch,
          journalBatch.filter(event => retainedBeforeWindow.has(event)));
        const retainedEvidence = projection.projectionWitnesses(
          journalBatch, now(), MAX_TRANSPORT_EVENTS, batch, specialAgents,
          protectedEvidence);
        // One bounded append-ordered projection composes canonical journals,
        // their pending exact requests, and ordinary/routine villager state.
        projection.foldEvents(agents, retainedEvidence, journalState);
      } else projection.foldEvents(agents, batch, journalState);
      projection.foldArtifacts(artifacts, batch);
      const at = now();
      fleet.foldFleet(fleetState, batch, windows ? windows.rejected : lines.length - batch.length,
        projection.routineRejections(journalBatch).length, at,
        event => journalState.ordinalForEvent(event));
      const previousMoodByAgent = deriveMood ? null :
        new Map(villagers.map(villager => [villager.id, villager.mood || null]));
      villagers = projection.reduce(agents, at, souls, approvalState, journalState);
      let moodByAgent = new Map();
      if (deriveMood && villagers.length) {
        // Derive from the complete retained authority. In particular, a
        // projected collision owner may depend on a canonical knock emitted by
        // a different, currently invisible agent.
        moodByAgent = moods.deriveMoods(moodEvidenceFor(
          new Set(villagers.map(villager => villager.id))));
      }
      for (const villager of villagers) villager.mood = deriveMood ?
        moodByAgent.get(villager.id) || null : previousMoodByAgent.get(villager.id) || null;
      onProjection({ villagers, artifacts, souls, approvalState, journalState, now: at });
      const taskEvents = batch.filter(event => jobs.TYPES.has(event.type));
      const approvalEvents = batch.filter(event => event.type === "needs_human_resolved");
      publishFleet(at, publishRoutineEvidence ?
        batch.filter(event => event.type.startsWith("routine_")) : [], reset, eventCursor,
        publishRoutineEvidence ? taskEvents : [], publishRoutineEvidence ? approvalEvents : [],
        publishRoutineEvidence ? batch : []);
      return batch;
    }

    function moodRequestId(event) {
      if (event.type === "needs_human") {
        const shape = approvals.classify(event);
        return shape.kind === "structured" ? shape.request_id : null;
      }
      return event.type === "needs_human_resolved" && event.payload ?
        event.payload.request_id : null;
    }

    function replaceMoodAgent(agentId, next) {
      const previous = moodEvidenceByAgent.get(agentId) || [];
      const retained = new Set(next);
      for (const event of previous) if (!retained.has(event)) moodOrdinals.delete(event);
      const oldIds = new Set(previous.map(moodRequestId).filter(Boolean));
      for (const requestId of oldIds) {
        const agents = moodApprovalsByRequest.get(requestId);
        if (!agents) continue;
        agents.delete(agentId);
        if (!agents.size) moodApprovalsByRequest.delete(requestId);
      }
      if (next.length) moodEvidenceByAgent.set(agentId, next);
      else moodEvidenceByAgent.delete(agentId);
      const grouped = new Map();
      for (const event of next) {
        const requestId = moodRequestId(event);
        if (!requestId) continue;
        const events = grouped.get(requestId) || [];
        events.push(event); grouped.set(requestId, events);
      }
      for (const [requestId, events] of grouped) {
        const agents = moodApprovalsByRequest.get(requestId) || new Map();
        agents.set(agentId, events); moodApprovalsByRequest.set(requestId, agents);
      }
    }

    function updateMoodEvidence(batch, grouped) {
      for (const event of batch) moodOrdinals.set(event, moodSequence++);
      if (grouped) {
        const retained = moods.retainMoodWitnesses(batch);
        moodAuthorityState = projection.moodAuthorityState(retained);
        const retainedOrder = moodAuthorityState.rawOrdinals.length === retained.length ?
          moodAuthorityState.rawOrdinals.map(Number) : retained.map(event => moodOrdinals.get(event));
        moodOrdinals = new Map(retained.map((event, index) => [event, retainedOrder[index]]));
        for (const value of retainedOrder) moodSequence = Math.max(moodSequence, value + 1);
        moodEvidenceByAgent = new Map(); moodApprovalsByRequest = new Map();
        const byAgent = new Map();
        for (const event of retained) {
          const events = byAgent.get(event.agent_id) || [];
          events.push(event); byAgent.set(event.agent_id, events);
        }
        for (const [agentId, events] of byAgent) replaceMoodAgent(agentId, events);
        return;
      }
      const additions = new Map();
      for (const event of batch) {
        const events = additions.get(event.agent_id) || [];
        events.push(event); additions.set(event.agent_id, events);
      }
      // Capsule authority remains one atomic global fold, while signal
      // witnesses stay per-agent. Remapping the capsule to each local source
      // gives it fresh raw coordinates; filtering the result by its real owner
      // prevents a cross-agent dependency from being assigned to the changed
      // agent. This preserves linear incremental ingestion for wide fleets.
      for (const [agentId, events] of additions) {
        const previous = moodEvidenceByAgent.get(agentId) || [];
        const ordered = [...previous, ...events].sort((left, right) =>
          moodOrdinals.get(left) - moodOrdinals.get(right));
        const globalOrdinalByEvent = new Map(moodAuthorityState.events.map((event, index) =>
          [event, moodAuthorityState.ordinals[index]]));
        for (const event of ordered) globalOrdinalByEvent.set(event, String(moodOrdinals.get(event)));
        const source = projection.parseEvents(ordered);
        const retained = moods.retainMoodWitnesses(remappedMoodAuthority(
          source, ordered, previous.length));
        moodAuthorityState = globalMoodAuthorityState(retained,
          projection.moodAuthorityState(retained), globalOrdinalByEvent);
        const ordinalByRetained = new Map(retained.map(event =>
          [event, Number(globalOrdinalByEvent.get(event))]));
        const owned = retained.filter(event => event.agent_id === agentId);
        const retainedSet = new Set(owned);
        for (const event of ordered) if (!retainedSet.has(event)) moodOrdinals.delete(event);
        for (const event of owned) moodOrdinals.set(event, ordinalByRetained.get(event));
        replaceMoodAgent(agentId, owned);
      }
    }

    function remappedMoodAuthority(source, ordered, sourceEpochCount = ordered.length) {
      if (moodAuthorityState.overflow) {
        return projection.withMoodAuthority(source, ordered, [], [], {
          ordinals: [], rawOrdinals: [], overflow: true,
          observed: moodAuthorityState.observed });
      }
      const authorityByOrdinal = new Map(moodAuthorityState.ordinals.map((ordinal, index) =>
        [ordinal, moodAuthorityState.events[index]]));
      // The carried capsule describes the prior source epoch. Newly received
      // records are append-after evidence and must be folded only after that
      // capsule has independently proved its prior canonical authority.
      const sourceEpoch = ordered.slice(0, sourceEpochCount);
      const proof = moods.requiredMoodRawIndexes(
        sourceEpoch, moodAuthorityState.events);
      if (proof.witnessOverflow) {
        return projection.withMoodAuthority(source, ordered, [], [], {
          ordinals: [], rawOrdinals: [], rawIndexes: [],
          rawCount: "0000000000000000", overflow: true,
          observed: moods.MAX_AUTHORITY_EVENTS + 1 });
      }
      const requiredIndexes = proof.indexes;
      const rawOrdinals = requiredIndexes.map(index => String(moodOrdinals.get(ordered[index])));
      const rawIndexes = requiredIndexes.map(index => String(index).padStart(16, "0"));
      const copies = rawOrdinals.filter((ordinal, index) => authorityByOrdinal.has(ordinal) &&
        projection.capsuleIdentityEqual(authorityByOrdinal.get(ordinal),
          ordered[requiredIndexes[index]]));
      return projection.withMoodAuthority(source, ordered, moodAuthorityState.events, copies, {
        ...moodAuthorityState, rawOrdinals, rawIndexes,
        rawCount: String(sourceEpochCount).padStart(16, "0"), copies });
    }

    function globalMoodAuthorityState(retained, local, globalOrdinalByEvent) {
      if (local.overflow) return { events: [], ordinals: [], copies: [], rawOrdinals: [],
        rawIndexes: [], rawCount: "0000000000000000", overflow: true,
        observed: local.observed };
      const localAuthorityByOrdinal = new Map(local.ordinals.map((ordinal, index) =>
        [ordinal, local.events[index]]));
      const copiedEvents = new Set(local.copies.map(ordinal => localAuthorityByOrdinal.get(ordinal)));
      const ordinals = local.events.map(event => globalOrdinalByEvent.get(event));
      const requiredIndexes = local.rawIndexes.map(Number);
      const rawOrdinals = requiredIndexes.map(index => globalOrdinalByEvent.get(retained[index]));
      const copies = local.events.filter(event => copiedEvents.has(event)).map(event =>
        globalOrdinalByEvent.get(event));
      const rawIndexes = local.rawIndexes.slice();
      const rawCount = String(retained.length).padStart(16, "0");
      const translated = { events: local.events, ordinals, copies, rawOrdinals, rawIndexes,
        rawCount, overflow: false, observed: local.observed };
      if (projection.moodAuthorityCapsuleByteLength(local.events, copies, translated) >
          moods.MAX_AUTHORITY_BYTES) {
        return { events: [], ordinals: [], copies: [], rawOrdinals: [], rawIndexes: [],
          rawCount: "0000000000000000", overflow: true,
          observed: moods.MAX_AUTHORITY_EVENTS + 1 };
      }
      return translated;
    }

    function moodEvidenceFor(projectedAgents) {
      const selected = new Set();
      const requestIds = new Set();
      for (const agentId of projectedAgents) {
        for (const event of moodEvidenceByAgent.get(agentId) || []) {
          selected.add(event);
          const requestId = moodRequestId(event);
          if (requestId) requestIds.add(requestId);
        }
      }
      // Approval collision truth is global by request ID, independent of the
      // approval panel's owner and capacity. Pull only connected lifecycle
      // members, not every invisible agent's unrelated Mood evidence.
      for (const requestId of requestIds) {
        const agents = moodApprovalsByRequest.get(requestId);
        if (!agents) continue;
        for (const events of agents.values()) for (const event of events) selected.add(event);
      }
      const ordered = [...selected].sort((left, right) =>
        moodOrdinals.get(left) - moodOrdinals.get(right));
      const source = projection.parseEvents(ordered);
      return remappedMoodAuthority(source, ordered);
    }

    function tick() { project([]); }

    function refreshResidents() {
      if (residentPromise) return residentPromise;
      residentPromise = (async () => {
        function markUnavailable() {
          residentReport = { ...residentReport, available: false };
          project([]);
        }
        try {
          const response = await fetch("/residents", { cache: "no-store" });
          if (!response.ok) { markUnavailable(); return; }
          const report = await response.json();
          if (!report || !Array.isArray(report.residents)) { markUnavailable(); return; }
          residentReport = { residents: report.residents,
            diagnosticResidents: Array.isArray(report.diagnostic_residents) ?
              report.diagnostic_residents : [],
            diagnostics: Array.isArray(report.diagnostics) ? report.diagnostics : [],
            available: true };
          residentSouls = report.residents;
          combineSouls();
          const nextDiagnostics = new Set();
          for (const diagnostic of report.diagnostics || []) {
            const message = `resident manifest ${diagnostic.file}${diagnostic.path}: ${diagnostic.message}`;
            nextDiagnostics.add(message);
            if (!reportedResidentDiagnostics.has(message)) warn(message);
          }
          reportedResidentDiagnostics = nextDiagnostics;
          project([]);
        } catch { markUnavailable(); }
      })().finally(() => { residentPromise = null; });
      return residentPromise;
    }

    function refreshTransportStatus() {
      if (transportStatusPromise) return transportStatusPromise;
      transportStatusPromise = (async () => {
        try {
          const response = await fetch("/transport/status", { cache: "no-store" });
          if (!response.ok) return;
          const report = await response.json();
          if (!report || typeof report !== "object") return;
          transportStatus = report;
          onTransportStatus(report);
        } catch {}
      })().finally(() => { transportStatusPromise = null; });
      return transportStatusPromise;
    }

    function poll() {
      if (pollPromise) return pollPromise;
      if (eventStream || streamReady) return Promise.resolve();
      // A cached cursor says where the last successful read ended, not that a
      // new read is observable. Keep the transport non-observable until this
      // exact request has published its /events response.
      setTransport("recovering");
      pollPromise = (async () => {
        try {
          const [evRes, soulRes] = await Promise.all([
            fetch("/events?since=" + eventCursor, { cache: "no-store" }),
            fetch("/villagers", { cache: "no-store" }).catch(() => null),
          ]);
          if (!evRes.ok) throw new Error("events request failed");
          const text = await evRes.text();
          const reset = evRes.headers.get("X-Burrow-Reset") === "1";
          const lines = text.split("\n").filter(Boolean);
          const previousCursor = eventCursor;
          eventCursor = evRes.headers.get("X-Burrow-Cursor") || 0;
          project(lines, reset, true, previousCursor === 0 || reset);
          setTransport("polling");
          if (soulRes && soulRes.ok) {
            try {
              const loaded = await soulRes.json();
              if (Array.isArray(loaded)) {
                residentSouls = loaded.filter(soul => soul && soul.valid === true &&
                  soul.manifest_version === 1);
                legacySouls = loaded.filter(soul => soul && (soul.valid !== true ||
                  soul.manifest_version !== 1));
                combineSouls();
              }
            } catch {}
          }
          project([]);
        } catch {
          setTransport("disconnected");
        }
      })().finally(() => { pollPromise = null; });
      return pollPromise;
    }

    function connectStream() {
      if (eventStream || !EventSource) return;
      setTransport("reconnecting");
      if (pollPromise) {
        pollPromise.finally(() => {
          if (!eventStream) connectStream();
        });
        return;
      }
      let stream;
      try {
        stream = new EventSource("/events/stream?since=" + encodeURIComponent(eventCursor));
      } catch {
        setTransport("disconnected");
        return;
      }
      eventStream = stream;
      const stageEligible = eventCursor !== 0;
      let stagedPublications = [];
      let stagedRecordCount = 0;
      let stagingOverflowed = false;
      let moodDeferred = false;
      function clearStaging() {
        stagedPublications = [];
        stagedRecordCount = 0;
        stagingOverflowed = false;
      }
      function publishStaging() {
        if (!stageEligible || stagingOverflowed) return false;
        for (const staged of stagedPublications) {
          // The validated batch is the one staging authority. Consumer slices
          // are derived only at publication so one record consumes one slot,
          // regardless of how many acknowledgement views need that record.
          publishFleet(now(), staged.batch.filter(event => event.type.startsWith("routine_")),
            false, staged.cursor, staged.batch.filter(event => jobs.TYPES.has(event.type)),
            staged.batch.filter(event => event.type === "needs_human_resolved"), staged.batch);
        }
        return true;
      }
      function publishOrConservativeRebase() {
        // A pre-ready cursor may already be past evidence that a subsequent
        // grouped poll cannot return. Publish every retained exact record, or
        // explicitly invalidate pending correlations when bounded staging
        // cannot prove what was crossed. Projection state is deliberately left
        // intact: `reset` here describes correlation authority, not the board.
        if (!publishStaging()) publishFleet(now(), [], true, eventCursor);
        clearStaging();
      }
      function flushDeferredMood() {
        if (!moodDeferred) return;
        // Queued records already changed the core projection. Decorate that
        // exact retained state once before any ready-less transport status can
        // publish it, and once at a successful ready boundary.
        moodDeferred = false;
        project([], false, false);
      }
      async function recoverStream() {
        flushDeferredMood();
        publishOrConservativeRebase();
        stream.close();
        eventStream = null;
        streamReady = false;
        setTransport("reconnecting");
        await poll();
        if (transport === "polling") setTransport("reconnecting");
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connectStream, 2000);
      }
      stream.onopen = () => {
        if (eventStream !== stream) return;
        // EventSource open only confirms HTTP framing. Queued replay records
        // still follow it, so transport remains non-observable until Burrow's
        // ordered, cursor-bearing readiness marker arrives.
        setTransport("recovering");
      };
      stream.onmessage = message => {
        if (eventStream !== stream) return;
        if (message.lastEventId) eventCursor = message.lastEventId;
        // Projection remains immediate: a real queued close can release a
        // knock before readiness even though acknowledgement publication is
        // staged. Mood is the expensive whole-history calculation, so defer
        // only that decoration until the ready marker closes the catch-up.
        const batch = project([message.data], false, streamReady, false, streamReady);
        if (!streamReady && batch.length) moodDeferred = true;
        if (streamReady || !stageEligible || stagingOverflowed) return;
        if (!batch.length) return;
        if (stagedRecordCount + batch.length > MAX_TRANSPORT_EVENTS) {
          stagedPublications = [];
          stagedRecordCount = 0;
          stagingOverflowed = true;
          return;
        }
        stagedPublications.push({ batch, cursor: message.lastEventId || 0 });
        stagedRecordCount += batch.length;
      };
      stream.addEventListener("ready", async message => {
        if (eventStream !== stream || streamReady) return;
        let declared;
        try { declared = JSON.parse(message.data).cursor; } catch { declared = null; }
        const exact = typeof declared === "string" && declared === message.lastEventId;
        const readyCursor = exact ? routines.parseCursor(declared) : null;
        const previousCursor = eventCursor === 0 ? null : routines.parseCursor(eventCursor);
        if (!readyCursor || (eventCursor !== 0 && (!previousCursor ||
            previousCursor.namespace !== readyCursor.namespace)) ||
            (eventCursor !== 0 && eventCursor !== declared)) {
          await recoverStream();
          return;
        }
        eventCursor = declared;
        // Recompute the whole retained Mood once at the exact boundary. Queued
        // records already updated core villager state as they arrived.
        flushDeferredMood();
        if (!stageEligible || stagingOverflowed) {
          // Bootstrap/reset-like catch-up and bounded-stage overflow cannot be
          // attributed safely. Rebase pending correlations at the validated
          // ending cursor and consider only evidence that follows it.
          publishFleet(now(), [], true, declared);
        } else {
          publishStaging();
        }
        clearStaging();
        streamReady = true;
        setTransport("live");
      });
      stream.addEventListener("reset", async () => {
        if (eventStream !== stream) return;
        flushDeferredMood();
        clearStaging();
        // An SSE reset deliberately has no id: the stream cannot describe the
        // end of the replay that follows it. Close it and use the grouped poll
        // response to establish one unambiguous baseline and ending cursor.
        // Keeping the rejected cursor lets /events report the generation reset.
        stream.close();
        eventStream = null;
        streamReady = false;
        setTransport("reconnecting");
        await poll();
        if (eventStream) return;
        if (transport === "polling") setTransport("reconnecting");
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connectStream, 2000);
      });
      stream.onerror = async () => {
        if (eventStream !== stream) return;
        // Every staged record already passed strict validation and carries an
        // exact cursor after the stream's known starting boundary. If the
        // connection dies before `ready`, publish that evidence before polling
        // from the advanced cursor; otherwise an accepted write can be visible
        // on the board yet its exact acknowledgement is lost forever.
        await recoverStream();
      };
    }

    function snapshot() {
      return { villagers, artifacts, souls, cursor: eventCursor, transport,
        transportStatus, fleetState, jobState, approvalState, journalState, residentReport,
        moodAuthority: { retained: moodAuthorityState.events.length,
          overflow: moodAuthorityState.overflow,
          observed: moodAuthorityState.observed } };
    }

    onTransport(transport);
    return { poll, connectStream, refreshResidents, refreshTransportStatus,
      tick, snapshot };
  }

  return { createBrowserRuntime };
});
