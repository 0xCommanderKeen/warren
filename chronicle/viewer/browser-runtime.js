"use strict";
(function (root, factory) {
  const transport = typeof module === "object" && module.exports ? require("./state-transport.js") : root.BurrowStateTransport;
  const api = factory(transport);
  if (typeof module === "object" && module.exports) module.exports = api; else root.BurrowBrowser = api;
})(typeof globalThis === "object" ? globalThis : this, function (transportApi) {
  function createBrowserRuntime(options) {
    let view = null, status = "disconnected", transportStatus = null;
    const adaptSnapshot = options.adaptSnapshot || (snapshot => snapshot);
    const baseUrl = String(options.baseUrl || "").replace(/\/+$/, "");
    const client = transportApi.createStateTransport({ fetch: options.fetch, EventSource: options.EventSource,
      baseUrl,
      warn: options.warn, onStatus(value) { status = value; if (view) view.transport = value;
        options.onTransport && options.onTransport(value); },
      onState(snapshot, meta) { view = { ...adaptSnapshot(snapshot), transport: status, reset: meta.reset };
        options.onProjection && options.onProjection(view); options.onFleet && options.onFleet(view); } });
    async function refreshTransportStatus() { try {
      const response = await options.fetch(baseUrl + "/transport/status", { cache: "no-store" });
      if (response.status === 200) { transportStatus = await response.json();
        options.onTransportStatus && options.onTransportStatus(transportStatus); }
    } catch (_) {} }
    return { poll: client.poll, connectStream: client.connect, refreshResidents() {},
      refreshTransportStatus, tick() {}, snapshot() { return view ? { ...view, transport: status, transportStatus } : { transport: status }; } };
  }
  return { createBrowserRuntime };
});
