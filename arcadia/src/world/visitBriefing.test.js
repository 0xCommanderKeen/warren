import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { createVisitBriefing, visitBaseline, readVisitBaseline, saveVisitBaseline } from "./visitBriefing.js";

const snapshot = extra => ({ generation: 1, log_generation: 1, evaluated_at: "2026-09-05T10:00:00Z", villagers: [{ id: "one", state: "working", name: "Private name" }], tasks: [{ id: "task", state: "claimed", title: "Private task" }], approvals: [], ...extra });
const total = value => value.arrivals.length + value.completedTasks.length + value.failures.length + value.approvals.length;

describe("since last visit baseline", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());
  it("starts quietly and compares retained changes against a fixed previous visit", () => {
    const first = snapshot();
    const tracker = createVisitBriefing();
    expect(total(tracker.update(first))).toBe(0);
    saveVisitBaseline(first);
    const returned = createVisitBriefing(readVisitBaseline());
    const later = snapshot({ generation: 2, villagers: [{ id: "one", state: "failed" }, { id: "new", state: "resting" }], tasks: [{ id: "task", state: "done" }] });
    expect(total(returned.update(later))).toBe(3);
    saveVisitBaseline(later);
    expect(total(returned.update({ ...later, generation: 3 }))).toBe(3);
    expect(total(createVisitBriefing(readVisitBaseline()).update(later))).toBe(0);
  });
  it("reminds returning visitors about existing unanswered requests, but mark seen keeps this mount quiet", () => {
    const current = snapshot({ approvals: [{ request_id: "a", state: "pending", message: "Private message" }] });
    expect(total(createVisitBriefing().update(current))).toBe(0);
    const tracker = createVisitBriefing(visitBaseline(current));
    expect(tracker.update(current).approvals).toHaveLength(1);
    tracker.markSeen(current);
    expect(total(tracker.update({ ...current, generation: 2 }))).toBe(0);
    const newRequest = { ...current, generation: 3, approvals: [...current.approvals, { request_id: "b", state: "pending" }] };
    expect(tracker.update(newRequest).approvals.map(item => item.request_id)).toEqual(["b"]);
    expect(createVisitBriefing(visitBaseline(current)).update(current).approvals).toHaveLength(1);
  });
  it("quietly rebaselines on log reset or backward generation, including pending requests", () => {
    const tracker = createVisitBriefing(visitBaseline(snapshot({ generation: 10 })));
    const reset = snapshot({ generation: 11, log_generation: 2, approvals: [{ request_id: "a", state: "pending" }] });
    expect(total(tracker.update(reset))).toBe(0);
    expect(total(tracker.update({ ...reset, generation: 9, villagers: [{ id: "new", state: "failed" }] }))).toBe(0);
  });
  it("does not infer events from missing records or include resolved requests", () => {
    const tracker = createVisitBriefing(visitBaseline(snapshot()));
    expect(total(tracker.update(snapshot({ generation: 2, villagers: [], tasks: [], approvals: [{ request_id: "a", state: "resolved" }] })))).toBe(0);
  });
  it("requires a recent timestamp for unknown completed or failed tasks, while preserving observed transitions", () => {
    const tracker = createVisitBriefing(visitBaseline(snapshot()));
    const tasks = [
      { id: "task", state: "done", updated_at: "2020-01-01T00:00:00Z" },
      ...["done", "failed"].flatMap(state => [
        { id: `${state}-old`, state, updated_at: "2020-01-01T00:00:00Z" },
        { id: `${state}-missing`, state },
        { id: `${state}-invalid`, state, updated_at: "not a timestamp" },
        { id: `${state}-recent`, state, updated_at: "2026-09-05T10:01:00Z" },
        { id: `${state}-boundary`, state, updated_at: "2026-09-05T10:00:00Z" },
      ]),
    ];
    const summary = tracker.update(snapshot({ generation: 2, tasks }));
    expect(summary.completedTasks.map(task => task.id)).toEqual(["task", "done-recent", "done-boundary"]);
    expect(summary.failures.map(item => item.record.id)).toEqual(["failed-recent", "failed-boundary"]);
  });
  it("stores only bounded ID/state metadata and ignores malformed storage", () => {
    saveVisitBaseline(snapshot());
    expect(localStorage.getItem("arcadia:visit:v1")).not.toContain("Private");
    const bad = visitBaseline(snapshot()); bad.agents.push(["one", "working"]);
    localStorage.setItem("arcadia:visit:v1", JSON.stringify(bad));
    expect(readVisitBaseline()).toBeNull();
    localStorage.setItem("arcadia:visit:v1", "bad JSON");
    expect(readVisitBaseline()).toBeNull();
    expect(visitBaseline(snapshot({ villagers: Array(10001).fill({ id: "a", state: "working" }) }))).toBeNull();
  });
  it("works without browser storage", () => {
    vi.stubGlobal("localStorage", { getItem() { throw Error(); }, setItem() { throw Error(); } });
    expect(readVisitBaseline()).toBeNull();
    expect(() => saveVisitBaseline(snapshot())).not.toThrow();
    expect(total(createVisitBriefing().update(snapshot()))).toBe(0);
  });
});
