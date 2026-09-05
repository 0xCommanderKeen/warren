import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { NavigationProvider } from "../navigation.jsx";
import FleetPage from "./FleetPage.jsx";

const model = {
  people: [{
    id: "codex:visitor",
    name: "Visitor",
    residency: "visitor",
    hasResidentRecord: false,
    state: "working",
    history: [],
  }],
  active: 0,
  tasks: [],
  approvals: [],
  artifacts: [],
  journals: [],
  routines: [],
  events: [],
};

describe("Fleet agent navigation", () => {
  beforeEach(() => {
    cleanup();
    window.history.replaceState({}, "", "/");
  });

  it("gives a transient agent a dedicated, collision-safe record page", () => {
    render(<NavigationProvider base="/"><FleetPage model={model} page="fleet" params={{}} /></NavigationProvider>);

    const link = screen.getByRole("link", { name: /Visitor/i });
    expect(link.getAttribute("href")).toBe("/agents/codex%3Avisitor");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("resolves a transient agent's full id on the record route", () => {
    render(<NavigationProvider base="/"><FleetPage model={model} page="agent" params={{ uuid: "codex:visitor" }} /></NavigationProvider>);

    expect(screen.getByRole("heading", { name: "Visitor" })).toBeTruthy();
    expect(screen.getByText("transient")).toBeTruthy();
  });
});

describe("Fleet inactive sessions", () => {
  beforeEach(() => {
    cleanup();
    window.history.replaceState({}, "", "/");
  });

  const person = (id, state = "stale", extra = {}) => ({
    ...model.people[0], id, name: id, state, ...extra,
  });
  const fleet = (value) => <NavigationProvider base="/"><FleetPage model={value} page="fleet" params={{}} /></NavigationProvider>;
  const shown = () => screen.queryAllByRole("link").map((link) => decodeURIComponent(link.getAttribute("href").replace("/agents/", "")));

  it("hides inactive visitors by default and toggles them without removing their history", () => {
    const value = {
      ...model,
      people: [person("working", "working"), person("old"), person("resting", "resting"), person("failed", "failed")],
      events: [{ agent_id: "old", ts: "2026-09-04T12:00:00Z", type: "tool_called", payload: { tool: "Retained tool" } }],
    };
    render(fleet(value));
    expect(shown()).toEqual(["working"]);
    expect(screen.getByText("3 inactive sessions hidden")).toBeTruthy();
    expect(screen.getByText("Retained tool")).toBeTruthy();
    expect(screen.getByRole("option", { name: "old" })).toBeTruthy();

    fireEvent.click(screen.getByRole("checkbox", { name: /Show inactive/ }));
    expect(shown()).toEqual(["working", "old", "resting", "failed"]);
    expect(screen.getByText("3 inactive sessions included")).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: /Show inactive/ }));
    expect(shown()).toEqual(["working"]);
    expect(value.people).toHaveLength(4);
  });

  it("keeps residents and inactive agents with decisions or unfinished work visible", () => {
    render(fleet({
      ...model,
      people: [
        person("resident", "stale", { residency: "resident", hasResidentRecord: false }),
        person("approval"), person("claimant"), person("poster"), person("runner"), person("finished"),
      ],
      approvals: [{ agent_id: "approval", state: "pending" }, { agent_id: "finished", state: "resolved" }],
      tasks: [
        { id: "open", state: "claimed", claimant: "claimant", posted_by: "poster" },
        ...["done", "cancelled", "failed"].map((state) => ({ id: state, state, claimant: "finished", posted_by: "finished" })),
      ],
      routines: [{ agent_id: "runner", state: "running" }, { agent_id: "finished", state: "finished" }, { agent_id: "finished", state: "failed" }],
    }));
    expect(shown()).toEqual(["resident", "approval", "claimant", "poster", "runner"]);
  });

  it("updates visibility when a session becomes stale or acquires a pending approval", () => {
    const { rerender } = render(fleet({ ...model, people: [person("changing", "working")] }));
    expect(shown()).toEqual(["changing"]);
    const stale = { ...model, people: [person("changing")] };
    rerender(fleet(stale));
    expect(shown()).toEqual([]);
    expect(screen.getByText("No agents to show.")).toBeTruthy();
    rerender(fleet({ ...stale, approvals: [{ agent_id: "changing", state: "pending" }] }));
    expect(shown()).toEqual(["changing"]);
  });

  it("still opens an inactive visitor directly by its full id", () => {
    render(<NavigationProvider base="/"><FleetPage model={{ ...model, people: [person("codex:old")] }} page="agent" params={{ uuid: "codex:old" }} /></NavigationProvider>);
    expect(screen.getByRole("heading", { name: "codex:old" })).toBeTruthy();
  });
});
