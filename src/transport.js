const COLLECTIONS = [
  "villagers", "residents", "diagnostic_residents", "artifacts", "tasks",
  "approvals", "journals", "routines", "diagnostics",
];

export function validateSnapshot(value) {
  if (!value || Object.getPrototypeOf(value) !== Object.prototype) return "snapshot must be an object";
  if (value.schema_version !== 1) return "unsupported snapshot schema";
  if (!Number.isSafeInteger(value.generation) || value.generation < 0) return "invalid generation";
  if (!Number.isSafeInteger(value.log_generation) || value.log_generation < 0) return "invalid log generation";
  if (typeof value.cursor !== "string") return "invalid cursor";
  if (typeof value.evaluated_at !== "string" || !Number.isFinite(Date.parse(value.evaluated_at))) return "invalid evaluation time";
  for (const key of COLLECTIONS) if (!Array.isArray(value[key])) return `invalid ${key}`;
  if (!value.capacity || typeof value.capacity !== "object") return "invalid capacity";
  if (!value.capabilities || typeof value.capabilities !== "object") return "invalid capabilities";
  return null;
}

export function createStateTransport(options) {
  const baseUrl = String(options.baseUrl || "").replace(/\/+$/, "");
  let current = null;
  let status = "disconnected";
  let stream = null;
  let polling = null;
  const retiredNamespaces = new Set();
  const setStatus = (next) => {
    if (status !== next) {
      status = next;
      options.onStatus?.(next);
    }
  };
  const namespace = (cursor) => {
    const parts = String(cursor).split(":");
    return parts.length === 6 && parts[0] === "v1" ? parts.slice(0, 5).join(":") : null;
  };
  const apply = (envelope) => {
    if (!envelope || !["snapshot", "reset"].includes(envelope.kind)) return false;
    const error = validateSnapshot(envelope.snapshot);
    if (error) {
      options.warn?.(error);
      return false;
    }
    const next = envelope.snapshot;
    if (current) {
      const currentNamespace = namespace(current.cursor);
      const nextNamespace = namespace(next.cursor);
      if (envelope.kind !== "reset" || currentNamespace === nextNamespace) {
        if (next.generation <= current.generation) return false;
      } else {
        if (nextNamespace && retiredNamespaces.has(nextNamespace)) return false;
        if (currentNamespace) retiredNamespaces.add(currentNamespace);
      }
    }
    current = next;
    options.onState?.(next);
    return true;
  };
  const poll = async () => {
    if (polling) return polling;
    polling = (async () => {
      const query = current ? `?generation=${current.generation}&cursor=${encodeURIComponent(current.cursor)}` : "";
      try {
        const response = await options.fetch(`${baseUrl}/state${query}`, { cache: "no-store" });
        if (response.status === 204) return setStatus(stream ? "live" : "polling");
        if (response.status !== 200) throw new Error(`state HTTP ${response.status}`);
        apply(await response.json());
        setStatus(stream ? "live" : "polling");
      } catch (error) {
        setStatus("disconnected");
        options.warn?.(String(error));
      }
    })();
    try { await polling; } finally { polling = null; }
  };
  const connect = () => {
    if (!options.EventSource || stream) return;
    const query = current ? `?generation=${current.generation}&cursor=${encodeURIComponent(current.cursor)}` : "";
    const candidate = new options.EventSource(`${baseUrl}/state/stream${query}`);
    stream = candidate;
    setStatus("reconnecting");
    candidate.addEventListener("snapshot", (message) => {
      if (stream !== candidate) return;
      try {
        if (apply(JSON.parse(message.data))) setStatus("live");
      } catch (error) {
        options.warn?.(`invalid state stream: ${error}`);
      }
    });
    candidate.onerror = async () => {
      if (stream !== candidate) return;
      candidate.close();
      stream = null;
      setStatus("reconnecting");
      await poll();
      if (!stream) connect();
    };
  };
  const close = () => {
    stream?.close();
    stream = null;
    setStatus("disconnected");
  };
  options.onStatus?.(status);
  return { apply, close, connect, poll, snapshot: () => current };
}
