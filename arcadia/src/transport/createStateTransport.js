import { parseSnapshot } from "../contract/parseSnapshot.js";

function trimTrailingSlashes(value) {
  return String(value || "").replace(/\/+$/, "");
}

function resumeQuery(snapshot) {
  if (!snapshot) return "";
  return `?generation=${snapshot.generation}&cursor=${encodeURIComponent(snapshot.cursor)}`;
}

function cursorNamespace(cursor) {
  const parts = String(cursor).split(":");
  return parts.length === 6 && parts[0] === "v1" ? parts.slice(0, 5).join(":") : null;
}

export function createStateTransport({
  fetch,
  EventSource,
  baseUrl = "",
  onEnvelope = () => {},
  onStatus = () => {},
  onError = () => {},
  random = Math.random,
  retryBaseMs = 1_000,
  retryMaxMs = 30_000,
}) {
  if (!Number.isFinite(retryBaseMs) || retryBaseMs <= 0) {
    throw new RangeError("retryBaseMs must be a finite positive number");
  }
  if (!Number.isFinite(retryMaxMs) || retryMaxMs <= 0) {
    throw new RangeError("retryMaxMs must be a finite positive number");
  }
  if (retryMaxMs < retryBaseMs) {
    throw new RangeError("retryMaxMs must be greater than or equal to retryBaseMs");
  }
  const backend = trimTrailingSlashes(baseUrl);
  let currentEnvelope = null;
  let stream = null;
  let stopped = false;
  const requests = new Set();
  let retryTimer = null;
  let consecutiveFailures = 0;
  const retiredNamespaces = new Set();

  function resetRetryDelay() {
    consecutiveFailures = 0;
  }

  function scheduleReconnect() {
    if (stopped || stream || retryTimer) return;
    const ceiling = Math.min(retryMaxMs, retryBaseMs * (2 ** consecutiveFailures));
    consecutiveFailures += 1;
    let sample = 0;
    try {
      sample = Number(random());
    } catch (error) {
      reportError(error);
    }
    const jitter = Number.isFinite(sample) ? Math.max(0, Math.min(1, sample)) : 0;
    const delay = Math.max(1, Math.floor((ceiling / 2) + ((ceiling / 2) * jitter)));
    if (stopped) return;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      connect();
    }, delay);
  }

  function reportError(error) {
    if (stopped) return;
    try {
      onError(error instanceof Error ? error : new Error(String(error)));
    } catch {
      // Reporting is best-effort: an observer cannot own the transport lifecycle.
    }
  }

  function reportStatus(status) {
    if (stopped && status !== "disconnected") return;
    try {
      onStatus(status);
    } catch (error) {
      reportError(error);
    }
  }

  function apply(envelope) {
    let nextSnapshot;
    try {
      nextSnapshot = parseSnapshot(envelope);
    } catch (error) {
      reportError(error);
      return { valid: false, changed: false };
    }

    const currentSnapshot = currentEnvelope?.snapshot;
    if (currentSnapshot) {
      const currentNamespace = cursorNamespace(currentSnapshot.cursor);
      const nextNamespace = cursorNamespace(nextSnapshot.cursor);
      const changesNamespace = envelope.kind === "reset" && currentNamespace !== nextNamespace;

      if (!changesNamespace && nextSnapshot.generation <= currentSnapshot.generation) {
        return { valid: true, changed: false };
      }
      if (changesNamespace) {
        if (nextNamespace && retiredNamespaces.has(nextNamespace)) {
          return { valid: true, changed: false };
        }
        if (currentNamespace) retiredNamespaces.add(currentNamespace);
      }
    }

    currentEnvelope = envelope;
    try {
      onEnvelope(envelope);
    } catch (error) {
      reportError(error);
    }
    return { valid: true, changed: true };
  }

  async function poll() {
    if (stopped) return;
    const controller = new AbortController();
    requests.add(controller);
    try {
      const response = await fetch(
        `${backend}/state${resumeQuery(currentEnvelope?.snapshot)}`,
        { cache: "no-store", signal: controller.signal },
      );
      if (stopped) return;
      if (response.status === 204) {
        resetRetryDelay();
        return;
      }
      if (response.status !== 200) throw new Error(`State request failed: HTTP ${response.status}`);
      const envelope = await response.json();
      if (stopped) return;
      const result = apply(envelope);
      if (result.valid) resetRetryDelay();
    } catch (error) {
      if (stopped) return;
      reportError(error);
      throw error;
    } finally {
      requests.delete(controller);
    }
  }

  function connect() {
    if (stopped || stream || !EventSource) return;
    const candidate = new EventSource(
      `${backend}/state/stream${resumeQuery(currentEnvelope?.snapshot)}`,
    );
    stream = candidate;
    reportStatus("reconnecting");

    candidate.onopen = () => {
      if (stream !== candidate) return;
      resetRetryDelay();
      reportStatus("live");
    };

    const receive = (message) => {
      if (stream !== candidate) return;
      try {
        if (apply(JSON.parse(message.data)).changed) {
          resetRetryDelay();
          reportStatus("live");
        }
      } catch (error) {
        reportError(new Error(`Invalid state stream: ${error.message}`));
      }
    };
    candidate.addEventListener("snapshot", receive);
    candidate.addEventListener("reset", receive);
    candidate.onerror = async () => {
      if (stream !== candidate) return;
      candidate.close();
      stream = null;
      reportStatus("reconnecting");
      try {
        await poll();
      } catch {
        // A stream retry can recover even when the catch-up request failed.
      }
      scheduleReconnect();
    };
  }

  async function start() {
    if (stopped) throw new Error("Cannot start a closed state transport");
    reportStatus("connecting");
    try {
      await poll();
    } finally {
      connect();
    }
  }

  function close() {
    if (stopped) return;
    stopped = true;
    for (const controller of requests) controller.abort();
    requests.clear();
    clearTimeout(retryTimer);
    retryTimer = null;
    stream?.close();
    stream = null;
    reportStatus("disconnected");
  }

  return { start, close, snapshot: () => currentEnvelope?.snapshot || null };
}
