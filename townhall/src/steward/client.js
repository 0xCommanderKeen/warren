/* The one place townhall talks to steward.
 *
 * Every read on this screen came from steward a moment ago and every write is reported
 * back as steward answered it — the commit it made, the diagnostics it refused with, the
 * word it used. The console this ports from had one rule above all others: render what
 * steward said, not what the click intended. Nothing here synthesises a success, retries
 * a write, or merges a partial edit.
 *
 * Requests are same-origin by default (`baseUrl: ""`), because the NAS's nginx proxies
 * steward's write routes behind the deployed origin. A `?steward=` override exists for
 * local development against a CORS-enabled steward.
 */

/** A refusal, or a non-answer, exactly as steward gave it. */
export class StewardError extends Error {
  constructor(message, { status = null, code = "steward_refusal", diagnostics = [], raw = null } = {}) {
    super(message);
    this.name = "StewardError";
    this.status = status;
    this.code = code;
    this.diagnostics = diagnostics;
    this.raw = raw;
  }
}

/**
 * Structured validation diagnostics, normalised.
 *
 * steward#214 shaped these for exactly this purpose: `file`/`field`/`problem`/`example`/
 * `severity`, so a form can highlight the field rather than print a paragraph. Anything
 * that is not that shape is dropped rather than rendered as `[object Object]`.
 */
export function normalizeDiagnostics(value) {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item) => item && typeof item === "object")
    .map((item) => ({
      file: typeof item.file === "string" ? item.file : null,
      field: typeof item.field === "string" ? item.field : null,
      problem: typeof item.problem === "string" ? item.problem : "",
      example: typeof item.example === "string" ? item.example : null,
      severity: item.severity === "warning" ? "warning" : "error",
    }))
    .filter((item) => item.problem || item.field);
}

/** Which diagnostics belong to one form field, so the box itself can carry the refusal. */
export function diagnosticsFor(diagnostics, field) {
  return (diagnostics || []).filter((item) => item.field === field);
}

function messageFrom(detail, fallback) {
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    return typeof detail.message === "string" && detail.message ? detail.message : fallback;
  }
  if (Array.isArray(detail)) {
    const lines = detail
      .map((item) => `${(item?.loc || []).join(".")}: ${item?.msg ?? ""}`.trim())
      .filter(Boolean);
    return lines.length ? lines.join("\n") : fallback;
  }
  return typeof detail === "string" && detail ? detail : fallback;
}

function codeFrom(detail, status) {
  if (detail && typeof detail === "object" && !Array.isArray(detail) && typeof detail.error === "string") {
    return detail.error;
  }
  if (Array.isArray(detail)) return "invalid_body";
  return `http_${status}`;
}

const normalizedBase = (value) => String(value || "").trim().replace(/\/+$/, "");

