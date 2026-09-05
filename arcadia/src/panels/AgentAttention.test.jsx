import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import fixture from "../contract/fixtures/complete-v1.js";
import {
  AgentAttention,
  ApprovalKnocks,
  ApprovalProvider,
} from "./AgentAttention.jsx";
import { createStewardClient } from "../steward/StewardClient.js";
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
function Both({ snapshot = fixture.snapshot, client }) {
  return (
    <ApprovalProvider snapshot={snapshot} stewardClient={client}>
      <AgentAttention
        snapshot={snapshot}
        stewardClient={client}
        agentId="claude:keeper"
      />
      <ApprovalKnocks snapshot={snapshot} stewardClient={client} />
    </ApprovalProvider>
  );
}
describe("contextual agent attention", () => {
  it("filters to the selected agent and sends exact decisions through shared authority", async () => {
    const data = structuredClone(fixture.snapshot);
    data.approvals.push({
      ...data.approvals[0],
      request_id: "other",
      agent_id: "codex:other",
      message: "Other question",
    });
    const client = {
      decideApproval: vi
        .fn()
        .mockResolvedValue({ state: "awaiting_confirmation" }),
    };
    const view = render(<Both snapshot={data} client={client} />);
    const contextual = screen.getByRole("region", {
      name: "Agent approval requests",
    });
    expect(contextual).toHaveTextContent("Deploy?");
    expect(contextual).not.toHaveTextContent("Other question");
    expect(document.querySelectorAll("#approvals")).toHaveLength(1);
    fireEvent.click(
      within(contextual).getByRole("button", { name: "Approve Deploy?" }),
    );
    expect(client.decideApproval).toHaveBeenCalledExactlyOnceWith(
      "approval-1",
      { decision: "approve" },
    );
    expect(
      within(screen.getByRole("region", { name: "Approval knocks" })).getByRole(
        "button",
        { name: "Deny Other question" },
      ),
    ).toBeDisabled();
    expect(
      within(contextual).getByRole("button", { name: "Deny Deploy?" }),
    ).toBeDisabled();
    const confirmed = structuredClone(data);
    confirmed.approvals[0].state = "resolved";
    view.rerender(<Both snapshot={confirmed} client={client} />);
    expect(
      screen.queryByRole("region", { name: "Agent approval requests" }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Deny Other question" }),
    ).toBeEnabled();
  });
  it("unlocks both views without storing tokens and reopens both forms after a definitive 401", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue({
        status: 401,
        json: async () => ({ detail: { message: "Credential expired" } }),
      });
    const client = createStewardClient({ fetch });
    render(<Both client={client} />);
    const contextual = screen.getByRole("region", {
      name: "Agent approval requests",
    });
    const global = screen.getByRole("region", { name: "Approval knocks" });
    expect(
      screen.getAllByLabelText("Steward token").map((input) => input.id),
    ).toEqual(expect.arrayContaining([expect.any(String), expect.any(String)]));
    expect(
      new Set(
        screen.getAllByLabelText("Steward token").map((input) => input.id),
      ).size,
    ).toBe(2);
    fireEvent.change(within(contextual).getByLabelText("Steward token"), {
      target: { value: "context-token" },
    });
    fireEvent.click(
      within(contextual).getByRole("button", { name: "Unlock answers" }),
    );
    expect(screen.queryByDisplayValue("context-token")).toBeNull();
    expect(
      within(global).getByRole("button", { name: "Approve Deploy?" }),
    ).toBeEnabled();
    fireEvent.click(
      within(contextual).getByRole("button", { name: "Approve Deploy?" }),
    );
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
    expect(screen.getAllByLabelText("Steward token")).toHaveLength(2);
    expect(fetch.mock.calls[0][1].headers.Authorization).toBe(
      "Bearer context-token",
    );
  });
  it("keeps both views locked after an ambiguous result and preserves edit validation", async () => {
    const client = {
      decideApproval: vi
        .fn()
        .mockRejectedValue(
          Object.assign(new Error("Outcome unknown"), { ambiguous: true }),
        ),
    };
    const data = structuredClone(fixture.snapshot);
    data.approvals[0].options = ["edit", "deny", { decision: "approve" }];
    render(<Both snapshot={data} client={client} />);
    const contextual = screen.getByRole("region", {
      name: "Agent approval requests",
    });
    vi.spyOn(window, "prompt")
      .mockReturnValueOnce("[]")
      .mockReturnValueOnce('{"target":"staging"}');
    fireEvent.click(
      within(contextual).getByRole("button", { name: "Edit Deploy?" }),
    );
    expect(client.decideApproval).not.toHaveBeenCalled();
    expect(within(contextual).getByRole("alert")).toHaveTextContent(
      "must be JSON objects",
    );
    fireEvent.click(
      within(contextual).getByRole("button", { name: "Edit Deploy?" }),
    );
    await waitFor(() =>
      expect(within(contextual).getByRole("alert")).toHaveTextContent(
        "Outcome unknown",
      ),
    );
    expect(client.decideApproval).toHaveBeenCalledExactlyOnceWith(
      "approval-1",
      { decision: "edit", edit: { target: "staging" } },
    );
    expect(
      screen
        .getAllByRole("button", { name: "Deny Deploy?" })
        .every((button) => button.disabled),
    ).toBe(true);
  });
  it("shows only recorded failures belonging to the selected agent and offers records without inventing retries", () => {
    const data = structuredClone(fixture.snapshot);
    data.approvals = [];
    data.tasks[0].state = "failed";
    data.routines[0].state = "failed";
    data.routines[0].error = "Process exited 1";
    data.tasks.push({
      ...data.tasks[0],
      id: "other-failure",
      claimant: "codex:other",
      title: "Unrelated failure",
    });
    render(<AgentAttention snapshot={data} agentId="claude:keeper" />);
    const failures = screen.getByRole("region", {
      name: "Agent failure context",
    });
    expect(failures).toHaveTextContent("Freeze contract");
    expect(failures).toHaveTextContent("Process exited 1");
    expect(failures).not.toHaveTextContent("Unrelated failure");
    expect(
      within(failures).getByRole("link", { name: "Review village records →" }),
    ).toHaveAttribute("href", "#records");
    expect(within(failures).queryByRole("button")).toBeNull();
  });
});
