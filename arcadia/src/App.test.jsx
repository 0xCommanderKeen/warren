import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { UnsupportedSchemaVersionError } from "./contract/parseSnapshot.js";
import { App, LiveApp, backendFromLocation } from "./App.jsx";
import fixture from "./contract/fixtures/complete-v1.js";
import { createStewardClient } from "./steward/StewardClient.js";

vi.mock("./game/PhaserGame.jsx", () => ({
  PhaserGame: () => <div data-testid="village-canvas" />,
}));

afterEach(cleanup);

describe("Arcadia", () => {
  it("starts the live Chronicle transport and renders its snapshots", async () => {
    let options;
    const close = vi.fn();
    const transportFactory = vi.fn((nextOptions) => {
      options = nextOptions;
      return { start: vi.fn().mockResolvedValue(), close };
    });
    const stewardClient = { confirm: vi.fn() };
    const { unmount } = render(
      <LiveApp
        baseUrl="/burrow"
        stewardClient={stewardClient}
        transportFactory={transportFactory}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Village snapshot has not loaded yet.");
    await waitFor(() => expect(transportFactory).toHaveBeenCalledWith(
      expect.objectContaining({ baseUrl: "/burrow" }),
    ));
    options.onEnvelope(fixture);
    await waitFor(() => expect(screen.getAllByText("Keeper").length).toBeGreaterThan(0));
    await waitFor(() => expect(stewardClient.confirm).toHaveBeenCalledWith(fixture.snapshot));

    unmount();
    expect(close).toHaveBeenCalledOnce();
  });

  it("uses the backend query parameter as the live transport prefix", () => {
    expect(backendFromLocation("?backend=%2Fburrow")).toBe("/burrow");
    expect(backendFromLocation("?unrelated=true")).toBe("/burrow");
  });

  it("keeps valid state visible through a transient transport failure", async () => {
    let options;
    const transportFactory = (nextOptions) => {
      options = nextOptions;
      return { start: vi.fn().mockResolvedValue(), close: vi.fn() };
    };
    render(<LiveApp transportFactory={transportFactory} />);
    options.onEnvelope(fixture);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Arcadia" })).toBeVisible());

    options.onError(new Error("State request failed: HTTP 502"));

    await waitFor(() => expect(screen.getByRole("heading", { name: "Arcadia" })).toBeVisible());
    expect(screen.queryByText("Contract mismatch")).not.toBeInTheDocument();
  });

  it("turns an unsupported live schema into the safe mismatch screen", async () => {
    let options;
    const transportFactory = (nextOptions) => {
      options = nextOptions;
      return { start: vi.fn().mockResolvedValue(), close: vi.fn() };
    };
    render(<LiveApp transportFactory={transportFactory} />);

    options.onError(new UnsupportedSchemaVersionError(2));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Unsupported village schema version: 2",
    );
  });

  it("renders every read-only panel directly from the contract fixture", () => {
    render(<App envelope={fixture} />);

    expect(screen.getByRole("region", { name: "Notice board" })).toHaveTextContent(
      /report\.mdKeeper · burrow/,
    );
    expect(screen.getByRole("region", { name: "Job board" })).toHaveTextContent(
      /Freeze contractclaimed · pythonKeeper/,
    );
    expect(screen.getByRole("region", { name: "Routine ledger" })).toHaveTextContent(
      /dailyfinished · ok · 300sreport\.md · Keeper/,
    );
    expect(screen.getByRole("region", { name: "Charter journal" })).toHaveTextContent(
      /KeeperkeeperMaintains Burrow\.Manifestv1 · keeper\.resident\.jsonMatchclaude:keeperHome2Capabilitiestools: ReadRoutinesNone declared/,
    );
    expect(screen.getByRole("region", { name: "Journal observations" })).toHaveTextContent(
      /Keeper2026-08-27 · dailyjournals\/2026-08-27\.md/,
    );
  });

  it("does not turn villager history into panel state", () => {
    const envelope = structuredClone(fixture);
    envelope.snapshot.artifacts = [];
    envelope.snapshot.tasks = [];
    envelope.snapshot.routines = [];
    envelope.snapshot.journals = [];
    envelope.snapshot.villagers[0].history = [
      {
        ...fixture.snapshot.villagers[0].history[0],
        type: "task_done",
        payload: { title: "History must remain invisible", artifact: "ghost.md" },
      },
    ];

    render(<App envelope={envelope} />);

    expect(screen.queryByText(/History must remain invisible|ghost\.md/)).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Notice board" })).toHaveTextContent(
      "Nothing has been produced yet.",
    );
    expect(screen.getByRole("region", { name: "Job board" })).toHaveTextContent(
      "There are no jobs in the queue.",
    );
    expect(screen.getByRole("region", { name: "Routine ledger" })).toHaveTextContent(
      "No routine runs have been observed.",
    );
    expect(screen.getByRole("region", { name: "Journal observations" })).toHaveTextContent(
      "No journal observations have been recorded.",
    );
  });

  it("waits for a snapshot rather than falling back to contract test data", () => {
    render(<App />);

    expect(screen.getByRole("status")).toHaveTextContent("Village snapshot has not loaded yet.");
    expect(screen.queryByText("Keeper")).toBeNull();
  });

  it("distinguishes a snapshot that has not loaded from an unavailable contract", () => {
    const { rerender } = render(<App envelope={null} />);

    expect(screen.getByRole("status")).toHaveTextContent("Village snapshot has not loaded yet.");

    rerender(<App envelope={{ kind: "snapshot", snapshot: { ...fixture.snapshot, tasks: null } }} />);

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Expected snapshot.tasks to be an array",
    );
  });

  it("visibly rejects an unsupported snapshot before rendering villagers", () => {
    render(
      <App
        envelope={{
          kind: "snapshot",
          snapshot: {
            schema_version: 2,
            villagers: [{ id: "future:villager", name: "Future Villager" }],
          },
        }}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Unsupported village schema version: 2",
    );
    expect(screen.queryByText("Future Villager")).not.toBeInTheDocument();
    expect(screen.queryByTestId("village-canvas")).not.toBeInTheDocument();
  });

  it("visibly rejects malformed nested state before rendering it", () => {
    const envelope = structuredClone(fixture);
    envelope.snapshot.tasks[0].required_skills = null;

    render(<App envelope={envelope} />);

    expect(screen.getByRole("alert")).toHaveTextContent("Contract mismatch");
    expect(screen.getByRole("alert")).toHaveTextContent("snapshot.tasks");
  });

  it("safely renders JSON-valued approval options allowed by Chronicle's contract", () => {
    const envelope = structuredClone(fixture);
    envelope.snapshot.approvals[0].options = [{ decision: "approve" }];

    render(<App envelope={envelope} />);

    expect(screen.getByRole("button", {
      name: '{"decision":"approve"} Deploy?',
    })).toBeInTheDocument();
  });

  it("offers each valid Chronicle snapshot to the Steward confirmation boundary", () => {
    const stewardClient = { confirm: vi.fn() };

    render(<App envelope={fixture} stewardClient={stewardClient} />);

    expect(stewardClient.confirm).toHaveBeenCalledWith(fixture.snapshot);
  });

  it("keeps a knock visible until a confirming snapshot resolves it", () => {
    const stewardClient = {
      confirm: vi.fn(),
      decideApproval: vi.fn().mockResolvedValue({ state: "awaiting_confirmation" }),
    };
    const { rerender } = render(<App envelope={fixture} stewardClient={stewardClient} />);
    const knocks = screen.getByRole("region", { name: "Approval knocks" });

    expect(knocks).toHaveTextContent("KeeperDeploy?");
    fireEvent.click(within(knocks).getByRole("button", { name: "Approve Deploy?" }));

    expect(stewardClient.decideApproval).toHaveBeenCalledWith("approval-1", {
      decision: "approve",
    });
    expect(screen.getByText("Deploy?")).toBeInTheDocument();

    const confirmed = structuredClone(fixture);
    confirmed.snapshot.approvals[0] = {
      ...confirmed.snapshot.approvals[0],
      state: "resolved",
      decision: "approve",
      resolved_at: "2026-08-27T12:01:00.000Z",
    };
    confirmed.snapshot.villagers[0].pending_approval_ids = [];
    confirmed.snapshot.villagers[0].state = "idle";
    rerender(<App envelope={confirmed} stewardClient={stewardClient} />);

    expect(screen.queryByRole("region", { name: "Approval knocks" })).not.toBeInTheDocument();
  });

  it("hands Steward credentials over without storing them in the page", () => {
    const stewardClient = {
      confirm: vi.fn(),
      setCredentials: vi.fn(),
      decideApproval: vi.fn(),
    };
    render(<App envelope={fixture} stewardClient={stewardClient} />);

    expect(screen.getByRole("button", { name: "Approve Deploy?" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Steward token"), {
      target: { value: "tab-only-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Unlock answers" }));

    expect(stewardClient.setCredentials).toHaveBeenCalledWith({ token: "tab-only-secret" });
    expect(screen.queryByDisplayValue("tab-only-secret")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve Deploy?" })).toBeEnabled();
  });

  it("reopens authentication after Steward rejects an expired credential", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce({
        status: 401,
        json: async () => ({ detail: { message: "Credential expired" } }),
      })
      .mockResolvedValueOnce({
        status: 202,
        json: async () => ({
          status: "recorded",
          request_id: "decision-2",
          approval_request_id: "approval-1",
          decision: "approve",
        }),
      });
    const stewardClient = createStewardClient({ fetch });
    render(<App envelope={fixture} stewardClient={stewardClient} />);

    fireEvent.change(screen.getByLabelText("Steward token"), {
      target: { value: "expired-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Unlock answers" }));
    fireEvent.click(screen.getByRole("button", { name: "Approve Deploy?" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Credential expired");
    expect(screen.getByLabelText("Steward token")).toHaveValue("");
    expect(screen.queryByDisplayValue("expired-secret")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Steward token"), {
      target: { value: "replacement-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Unlock answers" }));
    fireEvent.click(screen.getByRole("button", { name: "Approve Deploy?" }));

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(fetch.mock.calls.map(([, request]) => request.headers.Authorization)).toEqual([
      "Bearer expired-secret",
      "Bearer replacement-secret",
    ]);
    expect(screen.queryByDisplayValue("replacement-secret")).not.toBeInTheDocument();
    expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toMatch(
      /expired-secret|replacement-secret/,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "Answer sent. Waiting for Steward's confirming state",
    );
  });

  it("orders multiple fixture approvals deterministically and answers each by request id", () => {
    const stewardClient = {
      confirm: vi.fn(),
      decideApproval: vi.fn().mockResolvedValue({ state: "awaiting_confirmation" }),
    };

    const envelope = structuredClone(fixture);
    envelope.snapshot.generation = 8;
    envelope.snapshot.villagers.unshift({
      ...structuredClone(fixture.snapshot.villagers[0]),
      id: "claude:ada",
      name: "Ada",
      residency: "visitor",
      home: null,
      base: "lodge",
      resident_file: null,
      state: "knocking",
      project: "arcadia",
      cwd: "/work/arcadia",
      pending_approval_ids: ["approval-b"],
    });
    envelope.snapshot.approvals.unshift({
      ...structuredClone(fixture.snapshot.approvals[0]),
      request_id: "approval-b",
      agent_id: "claude:ada",
      project: "arcadia",
      message: "Publish?",
      action: "publish",
    });

    render(<App envelope={envelope} stewardClient={stewardClient} />);

    const knocks = screen.getByRole("region", { name: "Approval knocks" });
    expect(within(knocks).getAllByRole("article").map((item) => item.textContent)).toEqual([
      expect.stringContaining("Deploy?"),
      expect.stringContaining("Publish?"),
    ]);
    fireEvent.click(within(knocks).getByRole("button", { name: "Deny Publish?" }));
    expect(stewardClient.decideApproval).toHaveBeenCalledWith("approval-b", {
      decision: "deny",
    });
    expect(within(knocks).getByRole("button", { name: "Approve Deploy?" })).toBeDisabled();
  });

  it("never draws a knock for a resolved approval", () => {
    const envelope = structuredClone(fixture);
    envelope.snapshot.approvals[0].state = "resolved";
    envelope.snapshot.approvals[0].decision = "deny";

    render(<App envelope={envelope} />);

    expect(screen.queryByRole("region", { name: "Approval knocks" })).not.toBeInTheDocument();
  });

  it("keeps approvals answerable after a definitive preflight refusal", async () => {
    const refusal = Object.assign(new Error("Steward credentials are required"), {
      ambiguous: false,
      code: "credentials_required",
    });
    const stewardClient = {
      confirm: vi.fn(),
      setCredentials: vi.fn(),
      decideApproval: vi.fn().mockRejectedValue(refusal),
    };

    render(<App envelope={fixture} stewardClient={stewardClient} />);
    fireEvent.change(screen.getByLabelText("Steward token"), {
      target: { value: "stale-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Unlock answers" }));
    fireEvent.click(screen.getByRole("button", { name: "Approve Deploy?" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Steward credentials are required");
    expect(screen.getByLabelText("Steward token")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Approve Deploy?" })).toBeDisabled();
  });
});