export function createStewardClient({ baseUrl = "", fetch: fetchImpl, credential } = {}) {
  const doFetch = fetchImpl || ((...args) => globalThis.fetch(...args));

  async function call(path, { method = "GET", body, signal, query } = {}) {
    const auth = credential?.headers?.();
    if (!auth) {
      throw new StewardError(
        "Townhall holds no steward credential. Unlock the write path before asking steward for this.",
        { code: "credential_required" },
      );
    }

    const headers = { ...auth, Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";

    let response;
    try {
      const search = query ? `?${new URLSearchParams(query)}` : "";
      response = await doFetch(`${normalizedBase(baseUrl)}${path}${search}`, {
        method,
        headers,
        cache: "no-store",
        signal,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch (cause) {
      if (signal?.aborted) throw cause;
      throw new StewardError(`steward did not answer: ${cause?.message || cause}`, {
        status: 0,
        code: "unreachable",
      });
    }

    const text = await response.text();
    let payload = null;
    let parsed = false;
    try {
      payload = text ? JSON.parse(text) : null;
      parsed = true;
    } catch {
      parsed = false;
    }

    if (response.status === 401) {
      // Forget it on the spot: a token steward refused is a token this tab should stop
      // presenting, and the human is about to be asked for another one.
      credential.forget?.();
      throw new StewardError(
        "steward refused that credential. An operator credential that has been revoked reads " +
          "exactly like one that was never minted — `steward operator list` says which.",
        { status: 401, code: "unauthorized", raw: payload },
      );
    }

    if (!parsed) {
      // The commonest cause on the NAS is a route the origin does not proxy to steward at
      // all: nginx falls through to the SPA and answers with index.html, which is a 200
      // full of HTML. Say that, rather than "unexpected token <".
      throw new StewardError(
        `The origin answered ${response.status} with something that is not JSON, so this did not reach steward. ` +
          "A write route the deployed nginx does not proxy looks exactly like this.",
        { status: response.status, code: "not_json", raw: text.slice(0, 400) || null },
      );
    }

    if (!response.ok) {
      const detail = payload?.detail;
      throw new StewardError(messageFrom(detail, `steward answered ${response.status}.`), {
        status: response.status,
        code: codeFrom(detail, response.status),
        diagnostics: normalizeDiagnostics(detail?.diagnostics),
        raw: payload,
      });
    }

    return payload;
  }

  const at = (id) => encodeURIComponent(id);

  return {
    call,
    // -- reads -----------------------------------------------------------------------
    //
    // One method per path the steward console's own ROUTES map declared, which is the
    // authoritative list of what a control panel for this fleet has to be able to ask
    // (warren#225). Chronicle's `/state` answers none of these: its projection carries
    // journal *metadata* but no text, no inbox at all, and no budget — so the three
    // panels a resident page is judged on are steward's, not the village's.
    listResidents: (options) => call("/residents", options),
    readResident: (id, options) => call(`/residents/${at(id)}`, options),
    listSkills: (options) => call("/skills", options),
    readSkill: (name, options) => call(`/skills/${at(name)}`, options),
    readDeclaration: (id, options) => call(`/residents/${at(id)}/declaration`, options),
    readBudget: (id, options) => call(`/residents/${at(id)}/budget`, options),
    readJournal: (id, options) => call(`/residents/${at(id)}/journal`, options),
    readInbox: (id, options) => call(`/residents/${at(id)}/inbox`, options),
    listRoutines: (options) => call("/routines", options),
    listJobs: (options) => call("/jobs", options),
    listApprovals: (status, options) => call("/approvals", { ...options, query: { status } }),
    // The one read that exists so a write can be believed: every mutating route answers
    // with a request id and the word "accepted", and this is where the outcome turns up.
    readRequest: (id, options) => call(`/requests/${at(id)}`, options),

    // -- writes ----------------------------------------------------------------------
    // Each returns steward's whole answer, `commit` included. No caller invents one.
    createSkill: (body) => call("/skills", { method: "POST", body }),
    updateSkill: (name, body) => call(`/skills/${at(name)}`, { method: "PUT", body }),
    writeDeclaration: (id, body) => call(`/residents/${at(id)}/declaration`, { method: "PUT", body }),
    createResident: (body) => call("/residents", { method: "POST", body }),
    runRoutine: (residentId, routineId) =>
      call(`/residents/${at(residentId)}/routines/${at(routineId)}/run`, { method: "POST" }),
    postJob: (body) => call("/jobs", { method: "POST", body }),
    decideApproval: (requestId, body) => call(`/approvals/${at(requestId)}`, { method: "POST", body }),
    reload: () => call("/reload", { method: "POST" }),
  };
}

/**
 * What steward reported about the commit it made — or honestly, that it made none.
 *
 * `committed: false` with `sha: null` is the converged answer rather than a failure: what
 * is on disk was already what is in git. Rendering that as "saved, no commit" is the
 * truth; rendering it as a failure would be a lie in the other direction.
 */
export function describeCommit(commit) {
  if (!commit || typeof commit !== "object") {
    return { state: "none", sha: null, short: null, note: null, message: null };
  }
  const sha = typeof commit.sha === "string" && commit.sha ? commit.sha : null;
  return {
    state: commit.committed && sha ? "committed" : "converged",
    sha,
    short: sha ? sha.slice(0, 10) : null,
    // The subject line steward wrote, whose trailer carries the request id — the honest
    // link back to `GET /requests/{id}` for who asked and when.
    message: typeof commit.message === "string" && commit.message ? commit.message : null,
    note: typeof commit.note === "string" && commit.note ? commit.note : null,
  };
}
