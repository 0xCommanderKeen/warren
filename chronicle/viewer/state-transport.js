"use strict";

/* Complete Village State transport.  This module validates ordering metadata
 * and swaps snapshots atomically; it deliberately knows nothing about events. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowStateTransport = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  const COLLECTIONS = ["villagers", "residents", "diagnostic_residents", "artifacts",
    "tasks", "approvals", "journals", "routines", "diagnostics"];

  function validateSnapshot(value) {
    if (!value || Object.getPrototypeOf(value) !== Object.prototype) return "snapshot must be an object";
    if (value.schema_version !== 1) return "unsupported snapshot schema";
    if (!Number.isSafeInteger(value.generation) || value.generation < 0) return "invalid generation";
    if (!Number.isSafeInteger(value.log_generation) || value.log_generation < 0) return "invalid log generation";
    if (typeof value.cursor !== "string") return "invalid cursor";
    if (typeof value.evaluated_at !== "string" || !Number.isFinite(Date.parse(value.evaluated_at))) {
      return "invalid evaluation time";
    }
    for (const key of COLLECTIONS) if (!Array.isArray(value[key])) return `invalid ${key}`;
    if (!value.capacity || typeof value.capacity !== "object") return "invalid capacity";
    if (!value.capabilities || typeof value.capabilities !== "object") return "invalid capabilities";
    return null;
  }

  function validateEnvelope(value) {
    if (!value || !["snapshot", "reset"].includes(value.kind)) return "invalid envelope kind";
    return validateSnapshot(value.snapshot);
  }

  function createStateTransport(options) {
    const fetch = options.fetch;
    const EventSource = options.EventSource;
    const baseUrl = String(options.baseUrl || "").replace(/\/+$/, "");
    const onState = options.onState || (() => {});
    const onStatus = options.onStatus || (() => {});
    const warn = options.warn || (() => {});
    let current = null, status = "disconnected", stream = null, polling = null;
    const retiredNamespaces = new Set();

    function cursorNamespace(cursor) {
      const parts = String(cursor).split(":");
      return parts.length === 6 && parts[0] === "v1" ? parts.slice(0, 5).join(":") : null;
    }

    function setStatus(value) {
      if (status === value) return;
      status = value; onStatus(value);
    }

    function apply(envelope) {
      const error = validateEnvelope(envelope);
      if (error) { warn(error); return false; }
      const next = envelope.snapshot;
      if (current) {
        const currentNamespace = cursorNamespace(current.cursor);
        const nextNamespace = cursorNamespace(next.cursor);
        if (envelope.kind !== "reset" || currentNamespace === nextNamespace) {
          if (next.generation <= current.generation) return false;
        } else {
          if (nextNamespace && retiredNamespaces.has(nextNamespace)) return false;
          if (currentNamespace) retiredNamespaces.add(currentNamespace);
        }
      }
      current = next;
      onState(current, { reset: envelope.kind === "reset" });
      return true;
    }

    async function poll() {
      if (polling) return polling;
      polling = (async () => {
        const query = current ? `?generation=${current.generation}&cursor=${encodeURIComponent(current.cursor)}` : "";
        try {
          const response = await fetch(baseUrl + "/state" + query, { cache: "no-store" });
          if (response.status === 204) { setStatus(stream ? "live" : "polling"); return; }
          if (!response || response.status !== 200) throw new Error(`state HTTP ${response && response.status}`);
          apply(await response.json());
          setStatus(stream ? "live" : "polling");
        } catch (error) {
          setStatus("disconnected"); warn(String(error));
        }
      })();
      try { await polling; } finally { polling = null; }
    }

    function connect() {
      if (!EventSource || stream) return;
      const query = current ? `?generation=${current.generation}&cursor=${encodeURIComponent(current.cursor)}` : "";
      const candidate = new EventSource(baseUrl + "/state/stream" + query);
      stream = candidate; setStatus("reconnecting");
      candidate.addEventListener("snapshot", message => {
        if (stream !== candidate) return;
        try { if (apply(JSON.parse(message.data))) setStatus("live"); }
        catch (error) { warn(`invalid state stream: ${error}`); }
      });
      candidate.onerror = async () => {
        if (stream !== candidate) return;
        candidate.close(); stream = null; setStatus("reconnecting");
        await poll();
        if (!stream) connect();
      };
    }

    function close() { if (stream) stream.close(); stream = null; setStatus("disconnected"); }
    function snapshot() { return current; }
    onStatus(status);
    return { poll, connect, close, snapshot, apply };
  }

  return { validateSnapshot, validateEnvelope, createStateTransport };
});
