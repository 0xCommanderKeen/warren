import { afterEach, describe, expect, it, vi } from "vitest";
import { createStewardClient, isSameOrigin, stewardBaseFromLocation } from "./StewardClient.js";

function response(status, body) {
  return {
    status,
    json: async () => body,
  };
}

describe("Steward client", () => {
  it("supplies changeable credentials from memory on every write", async () => {
    const fetch = vi.fn().mockResolvedValue(response(202, {
      status: "recorded",
      request_id: "request-1",
      approval_request_id: "a-1", decision: "approve",
    }));
    const client = createStewardClient({ baseUrl: "https://steward.test", fetch });

    client.setCredentials({ token: "first" });
    await client.decideApproval("a-1", { decision: "approve" });
    client.setCredentials({ token: "second" });
    client.confirm({ approvals: [{ request_id: "a-1", state: "resolved", decision: "approve" }] });
    await client.decideApproval("a-1", { decision: "approve" });

    expect(fetch).toHaveBeenNthCalledWith(1, "https://steward.test/approvals/a-1", expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer first" }),
    }));
    expect(fetch).toHaveBeenNthCalledWith(2, "https://steward.test/approvals/a-1", expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer second" }),
    }));
  });

  it("requires credentials after they are cleared", async () => {
    const fetch = vi.fn();
    const client = createStewardClient({ fetch });
    client.setCredentials({ token: "secret" });
    client.clearCredentials();
    await expect(client.decideApproval("a-1", { decision: "approve" })).rejects.toMatchObject({ code: "credentials_required" });
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([401, 422])("allows retry after a pre-mutation %s refusal", async (status) => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(status, { detail: { message: "Refused" } }))
      .mockResolvedValueOnce(response(202, {
        status: "recorded", request_id: "request-2", approval_request_id: "a-1", decision: "approve",
      }));
    const client = createStewardClient({ fetch });
    client.setCredentials({ token: "secret" });

    await expect(client.decideApproval("a-1", { decision: "approve" })).rejects.toMatchObject({
      status, retryable: true, ambiguous: false,
    });
    if (status === 401) {
      await expect(client.decideApproval("a-1", { decision: "approve" })).rejects.toMatchObject({ code: "credentials_required" });
      expect(fetch).toHaveBeenCalledTimes(1);
      client.setCredentials({ token: "corrected" });
    }
    await expect(client.decideApproval("a-1", { decision: "approve" })).resolves.toMatchObject({
      state: "awaiting_confirmation",
    });
  });

  it.each([
    ["a server failure", () => response(500, { detail: { message: "Oops" } })],
    ["a malformed acceptance", () => response(202, { status: "recorded" })],
    ["an unreadable receipt", () => ({ status: 202, json: async () => { throw new Error("invalid JSON"); } })],
  ])("blocks retries after %s", async (_name, result) => {
    const fetch = vi.fn().mockResolvedValue(result());
    const client = createStewardClient({ fetch });
    client.setCredentials({ token: "secret" });

    await expect(client.decideApproval("a-1", { decision: "approve" })).rejects.toMatchObject({
      retryable: false, ambiguous: true,
    });
    await expect(client.decideApproval("a-1", { decision: "approve" })).rejects.toMatchObject({
      code: "write_blocked",
    });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("blocks overlapping writes and unlocks only for the matching Chronicle snapshot", async () => {
    let resolveFetch;
    const fetch = vi.fn(() => new Promise((resolve) => { resolveFetch = resolve; }));
    const client = createStewardClient({ fetch });
    client.setCredentials({ token: "secret" });

    const first = client.decideApproval("a-1", { decision: "approve" });
    await expect(client.decideApproval("a-2", { decision: "deny" })).rejects.toMatchObject({
      code: "write_blocked",
    });
    resolveFetch(response(202, {
      status: "recorded", request_id: "request-1", approval_request_id: "a-1", decision: "approve",
    }));
    const snapshot = { approvals: [{ request_id: "a-1", state: "pending" }] };
    expect(client.confirm(snapshot)).toBe(false);
    await expect(first).resolves.toMatchObject({ state: "awaiting_confirmation" });
    expect(snapshot.approvals).toEqual([{ request_id: "a-1", state: "pending" }]);
    expect(client.confirm(snapshot)).toBe(false);
    expect(client.confirm({ approvals: [{ request_id: "a-1", state: "resolved", decision: "deny" }] })).toBe(false);

    expect(client.confirm({ approvals: [{ request_id: "other", state: "resolved", decision: "approve" }] })).toBe(false);
    await expect(client.decideApproval("a-1", { decision: "approve" })).rejects.toMatchObject({
      code: "write_blocked",
    });
    expect(client.confirm({ approvals: [{ request_id: "a-1", state: "resolved", decision: "approve" }] })).toBe(true);
  });

  it("reconciles an ambiguous receipt using the requested approval identity", async () => {
    const fetch = vi.fn().mockResolvedValue(response(202, {
      status: "unexpected", approval_request_id: "a-1", decision: "approve",
    }));
    const client = createStewardClient({ fetch });
    client.setCredentials({ token: "secret" });

    await expect(client.decideApproval("a-1", { decision: "approve" })).rejects.toMatchObject({ ambiguous: true });

    expect(client.confirm({ approvals: [{ request_id: "a-1", state: "resolved", decision: "approve" }] })).toBe(true);
  });

  it("surfaces a network failure as ambiguous", async () => {
    const fetch = vi.fn().mockRejectedValue(new Error("offline"));
    const client = createStewardClient({ fetch });
    client.setCredentials({ token: "secret" });

    await expect(client.decideApproval("a-1", { decision: "approve" })).rejects.toMatchObject({
      retryable: false, ambiguous: true,
    });
    await expect(client.decideApproval("a-2", { decision: "deny" })).rejects.toMatchObject({ code: "write_blocked" });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(client.confirm({ approvals: [{ request_id: "a-1", state: "resolved", decision: "approve" }] })).toBe(true);
  });
});

