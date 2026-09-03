import { cleanup, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { NavigationProvider } from "../navigation.jsx";
import FleetPage from "./FleetPage.jsx";

const model = {
  people: [{
    id: "codex:visitor",
    name: "Visitor",
    residency: "visitor",
    hasResidentRecord: false,
    state: "idle",
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
