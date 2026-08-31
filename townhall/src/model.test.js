import { describe, expect, it } from "vitest";
import fixture from "./fixtures/complete-v1.js";
import { agentUrl, eventFeed, routeAgent, viewModel } from "./model.js";

const snapshot = fixture.snapshot;

describe("Observatory presentation model", () => {
  it("derives the fleet only from a complete snapshot", () => {
    const model = viewModel(snapshot);
    expect(model.people).toHaveLength(snapshot.villagers.length);
    expect(model.tasks).toBe(snapshot.tasks);
    expect(model.approvals).toBe(snapshot.approvals);
    expect(model.active).toBe(1);
    expect(model.people.every((person) => Number.isFinite(person.x) && Number.isFinite(person.y))).toBe(true);
  });

  it("gives created residents permanent UUID routes and leaves visitors transient", () => {
    const changed = structuredClone(snapshot);
    changed.residents[0].match = { project: "chronicle" };
    changed.villagers.push({
      ...changed.villagers[0],
      id: "codex:visitor",
      name: "Visitor",
      resident_file: null,
      residency: "visitor",
    });
    const model = viewModel(changed);
    expect(model.people.find((person) => person.id === "claude:keeper").hasPage).toBe(true);
    expect(model.people.find((person) => person.id === "codex:visitor").hasPage).toBe(false);
    expect(agentUrl("codex:550e8400-e29b-41d4-a716-446655440000")).toBe("/agents/550e8400-e29b-41d4-a716-446655440000");
    expect(routeAgent("/agents/550e8400-e29b-41d4-a716-446655440000")).toBe("550e8400-e29b-41d4-a716-446655440000");
  });

  it("forms one newest-first retained activity feed", () => {
    const villagers = [...snapshot.villagers, {
      ...snapshot.villagers[0],
      id: "codex:second",
      name: "Second",
      history: [{ ts: "2026-08-27T12:01:00Z", type: "tool_called", payload: { tool: "Read" } }],
    }];
    const events = eventFeed(villagers);
    expect(events[0].agent_id).toBe("codex:second");
    expect(events[1].type).toBe("needs_human");
  });
});
