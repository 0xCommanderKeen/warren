"use strict";

/* The browser's deep runtime module. EventSource, fetch and wall time are the
 * true external seams; both the page and tests use this same interface. */
(function (root, factory) {
  const projection = typeof module === "object" && module.exports
    ? require("./projection.js")
    : { parseEvents, routineRejections, foldEvents, foldArtifacts, reduce };
  const fleet = typeof module === "object" && module.exports
    ? require("./fleet-operations.js") : root.BurrowFleet;
  const runtime = factory(projection, fleet);
  if (typeof module === "object" && module.exports) module.exports = runtime;
  else root.BurrowBrowser = runtime;
})(typeof globalThis === "object" ? globalThis : this, function (projection, fleet) {
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
    let residentReport = { residents: [], diagnosticResidents: [], diagnostics: [], available: false };

    function publishFleet(at = now(), routineBatch = [], reset = false,
        cursor = eventCursor) {
      onFleet({ state: fleetState, residents: residentReport.residents,
        diagnosticResidents: residentReport.diagnosticResidents,
        diagnostics: residentReport.diagnostics, routineBatch, cursor, reset,
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

    function project(lines, reset = false, publishRoutineEvidence = true) {
      if (reset) { agents = new Map(); artifacts = []; fleetState = fleet.createFleetState(); }
      const batch = projection.parseEvents(lines);
      projection.foldEvents(agents, batch);
      projection.foldArtifacts(artifacts, batch);
      const at = now();
      fleet.foldFleet(fleetState, batch, lines.length - batch.length,
        projection.routineRejections(batch).length, at);
      villagers = projection.reduce(agents, at, souls);
      onProjection({ villagers, artifacts, souls, now: at });
      publishFleet(at, publishRoutineEvidence ?
        batch.filter(event => event.type.startsWith("routine_")) : [], reset);
      return batch;
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
          project(previousCursor === 0 || reset ?
            lines.slice(-MAX_TRANSPORT_EVENTS) : lines, reset);
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
      let stagedRoutineEvidence = [];
      let stagingOverflowed = false;
      function clearStaging() {
        stagedRoutineEvidence = [];
        stagingOverflowed = false;
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
        const batch = project([message.data], false, streamReady);
        if (streamReady || !stageEligible || stagingOverflowed) return;
        const evidence = batch.filter(event => event.type.startsWith("routine_"));
        if (!evidence.length) return;
        if (stagedRoutineEvidence.length + evidence.length > MAX_TRANSPORT_EVENTS) {
          stagedRoutineEvidence = [];
          stagingOverflowed = true;
          return;
        }
        stagedRoutineEvidence.push({ events: evidence, cursor: message.lastEventId || 0 });
      };
      stream.addEventListener("ready", async message => {
        if (eventStream !== stream || streamReady) return;
        let declared;
        try { declared = JSON.parse(message.data).cursor; } catch { declared = null; }
        const exact = typeof declared === "string" && declared === message.lastEventId;
        const parseCursor = value => {
          const match = typeof value === "string" &&
            value.match(/^v1:([0-9a-f]{32}):(\d+):(\d+):(\d+):(\d+)$/);
          if (!match) return null;
          const limit = 18446744073709551615n;
          for (const field of match.slice(2)) {
            const integer = BigInt(field);
            if (integer > limit || String(integer) !== field) return null;
          }
          return { namespace: ["v1", ...match.slice(1, 5)].join(":") };
        };
        const readyCursor = exact ? parseCursor(declared) : null;
        const previousCursor = eventCursor === 0 ? null : parseCursor(eventCursor);
        if (!readyCursor || (eventCursor !== 0 && (!previousCursor ||
            previousCursor.namespace !== readyCursor.namespace)) ||
            (eventCursor !== 0 && eventCursor !== declared)) {
          clearStaging();
          stream.close();
          eventStream = null;
          streamReady = false;
          setTransport("reconnecting");
          await poll();
          if (transport === "polling") setTransport("reconnecting");
          clearTimeout(reconnectTimer);
          reconnectTimer = setTimeout(connectStream, 2000);
          return;
        }
        eventCursor = declared;
        if (!stageEligible || stagingOverflowed) {
          // Bootstrap/reset-like catch-up and bounded-stage overflow cannot be
          // attributed safely. Rebase pending correlations at the validated
          // ending cursor and consider only evidence that follows it.
          publishFleet(now(), [], true, declared);
        } else {
          for (const staged of stagedRoutineEvidence) {
            publishFleet(now(), staged.events, false, staged.cursor);
          }
        }
        clearStaging();
        streamReady = true;
        setTransport("live");
      });
      stream.addEventListener("reset", async () => {
        if (eventStream !== stream) return;
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
        clearStaging();
        stream.close();
        eventStream = null;
        streamReady = false;
        setTransport("reconnecting");
        await poll();
        if (transport === "polling") setTransport("reconnecting");
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connectStream, 2000);
      };
    }

    function snapshot() {
      return { villagers, artifacts, souls, cursor: eventCursor, transport,
        transportStatus, fleetState, residentReport };
    }

    onTransport(transport);
    return { poll, connectStream, refreshResidents, refreshTransportStatus,
      tick, snapshot };
  }

  return { createBrowserRuntime };
});
