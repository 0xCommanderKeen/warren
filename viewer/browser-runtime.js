"use strict";

/* The browser's deep runtime module. EventSource, fetch and wall time are the
 * true external seams; both the page and tests use this same interface. */
(function (root, factory) {
  const projection = typeof module === "object" && module.exports
    ? require("./projection.js")
    : { parseEvents, foldEvents, foldArtifacts, reduce };
  const fleet = typeof module === "object" && module.exports
    ? require("./fleet-operations.js") : root.BurrowFleet;
  const runtime = factory(projection, fleet);
  if (typeof module === "object" && module.exports) module.exports = runtime;
  else root.BurrowBrowser = runtime;
})(typeof globalThis === "object" ? globalThis : this, function (projection, fleet) {
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

    function publishFleet(at = now()) {
      onFleet({ state: fleetState, residents: residentReport.residents,
        diagnosticResidents: residentReport.diagnosticResidents,
        diagnostics: residentReport.diagnostics,
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

    function project(lines, reset = false) {
      if (reset) { agents = new Map(); artifacts = []; fleetState = fleet.createFleetState(); }
      const batch = projection.parseEvents(lines);
      projection.foldEvents(agents, batch);
      projection.foldArtifacts(artifacts, batch);
      fleet.foldFleet(fleetState, batch, lines.length - batch.length);
      const at = now();
      villagers = projection.reduce(agents, at, souls);
      onProjection({ villagers, artifacts, souls, now: at });
      publishFleet(at);
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
      setTransport("polling");
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
          project(eventCursor === 0 || reset ? lines.slice(-4000) : lines, reset);
          eventCursor = evRes.headers.get("X-Burrow-Cursor") || 0;
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
      stream.onopen = () => {
        if (eventStream !== stream) return;
        streamReady = true;
        setTransport("live");
      };
      stream.onmessage = message => {
        if (eventStream !== stream) return;
        streamReady = true;
        setTransport("live");
        if (message.lastEventId) eventCursor = message.lastEventId;
        project([message.data]);
      };
      stream.addEventListener("reset", () => {
        if (eventStream !== stream) return;
        eventCursor = 0;
        project([], true);
      });
      stream.onerror = async () => {
        if (eventStream !== stream) return;
        stream.close();
        eventStream = null;
        streamReady = false;
        setTransport("reconnecting");
        await poll();
        setTransport("reconnecting");
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
