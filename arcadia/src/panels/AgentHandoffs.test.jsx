import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentHandoffs } from "./AgentHandoffs.jsx";
// Same real emitted event shapes as the delegation protocol tests; fixture stays local to avoid importing a test suite.
const snapshot = () => ({
  villagers: [
    {
      id: "steward:project-agent",
      name: "Project agent",
      lineage: {},
      history: [
        {
          v: 0,
          source: "steward",
          agent_id: "steward:project-agent",
          project: "warren",
          type: "task_delegated",
          ts: "2026-09-05T10:00:00.000Z",
          payload: {
            task_id: "task-1",
            title: "Check the errand list",
            from: "steward:project-agent",
            to: "claude-code:receiver-resident",
            route: "handoff",
            depth: 1,
            parent_task_id: "root-1",
          },
        },
      ],
    },
    {
      id: "claude-code:receiver-resident",
      name: "Receiver",
      lineage: {},
      history: [
        {
          v: 0,
          source: "steward",
          agent_id: "claude-code:receiver-resident",
          project: "warren",
          type: "task_done",
          ts: "2026-09-05T10:01:00.000Z",
          payload: {
            task_id: "task-1",
            title: "Check the errand list",
            claimant: "claude-code:receiver-resident",
            artifacts: ["report.md"],
            parent_task_id: "root-1",
          },
        },
      ],
    },
  ],
  tasks: [],
  approvals: [],
});
afterEach(cleanup);
describe("agent handoff panel", () => {
  it("shows exact recorded endpoints and completion, permits selection, and disclaims unavailable replies", () => {
    const select = vi.fn();
    render(<AgentHandoffs snapshot={snapshot()} onSelectAgent={select} />);
    expect(screen.getByText("Completion recorded")).toBeVisible();
    expect(screen.getByText("report.md")).toBeVisible();
    expect(
      screen.getByText(/Reply delivery and message text are not recorded/),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Receiver" }));
    expect(select).toHaveBeenCalledWith("claude-code:receiver-resident");
    expect(screen.getByText("root-1")).toBeVisible();
  });
  it("does not invent relationships when no delegated origin is retained", () => {
    const data = snapshot();
    data.villagers[0].history = [];
    render(<AgentHandoffs snapshot={data} />);
    expect(
      screen.getByText(
        "No explicit agent-to-agent handoffs are retained in this snapshot.",
      ),
    ).toBeVisible();
    expect(screen.getByText("Unlinked task records · 1")).toBeVisible();
    expect(screen.queryByText("Completion recorded")).toBeNull();
  });
  it("shows refused attempts as undelivered and links the exact approval request", () => {
    const data = snapshot();
    data.approvals = [
      {
        request_id: "refusal-1",
        agent_id: "steward:project-agent",
        action: "rejected_delegation",
        state: "pending",
        message: "Refused",
        detail: {
          to: "receiver-resident",
          title: "Read this",
          reason: "not_permitted",
        },
        opened_at: "2026-09-05T10:02:00.000Z",
      },
    ];
    render(<AgentHandoffs snapshot={data} />);
    const blocked = screen.getByRole("region", {
      name: "Blocked handoff attempts",
    });
    expect(blocked).toHaveTextContent("identity not linked");
    expect(within(blocked).getByRole("link")).toHaveAttribute(
      "href",
      "#approval-refusal-1",
    );
  });
});
