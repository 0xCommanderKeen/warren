import { describe, expect, it, vi } from "vitest";
import { createStewardClient } from "./StewardClient.js";

function response(status, body) {
  return {
    status,
    json: async () => body,
  };
}

describe("Steward client", () => {
  it("supplies changeable credentials from memory on every write", async () => {
    const fetch = vi.fn().mockResolvedValue(response(202, {
      status: "accepted",
      request_id: "request-1",
      task_id: "task-1",
    }));
    const client = createStewardClient({ baseUrl: "https://steward.test", fetch });

    client.setCredentials({ token: "first" });
    await client.postJob({ title: "Map the woods" });
    client.setCredentials({ token: "second" });
    client.confirm({ tasks: [{ id: "task-1" }] });
    await client.postJob({ title: "Repair the bridge" });

    expect(fetch).toHaveBeenNthCalledWith(1, "https://steward.test/jobs", expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer first" }),
    }));
    expect(fetch).toHaveBeenNthCalledWith(2, "https://steward.test/jobs", expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer second" }),
    }));
  });

  it.each([401, 422])("allows retry after a pre-mutation %s refusal", async (status) => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(status, { detail: { message: "Refused" } }))
      .mockResolvedValueOnce(response(202, {
        status: "accepted", request_id: "request-2", task_id: "task-2",
      }));
    const client = createStewardClient({ fetch });
    client.setCredentials({ token: "secret" });

    await expect(client.postJob({ title: "Map the woods" })).rejects.toMatchObject({
      status, retryable: true, ambiguous: false,
    });
    if (status === 401) client.setCredentials({ token: "corrected" });
    await expect(client.postJob({ title: "Map the woods" })).resolves.toMatchObject({
      state: "awaiting_confirmation",
    });
  });

  it.each([
    ["a server failure", () => response(500, { detail: { message: "Oops" } })],
    ["a malformed acceptance", () => response(202, { status: "accepted" })],
  ])("blocks retries after %s", async (_name, result) => {
    const fetch = vi.fn().mockResolvedValue(result());
    const client = createStewardClient({ fetch });
    client.setCredentials({ token: "secret" });

    await expect(client.postJob({ title: "Map the woods" })).rejects.toMatchObject({
      retryable: false, ambiguous: true,
    });
    await expect(client.postJob({ title: "Map the woods" })).rejects.toMatchObject({
      code: "write_blocked",
    });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("blocks overlapping writes and unlocks only for the matching Burrow snapshot", async () => {
    let resolveFetch;
    const fetch = vi.fn(() => new Promise((resolve) => { resolveFetch = resolve; }));
    const client = createStewardClient({ fetch });
    client.setCredentials({ token: "secret" });

    const first = client.postJob({ title: "Map the woods" });
    await expect(client.runRoutine("keeper", "daily")).rejects.toMatchObject({
      code: "write_blocked",
    });
    resolveFetch(response(202, {
      status: "accepted", request_id: "request-1", task_id: "task-1",
    }));
    await first;

    expect(client.confirm({ tasks: [{ id: "other" }] })).toBe(false);
    await expect(client.postJob({ title: "Map the woods" })).rejects.toMatchObject({
      code: "write_blocked",
    });
    expect(client.confirm({ tasks: [{ id: "task-1" }] })).toBe(true);
  });

  it("does not confuse a historical routine run with confirmation of a new write", async () => {
    const fetch = vi.fn().mockResolvedValue(response(202, {
      status: "accepted", request_id: "request-1", resident: "keeper", routine: "daily",
    }));
    const client = createStewardClient({ fetch });
    client.setCredentials({ token: "secret" });
    const oldRun = { run_id: "old", routine: "daily", trigger: "manual", agent_id: "claude:keeper" };
    client.confirm({ generation: "g", cursor: 4, routines: [oldRun] });

    await client.runRoutine("keeper", "daily");

    expect(client.confirm({ generation: "g", cursor: 5, routines: [oldRun] })).toBe(false);
    expect(client.confirm({ generation: "g", cursor: 6, routines: [oldRun, { ...oldRun, run_id: "new" }] })).toBe(true);
  });

  it("reconciles an ambiguous receipt when it retained an exact identity", async () => {
    const fetch = vi.fn().mockResolvedValue(response(202, {
      status: "unexpected", task_id: "task-1",
    }));
    const client = createStewardClient({ fetch });
    client.setCredentials({ token: "secret" });

    await expect(client.postJob({ title: "Map the woods" })).rejects.toMatchObject({ ambiguous: true });

    expect(client.confirm({ tasks: [{ id: "task-1" }] })).toBe(true);
  });

  it("surfaces a network failure as ambiguous", async () => {
    const client = createStewardClient({ fetch: vi.fn().mockRejectedValue(new Error("offline")) });
    client.setCredentials({ token: "secret" });

    await expect(client.postJob({ title: "Map the woods" })).rejects.toMatchObject({
      retryable: false, ambiguous: true,
    });
  });

  it("owns resident, routine, and approval writes without changing village state", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(201, { status: "accepted", request_id: "n-1", id: "keeper", changed: true }))
      .mockResolvedValueOnce(response(202, { status: "accepted", request_id: "r-1", resident: "keeper", routine: "daily" }))
      .mockResolvedValueOnce(response(202, { status: "recorded", request_id: "a-2", approval_request_id: "a-1", decision: "approve" }));
    const client = createStewardClient({ fetch });
    client.setCredentials({ token: "secret" });

    const snapshot = { villagers: [], routines: [], approvals: [] };
    await client.createResident({ id: "keeper", name: "Keeper" });
    expect(snapshot.villagers).toEqual([]);
    expect(client.confirm({ villagers: [{ id: "claude:keeper" }] })).toBe(true);
    await client.runRoutine("keeper", "daily");
    expect(snapshot.routines).toEqual([]);
    expect(client.confirm({ routines: [{ routine: "daily", trigger: "manual", agent_id: "claude:keeper" }] })).toBe(true);
    await client.decideApproval("a-1", { decision: "approve" });
    expect(snapshot.approvals).toEqual([]);
    expect(client.confirm({ approvals: [{ request_id: "a-1", state: "resolved", decision: "approve" }] })).toBe(true);

    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      "/residents",
      "/residents/keeper/routines/daily/run",
      "/approvals/a-1",
    ]);
  });
});
