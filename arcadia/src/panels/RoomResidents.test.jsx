import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RoomResidents } from "./RoomResidents.jsx";
afterEach(cleanup);
const agents = Array.from({ length: 15 }, (_, i) => ({
  id: `agent-${i}`,
  name: `Resident ${String(i).padStart(2, "0")}`,
  project: i % 2 ? "Archive" : "Garden",
}));
describe("room resident navigation", () => {
  it("cycles in stable name order, wraps, and tolerates a departed selected agent", () => {
    const focus = vi.fn();
    const view = render(
      <RoomResidents
        agents={[agents[1], agents[0]]}
        selectedAgentId={agents[0].id}
        onFocusAgent={focus}
        kind="workshop"
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Focus previous resident" }),
    );
    expect(focus).toHaveBeenLastCalledWith(agents[1].id);
    view.rerender(
      <RoomResidents
        agents={[agents[1], agents[0]]}
        selectedAgentId="departed"
        onFocusAgent={focus}
        kind="workshop"
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Focus next resident" }),
    );
    expect(focus).toHaveBeenLastCalledWith(agents[0].id);
  });
  it("searches actual people and projects in crowded rooms and focuses the chosen identity", () => {
    const focus = vi.fn();
    render(
      <RoomResidents agents={agents} onFocusAgent={focus} kind="workshop" />,
    );
    fireEvent.change(
      screen.getByRole("searchbox", { name: "Find someone in this room" }),
      { target: { value: "Archive" } },
    );
    expect(screen.getAllByRole("option")).toHaveLength(8);
    fireEvent.change(
      screen.getByRole("combobox", { name: "Focus a room resident" }),
      { target: { value: agents[1].id } },
    );
    expect(focus).toHaveBeenCalledWith(agents[1].id);
    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "No such person" },
    });
    expect(screen.getByRole("option")).toHaveTextContent(
      "No matching residents",
    );
  });
  it("groups lodge guests only by their recorded project and makes names selectable", () => {
    const focus = vi.fn();
    render(
      <RoomResidents
        agents={[agents[0], agents[1], { ...agents[2], project: "" }]}
        onFocusAgent={focus}
        kind="lodge"
      />,
    );
    expect(screen.getByText("Garden")).toBeVisible();
    expect(screen.getByText("Archive")).toBeVisible();
    expect(screen.getByText("No project recorded")).toBeVisible();
    const details = screen.getByText("Garden").closest("details");
    details.open = true;
    fireEvent.click(screen.getByRole("button", { name: agents[0].name }));
    expect(focus).toHaveBeenCalledWith(agents[0].id);
    expect(screen.queryByRole("searchbox")).toBeNull();
  });
  it("does not add navigation for an empty room or cycling controls for one person", () => {
    const view = render(
      <RoomResidents agents={[]} onFocusAgent={() => {}} kind="home" />,
    );
    expect(screen.queryByRole("region")).toBeNull();
    view.rerender(
      <RoomResidents
        agents={[agents[0]]}
        onFocusAgent={() => {}}
        kind="home"
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });
});
