import { afterEach, describe, expect, it, vi } from "vitest";
import { createOperatorCredential } from "./credential.js";
import {
  StewardError, createStewardClient, describeCommit, diagnosticsFor, isSameOrigin,
  isDefiniteApprovalRefusal, normalizeDiagnostics,
} from "./client.js";

const answer = (status, body, { json = true } = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  text: async () => (typeof body === "string" ? body : json ? JSON.stringify(body) : String(body)),
});

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key),
  };
}

const held = (token = "s3cret") => {
  const credential = createOperatorCredential({ storage: memoryStorage() });
  credential.remember(token);
  return credential;
};

describe("the operator credential", () => {
  it("starts unknown, so nothing is sent before a human says anything", () => {
    const credential = createOperatorCredential({ storage: memoryStorage() });
    expect(credential.status()).toBe("unknown");
    expect(credential.headers()).toBeNull();
  });

  it("carries a typed token as a bearer header and forgets it on request", () => {
    const credential = createOperatorCredential({ storage: memoryStorage() });
    credential.remember("  padded-token  ");
    expect(credential.status()).toBe("held");
    expect(credential.headers()).toEqual({ Authorization: "Bearer padded-token" });
    credential.forget();
    expect(credential.status()).toBe("unknown");
  });

  it("treats an open steward as known-to-be-nothing, not as unknown", () => {
    const credential = createOperatorCredential({ storage: memoryStorage() });
    credential.declareOpen();
    expect(credential.status()).toBe("open");
    expect(credential.headers()).toEqual({});
  });

  it("keeps working in memory when the browser refuses storage", () => {
    const hostile = {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
      removeItem: () => { throw new Error("blocked"); },
    };
    const credential = createOperatorCredential({ storage: hostile });
    expect(credential.ephemeral).toBe(true);
    credential.remember("t");
    expect(credential.headers()).toEqual({ Authorization: "Bearer t" });
  });
});

