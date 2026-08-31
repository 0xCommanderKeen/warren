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

  async function call(path, { method = "GET", body, signal } = {}) {
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
      response = await doFetch(`${normalizedBase(baseUrl)}${path}`, {
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
        "steward refused that credential. It compares what you paste against STEWARD_TOKEN in its own environment.",
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

  return {
    call,
    // -- reads -----------------------------------------------------------------------
    listResidents: (options) => call("/residents", options),
    listSkills: (options) => call("/skills", options),
    readSkill: (name, options) => call(`/skills/${encodeURIComponent(name)}`, options),
    readDeclaration: (id, options) => call(`/residents/${encodeURIComponent(id)}/declaration`, options),
    readBudget: (id, options) => call(`/residents/${encodeURIComponent(id)}/budget`, options),

    // -- writes ----------------------------------------------------------------------
    // Each returns steward's whole answer, `commit` included. No caller invents one.
    createSkill: (body) => call("/skills", { method: "POST", body }),
    updateSkill: (name, body) => call(`/skills/${encodeURIComponent(name)}`, { method: "PUT", body }),
    writeDeclaration: (id, body) =>
      call(`/residents/${encodeURIComponent(id)}/declaration`, { method: "PUT", body }),
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
    return { state: "none", sha: null, short: null, note: null, identity: null };
  }
  const sha = typeof commit.sha === "string" && commit.sha ? commit.sha : null;
  return {
    state: commit.committed && sha ? "committed" : "converged",
    sha,
    short: sha ? sha.slice(0, 10) : null,
    identity: typeof commit.identity === "string" ? commit.identity : null,
    note: typeof commit.note === "string" && commit.note ? commit.note : null,
  };
}