/* -- the origin guard (warren#256) ----------------------------------------------------- */

describe("where Steward lives", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("is this origin when nobody says otherwise", () => {
    expect(stewardBaseFromLocation("?unrelated=true")).toBe("");
  });

  it("is still this origin under ?steward= in anything that ships", () => {
    // The exfiltration link, defused: `import.meta.env.DEV` is false in a built bundle and
    // Vite eliminates the branch, so the parameter is inert text in a URL.
    vi.stubEnv("DEV", false);
    expect(stewardBaseFromLocation("?steward=https://evil.tld")).toBe("");
  });

  it("ignores VITE_STEWARD_URL in anything that ships, for the same reason", () => {
    // A build-time variable is not attacker-controlled, but a shipped Arcadia has no use
    // for one: deploy/nginx.conf proxies Steward's write routes behind the same origin.
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_STEWARD_URL", "https://steward.elsewhere");
    expect(stewardBaseFromLocation("?unrelated=true")).toBe("");
  });

  it("is whatever a developer running vite points it at", () => {
    // Developers may override the same-origin development proxy.
    vi.stubEnv("DEV", true);
    expect(stewardBaseFromLocation("?steward=http://127.0.0.1:8802")).toBe("http://127.0.0.1:8802");
    vi.stubEnv("VITE_STEWARD_URL", "http://127.0.0.1:8802");
    expect(stewardBaseFromLocation("?unrelated=true")).toBe("http://127.0.0.1:8802");
  });
});

describe("the credential never leaves this origin", () => {
  // warren#256, sibling of #241: the approval prompt hands a bearer token to this client,
  // so a base chosen by a link would carry that token to whoever wrote the link. In a built
  // bundle `import.meta.env.DEV` is false and this refusal is unconditional.
  afterEach(() => vi.unstubAllEnvs());

  const shipped = () => vi.stubEnv("DEV", false);

  it("refuses a cross-origin base outright, so no request is made at all", async () => {
    shipped();
    const fetch = vi.fn();
    const client = createStewardClient({ baseUrl: "https://evil.tld", fetch });
    client.setCredentials({ token: "secret" });

    await expect(client.decideApproval("a-1", { decision: "approve" })).rejects.toMatchObject({
      code: "cross_origin_base", ambiguous: false, retryable: false,
    });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("sends no Authorization header off-origin, on any write", async () => {
    shipped();
    const fetch = vi.fn();
    for (const baseUrl of ["https://evil.tld", "//evil.tld", "http://127.0.0.1:8802"]) {
      const client = createStewardClient({ baseUrl, fetch });
      client.setCredentials({ token: "secret" });
      for (const requestId of ["a-1", "a-2"]) {
        await expect(client.decideApproval(requestId, { decision: "approve" })).rejects.toMatchObject({
          code: "cross_origin_base",
        });
        expect(fetch).not.toHaveBeenCalled();
      }
    }

    expect(fetch).not.toHaveBeenCalled();
  });

  it("does not wedge the client: a refusal leaves no write unresolved", async () => {
    // The refusal has to happen before any write is registered. A cross-origin base that
    // parked `writeState` would answer every later write with "already unresolved" — the
    // client would be dead until reload, which is a worse bug than the one being fixed.
    shipped();
    const fetch = vi.fn();
    const client = createStewardClient({ baseUrl: "https://evil.tld", fetch });
    client.setCredentials({ token: "secret" });

    await expect(client.decideApproval("a-1", { decision: "approve" })).rejects.toMatchObject({ code: "cross_origin_base" });
    await expect(client.decideApproval("a-1", { decision: "approve" })).rejects.toMatchObject({ code: "cross_origin_base" });
  });

  it("still writes to this origin, by empty base or bare path", async () => {
    shipped();
    const fetch = vi.fn().mockResolvedValue(response(202, {
      status: "recorded", request_id: "request-1", approval_request_id: "a-1", decision: "approve",
    }));

    const here = createStewardClient({ fetch });
    here.setCredentials({ token: "secret" });
    await here.decideApproval("a-1", { decision: "approve" });

    const prefixed = createStewardClient({ baseUrl: `${window.location.origin}/arcadia`, fetch });
    prefixed.setCredentials({ token: "secret" });
    await prefixed.decideApproval("a-1", { decision: "approve" });

    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      "/approvals/a-1", `${window.location.origin}/arcadia/approvals/a-1`,
    ]);
    expect(fetch.mock.calls[0][1].headers.Authorization).toBe("Bearer secret");
  });

  it("answers the same question directly, for a base that came from anywhere", () => {
    expect(isSameOrigin("")).toBe(true);
    expect(isSameOrigin("/chronicle")).toBe(true);
    expect(isSameOrigin(window.location.origin)).toBe(true);
    expect(isSameOrigin("https://evil.tld")).toBe(false);
    expect(isSameOrigin("//evil.tld")).toBe(false);
    expect(isSameOrigin("javascript:alert(1)")).toBe(false);
    expect(isSameOrigin("https://steward.test")).toBe(false);
  });
});
