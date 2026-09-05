import "@testing-library/jest-dom/vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import fixture from "../contract/fixtures/complete-v1.js";
import { WorkshopBoard } from "./WorkshopBoard.jsx";

afterEach(cleanup);
function snapshot() {
  const value = structuredClone(fixture.snapshot);
  value.tasks = ["open", "claimed", "failed", "done"].map((state, i) => ({
    ...value.tasks[0],
    id: `task-${i}`,
    title: `${state} task`,
    state,
    assignee: null,
    claimant: state === "open" ? null : "claude:keeper",
  }));
  return value;
}
const mount = (extra = {}) =>
  render(
    <WorkshopBoard snapshot={snapshot()} onSelectAgent={vi.fn()} {...extra} />,
  );

describe("workshop task board", () => {
  it("maps recorded task states to truthful board columns", () => {
    mount();
    for (const [column, state] of [
      ["Queued", "open"],
      ["Active", "claimed"],
      ["Needs attention", "failed"],
      ["Completed", "done"],
    ]) {
      const region = screen.getByRole("region", { name: column, exact: true });
      expect(within(region).getByRole("button")).toHaveTextContent(
        `${state} task`,
      );
      expect(region).toHaveTextContent(
        state === "failed" ? "Failed" : state[0].toUpperCase() + state.slice(1),
      );
    }
    expect(screen.queryByText(/blocked/i)).not.toBeInTheDocument();
  });

  it("opens actual details and only locates an agent on an explicit request", () => {
    const onSelectAgent = vi.fn();
    mount({ onSelectAgent });
    fireEvent.click(
      within(
        screen.getByRole("region", { name: "Active", exact: true }),
      ).getByRole("button"),
    );
    const details = screen.getByRole("region", { name: "Selected task" });
    expect(details).toHaveTextContent("task-1");
    expect(details).toHaveTextContent("python");
    expect(details).toHaveTextContent("Keeper");
    expect(details).toHaveTextContent("Not assigned");
    expect(within(details).getByRole("time")).toHaveAttribute(
      "datetime",
      fixture.snapshot.tasks[0].updated_at,
    );
    expect(onSelectAgent).not.toHaveBeenCalled();
    fireEvent.click(
      within(details).getByRole("button", { name: "Locate claimant Keeper" }),
    );
    expect(onSelectAgent).toHaveBeenCalledWith({
      kind: "agent",
      id: "claude:keeper",
    });
    fireEvent.click(
      within(details).getByRole("button", { name: "Close task details" }),
    );
    expect(
      screen.queryByRole("region", { name: "Selected task" }),
    ).not.toBeInTheDocument();
  });

  it("searches real task, person and skill fields, then clears filters", () => {
    mount();
    const search = screen.getByRole("searchbox", {
      name: "Search workshop tasks",
    });
    fireEvent.change(search, { target: { value: "Keeper" } });
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    fireEvent.change(search, { target: { value: "python" } });
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
    fireEvent.change(search, { target: { value: "task-2" } });
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    fireEvent.change(search, { target: { value: "missing" } });
    expect(screen.getByText("No matching tasks.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
  });

  it("preserves unknown agent IDs without offering a broken location action", () => {
    const value = snapshot();
    value.tasks[1].claimant = "retired-agent";
    value.tasks[1].assignee = "another-retired-agent";
    mount({ snapshot: value });
    fireEvent.click(
      within(
        screen.getByRole("region", { name: "Active", exact: true }),
      ).getByRole("button"),
    );
    const details = screen.getByRole("region", { name: "Selected task" });
    expect(details).toHaveTextContent("retired-agent");
    expect(details).toHaveTextContent("Not in the current village");
    expect(
      within(details).queryByRole("button", { name: /Locate/ }),
    ).not.toBeInTheDocument();
  });

  it("updates selected records and removes details when a task leaves the snapshot", () => {
    const value = snapshot();
    const view = mount({ snapshot: value });
    fireEvent.click(
      within(
        screen.getByRole("region", { name: "Active", exact: true }),
      ).getByRole("button"),
    );
    const changed = structuredClone(value);
    changed.tasks[1].state = "done";
    view.rerender(<WorkshopBoard snapshot={changed} />);
    expect(
      screen.getByRole("region", { name: "Selected task" }),
    ).toHaveTextContent("Task record · Done");
    view.rerender(<WorkshopBoard snapshot={{ ...changed, tasks: [] }} />);
    expect(
      screen.queryByRole("region", { name: "Selected task" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("A clear workbench.")).toBeVisible();
  });

  it("folds the board away and restores it without inventing tasks", () => {
    mount({ snapshot: { ...snapshot(), tasks: [] } });
    fireEvent.click(
      screen.getByRole("button", { name: "Collapse task board" }),
    );
    expect(screen.queryByRole("searchbox")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Expand task board" }));
    expect(screen.getByText("A clear workbench.")).toBeVisible();
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });
  it("opens the exact requested task, clears search and unfolds the board once per request", () => {
    const value = snapshot(),
      onHighlightAgent = vi.fn(),
      onSelectAgent = vi.fn();
    const view = mount({ snapshot: value, onHighlightAgent, onSelectAgent });
    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "missing" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Collapse task board" }),
    );
    view.rerender(
      <WorkshopBoard
        snapshot={value}
        taskRequest={{ id: "task-2", nonce: 1 }}
        onHighlightAgent={onHighlightAgent}
        onSelectAgent={onSelectAgent}
      />,
    );
    expect(screen.getByRole("searchbox")).toHaveValue("");
    expect(
      screen.getByRole("region", { name: "Selected task" }),
    ).toHaveTextContent("failed task");
    expect(onHighlightAgent).toHaveBeenLastCalledWith({
      kind: "agent",
      id: "claude:keeper",
    });
    expect(onSelectAgent).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Close task details" }));
    view.rerender(
      <WorkshopBoard
        snapshot={structuredClone(value)}
        taskRequest={{ id: "task-2", nonce: 1 }}
        onHighlightAgent={onHighlightAgent}
      />,
    );
    expect(
      screen.queryByRole("region", { name: "Selected task" }),
    ).not.toBeInTheDocument();
    view.rerender(
      <WorkshopBoard
        snapshot={value}
        taskRequest={{ id: "task-2", nonce: 2 }}
        onHighlightAgent={onHighlightAgent}
      />,
    );
    expect(
      screen.getByRole("region", { name: "Selected task" }),
    ).toHaveTextContent("failed task");
  });

  it("highlights a current claimant or assigned agent without navigating and clears on close", () => {
    const value = snapshot(),
      onHighlightAgent = vi.fn(),
      onSelectAgent = vi.fn();
    value.tasks[0].assignee = "claude:keeper";
    value.tasks[2].claimant = "retired-agent";
    mount({ snapshot: value, onHighlightAgent, onSelectAgent });
    fireEvent.click(
      within(
        screen.getByRole("region", { name: "Active", exact: true }),
      ).getByRole("button"),
    );
    expect(onHighlightAgent).toHaveBeenLastCalledWith({
      kind: "agent",
      id: "claude:keeper",
    });
    fireEvent.click(screen.getByRole("button", { name: "Close task details" }));
    expect(onHighlightAgent).toHaveBeenLastCalledWith(null);
    fireEvent.click(
      within(
        screen.getByRole("region", { name: "Queued", exact: true }),
      ).getByRole("button"),
    );
    expect(onHighlightAgent).toHaveBeenLastCalledWith({
      kind: "agent",
      id: "claude:keeper",
    });
    fireEvent.click(
      within(
        screen.getByRole("region", { name: "Needs attention", exact: true }),
      ).getByRole("button"),
    );
    expect(onHighlightAgent).toHaveBeenLastCalledWith(null);
    expect(onSelectAgent).not.toHaveBeenCalled();
    expect(
      onHighlightAgent.mock.calls.every(
        ([selection]) => selection === null || selection.id === "claude:keeper",
      ),
    ).toBe(true);
  });

  it("waits for a requested task to exist without fabricating its details", () => {
    const value = snapshot(),
      request = { id: "task-2", nonce: 1 };
    const view = mount({
      snapshot: { ...value, tasks: [] },
      taskRequest: request,
    });
    expect(
      screen.queryByRole("region", { name: "Selected task" }),
    ).not.toBeInTheDocument();
    view.rerender(<WorkshopBoard snapshot={value} taskRequest={request} />);
    expect(
      screen.getByRole("region", { name: "Selected task" }),
    ).toHaveTextContent("failed task");
  });
});

it("clears a selected agent highlight when its task record disappears", () => {
  const value = snapshot(),
    onHighlightAgent = vi.fn();
  const view = mount({ snapshot: value, onHighlightAgent });
  fireEvent.click(
    within(
      screen.getByRole("region", { name: "Active", exact: true }),
    ).getByRole("button"),
  );
  expect(onHighlightAgent).toHaveBeenLastCalledWith({
    kind: "agent",
    id: "claude:keeper",
  });
  view.rerender(
    <WorkshopBoard
      snapshot={{ ...value, tasks: [] }}
      onHighlightAgent={onHighlightAgent}
    />,
  );
  expect(onHighlightAgent).toHaveBeenLastCalledWith(null);
});

it("brings explicitly selected task details into view without scrolling on live refresh", () => {
  const original = Element.prototype.scrollIntoView;
  const scroll = vi.fn();
  Element.prototype.scrollIntoView = scroll;
  try {
    const value = snapshot();
    const view = mount({ snapshot: value });
    fireEvent.click(
      within(
        screen.getByRole("region", { name: "Active", exact: true }),
      ).getByRole("button"),
    );
    expect(scroll).toHaveBeenCalledOnce();
    view.rerender(<WorkshopBoard snapshot={structuredClone(value)} />);
    expect(scroll).toHaveBeenCalledOnce();
  } finally {
    if (original) Element.prototype.scrollIntoView = original;
    else delete Element.prototype.scrollIntoView;
  }
});