describe("the steward client", () => {
  it("classifies only trusted approval refusals as safe to retry", () => {
    for (const status of [401, 404, 422]) {
      expect(isDefiniteApprovalRefusal(new StewardError("no", { status }))).toBe(true);
    }
    expect(isDefiniteApprovalRefusal(new StewardError("local", { code: "credential_required" }))).toBe(true);
    expect(isDefiniteApprovalRefusal(new StewardError("expired", {
      status: 409,
      code: "approval_expired",
      raw: { detail: { error: "approval_expired", message: "too late" } },
    }))).toBe(true);

    for (const error of [
      new StewardError("timeout", { status: 408 }),
      new StewardError("generated", { status: 409, code: "approval_expired" }),
      new StewardError("extra", {
        status: 409,
        code: "approval_expired",
        raw: { detail: { error: "approval_expired", message: "too late", offered: [] } },
      }),
      new StewardError("network", { status: 0, code: "unreachable" }),
    ]) {
      expect(isDefiniteApprovalRefusal(error)).toBe(false);
    }
  });

  it("refuses to call at all before a credential exists", async () => {
    const fetch = vi.fn();
    const client = createStewardClient({
      credential: createOperatorCredential({ storage: memoryStorage() }), fetch,
    });
    await expect(client.listSkills()).rejects.toMatchObject({ code: "credential_required" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("sends the operator token same-origin, and never as a query parameter", async () => {
    const fetch = vi.fn().mockResolvedValue(answer(200, { skills: [] }));
    const client = createStewardClient({ credential: held(), fetch });
    await client.listSkills();
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("/skills");
    expect(init.headers.Authorization).toBe("Bearer s3cret");
    expect(init.method).toBe("GET");
    expect(init.cache).toBe("no-store");
  });

  it("encodes names into paths so a slash cannot forge a route", async () => {
    const fetch = vi.fn().mockResolvedValue(answer(200, {}));
    const client = createStewardClient({ credential: held(), fetch });
    await client.readSkill("a/b");
    expect(fetch.mock.calls[0][0]).toBe("/skills/a%2Fb");
    await client.readBudget("hob");
    expect(fetch.mock.calls[1][0]).toBe("/residents/hob/budget");
  });

  it("PUTs a declaration as JSON and hands back steward's whole answer", async () => {
    const body = { id: "hob", status: "written", commit: { committed: true, sha: "abc123def456" } };
    const fetch = vi.fn().mockResolvedValue(answer(200, body));
    const client = createStewardClient({ credential: held(), fetch });
    const result = await client.writeDeclaration("hob", { manifest: { version: 0 }, revision: "sha256:x" });

    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("/residents/hob/declaration");
    expect(init.method).toBe("PUT");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({ manifest: { version: 0 }, revision: "sha256:x" });
    expect(result).toEqual(body);
  });

  it("surfaces structured validation diagnostics instead of a paragraph", async () => {
    const fetch = vi.fn().mockResolvedValue(answer(422, {
      detail: {
        error: "manifest_invalid",
        message: "the declaration for 'hob' does not validate: charter.mission: …",
        diagnostics: [
          { file: "residents/hob/manifest.yaml", field: "charter.mission",
            problem: "exceeds the 2000 character limit", example: "mission: Keep it short.",
            severity: "error" },
          "not a diagnostic",
        ],
      },
    }));
    const client = createStewardClient({ credential: held(), fetch });

    const error = await client.writeDeclaration("hob", {}).catch((caught) => caught);
    expect(error).toBeInstanceOf(StewardError);
    expect(error.status).toBe(422);
    expect(error.code).toBe("manifest_invalid");
    expect(error.diagnostics).toHaveLength(1);
    expect(diagnosticsFor(error.diagnostics, "charter.mission")[0].example).toBe("mission: Keep it short.");
    expect(diagnosticsFor(error.diagnostics, "charter.duties")).toEqual([]);
  });

  it("forgets a credential steward answered 401 to", async () => {
    const credential = held();
    const fetch = vi.fn().mockResolvedValue(answer(401, { detail: { error: "unauthorized" } }));
    const client = createStewardClient({ credential, fetch });

    await expect(client.listSkills()).rejects.toMatchObject({ code: "unauthorized", status: 401 });
    expect(credential.status()).toBe("unknown");
  });

  it("keeps a 403 as steward spelled it, so a session credential names itself", async () => {
    const fetch = vi.fn().mockResolvedValue(answer(403, {
      detail: { error: "session_credential_forbidden", message: "writing a skill is a human act." },
    }));
    const client = createStewardClient({ credential: held(), fetch });
    await expect(client.createSkill({ name: "x" })).rejects.toMatchObject({
      code: "session_credential_forbidden",
      message: "writing a skill is a human act.",
      status: 403,
    });
  });

  it("flattens FastAPI's field-error list rather than dropping it", async () => {
    const fetch = vi.fn().mockResolvedValue(answer(422, {
      detail: [{ loc: ["body", "description"], msg: "field required" }],
    }));
    const client = createStewardClient({ credential: held(), fetch });
    await expect(client.createSkill({})).rejects.toMatchObject({
      code: "invalid_body",
      message: "body.description: field required",
    });
  });

  it("names an HTML answer as never having reached steward", async () => {
    // What the deployed nginx does with a route it does not proxy: the SPA's index.html,
    // 200, no steward involved at all.
    const fetch = vi.fn().mockResolvedValue(answer(200, "<!doctype html><html>…", { json: false }));
    const client = createStewardClient({ credential: held(), fetch });
    const error = await client.createSkill({ name: "x" }).catch((caught) => caught);
    expect(error.code).toBe("not_json");
    expect(error.message).toMatch(/did not reach steward/);
  });

  it("says an unreachable steward is unreachable rather than refused", async () => {
    const fetch = vi.fn().mockRejectedValue(new Error("Failed to fetch"));
    const client = createStewardClient({ credential: held(), fetch });
    await expect(client.listResidents()).rejects.toMatchObject({ code: "unreachable", status: 0 });
  });

  it("honours a base override for a CORS-enabled steward in development", async () => {
    const fetch = vi.fn().mockResolvedValue(answer(200, {}));
    const client = createStewardClient({ baseUrl: "http://127.0.0.1:8801/", credential: held(), fetch });
    await client.listResidents();
    expect(fetch.mock.calls[0][0]).toBe("http://127.0.0.1:8801/residents");
  });
});

/* -- the origin guard (warren#241) ---------------------------------------------------- */

describe("the credential never leaves this origin", () => {
  // The attack this closes: a link like `/observatory/?steward=https://evil.tld` opened in
  // a tab that already holds an unlocked operator token. The token is the whole control
  // plane's master key, so the client refuses the base rather than trusting whoever set it.
  // In development `import.meta.env.DEV` is true and a human pointing vite at their own
  // steward is still honoured; the built bundle has no such branch in it.
  afterEach(() => vi.unstubAllEnvs());

  const shipped = () => vi.stubEnv("DEV", false);

  it("refuses a cross-origin base outright, so no request is made at all", async () => {
    shipped();
    const fetch = vi.fn().mockResolvedValue(answer(200, {}));
    const client = createStewardClient({ baseUrl: "https://evil.tld", credential: held(), fetch });

    await expect(client.listResidents()).rejects.toMatchObject({ code: "cross_origin_base" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("sends no Authorization header anywhere but this origin, on any route", async () => {
    shipped();
    const fetch = vi.fn().mockResolvedValue(answer(200, {}));
    const hostile = ["https://evil.tld", "//evil.tld", "http://127.0.0.1:8801", "https://evil.tld:443/x"];

    for (const baseUrl of hostile) {
      const client = createStewardClient({ baseUrl, credential: held(), fetch });
      await client.listResidents().catch(() => {});
      await client.createSkill({ name: "x" }).catch(() => {});
      await client.reload().catch(() => {});
    }

    expect(fetch).not.toHaveBeenCalled();
    const headersSent = fetch.mock.calls.map(([, init]) => init?.headers?.Authorization);
    expect(headersSent).not.toContain("Bearer s3cret");
  });

  it("still calls this origin, by empty base or by bare path", async () => {
    shipped();
    const fetch = vi.fn().mockResolvedValue(answer(200, {}));

    await createStewardClient({ credential: held(), fetch }).listResidents();
    await createStewardClient({ baseUrl: "/api", credential: held(), fetch }).listResidents();
    await createStewardClient({ baseUrl: window.location.origin, credential: held(), fetch }).listResidents();

    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      "/residents", "/api/residents", `${window.location.origin}/residents`,
    ]);
    expect(fetch.mock.calls[0][1].headers.Authorization).toBe("Bearer s3cret");
  });

  it("answers the same question directly, for a base that came from anywhere", () => {
    expect(isSameOrigin("")).toBe(true);
    expect(isSameOrigin("/api")).toBe(true);
    expect(isSameOrigin(window.location.origin)).toBe(true);
    expect(isSameOrigin("https://evil.tld")).toBe(false);
    expect(isSameOrigin("//evil.tld")).toBe(false);
    expect(isSameOrigin("javascript:alert(1)")).toBe(false);
    expect(isSameOrigin("http://localhost:9999")).toBe(false);
  });
});

describe("reading steward's commit back", () => {
  it("reports the sha steward committed under, and the message it wrote", () => {
    const message = "chore(residents): update hob via the API";
    expect(describeCommit({ committed: true, sha: "0123456789abcdef", message }))
      .toMatchObject({ state: "committed", short: "0123456789", message });
  });

  it("calls an uncommitted write converged, not failed", () => {
    // steward: "committed: false with sha: null is the converged answer, not a failure".
    expect(describeCommit({ committed: false, sha: null }).state).toBe("converged");
    expect(describeCommit(null).state).toBe("none");
  });

  it("carries the note steward attaches when it wrote without git", () => {
    const note = "STEWARD_ALLOW_UNCOMMITTED_WRITES=1: written, not committed.";
    expect(describeCommit({ committed: false, sha: null, note }).note).toBe(note);
  });
});

describe("diagnostic normalisation", () => {
  it("drops anything that is not a diagnostic and defaults severity to error", () => {
    expect(normalizeDiagnostics(null)).toEqual([]);
    expect(normalizeDiagnostics([{ field: "a", problem: "b" }])[0]).toEqual({
      file: null, field: "a", problem: "b", example: null, severity: "error",
    });
    expect(normalizeDiagnostics([{ field: "a", problem: "b", severity: "warning" }])[0].severity).toBe("warning");
  });
});
