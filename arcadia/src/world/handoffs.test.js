import { describe, expect, it } from "vitest";
import { buildAgentHandoffs } from "./handoffs.js";
// Exact emitted payload shapes from steward/tests/test_delegation.py::test_the_event_names_both_ends
// and steward/src/steward/events.py::task_claimed_event/task_done_event/task_failed_event.
export function delegationSnapshot() {
  const from = "claude-code:sender-agent",
    to = "claude-code:receiver-agent";
  const event = (type, agent_id, payload, minute) => ({
    v: 0,
    type,
    source: "steward",
    agent_id,
    project: "library",
    ts: `2026-09-05T10:${String(minute).padStart(2, "0")}:00.000Z`,
    payload,
  });
  const origin = event(
    "task_delegated",
    from,
    {
      task_id: "letter-1",
      title: "Read the background",
      from,
      to,
      route: "inbox",
      parent_task_id: null,
      depth: 1,
    },
    0,
  );
  const claimed = event(
    "task_claimed",
    to,
    { task_id: "letter-1", title: "Read the background", claimant: to },
    1,
  );
  const done = event(
    "task_done",
    to,
    {
      task_id: "letter-1",
      title: "Read the background",
      claimant: to,
      artifacts: ["report.md"],
      run_id: "run-1",
    },
    2,
  );
  return {
    villagers: [
      { id: from, name: "Sender", history: [origin], lineage: {} },
      { id: to, name: "Receiver", history: [claimed, done], lineage: {} },
    ],
    tasks: [
      {
        id: "letter-1",
        title: "Read the background",
        posted_by: from,
        assignee: to,
        claimant: to,
        state: "done",
        updated_at: done.ts,
      },
    ],
    approvals: [],
  };
}
describe("recorded handoff projection", () => {
  it("joins exact task/recipient identities and exposes artifacts without claiming reply delivery", () => {
    const model = buildAgentHandoffs(delegationSnapshot());
    expect(model.handoffs).toHaveLength(1);
    expect(model.handoffs[0]).toMatchObject({
      from: "claude-code:sender-agent",
      to: "claude-code:receiver-agent",
      state: "done",
      result: { artifacts: ["report.md"], runId: "run-1" },
    });
    expect(model.handoffs[0]).not.toHaveProperty("reply");
    expect(model.unlinked).toHaveLength(0);
  });
  it("never matches a wrong recipient, same project, or textual mention", () => {
    const data = delegationSnapshot();
    data.tasks = [];
    data.villagers[1].history = data.villagers[1].history.map((event) => ({
      ...event,
      agent_id: "codex:other",
      payload: { ...event.payload, claimant: "codex:other" },
    }));
    const model = buildAgentHandoffs(data);
    expect(model.handoffs[0].state).toBe("unconfirmed");
    expect(model.handoffs[0].result).toBeNull();
    expect(model.unlinked).toHaveLength(2);
    data.villagers[0].history = [];
    expect(buildAgentHandoffs(data).handoffs).toHaveLength(0);
  });
  it("labels missing retained origins unlinked and distinguishes late session output from task completion", () => {
    const data = delegationSnapshot();
    data.tasks[0].state = "claimed";
    const late = data.villagers[1].history[1];
    late.type = "task_session_finished";
    late.payload = {
      ...late.payload,
      outcome: "ok",
      reason: "claim_lost",
      duration_s: 5,
    };
    expect(buildAgentHandoffs(data).handoffs[0].state).toBe("claimed");
    expect(buildAgentHandoffs(data).handoffs[0].result).toBeNull();
    data.villagers[0].history = [];
    expect(buildAgentHandoffs(data).unlinked).toHaveLength(2);
  });
  it("shows pending only from the matching open task ledger, not silence after delivery", () => {
    const data = delegationSnapshot();
    data.villagers[1].history = [];
    data.tasks[0].state = "open";
    data.tasks[0].claimant = null;
    expect(buildAgentHandoffs(data).handoffs[0].state).toBe("open");
    data.tasks = [];
    expect(buildAgentHandoffs(data).handoffs[0].state).toBe("unconfirmed");
  });
  it("deduplicates retained events and refuses conflicting delegation identities", () => {
    const data = delegationSnapshot();
    data.villagers[0].history.push(
      structuredClone(data.villagers[0].history[0]),
    );
    expect(buildAgentHandoffs(data).handoffs[0].transitions).toHaveLength(2);
    data.villagers[0].history.push({
      ...data.villagers[0].history[0],
      payload: {
        ...data.villagers[0].history[0].payload,
        to: "different-agent",
      },
    });
    expect(buildAgentHandoffs(data).handoffs).toHaveLength(0);
  });
  it("keeps blocked resident-address attempts and explicit session lineage separate from delivered work", () => {
    const data = delegationSnapshot();
    data.approvals = [
      {
        request_id: "refusal-1",
        agent_id: data.villagers[0].id,
        action: "rejected_delegation",
        state: "pending",
        message: "Sender tried to delegate, refused",
        detail: {
          to: "receiver-agent",
          title: "Read this",
          reason: "not_permitted",
          problem: "Sender may not delegate",
        },
        opened_at: "2026-09-05T10:05:00.000Z",
      },
    ];
    data.villagers[1].lineage = { parent_agent_id: data.villagers[0].id };
    const model = buildAgentHandoffs(data, data.villagers[0].id);
    expect(model.blocked[0]).toMatchObject({
      recipient: "receiver-agent",
      reason: "Sender may not delegate",
    });
    expect(model.blocked[0]).not.toHaveProperty("to");
    expect(model.lineage).toEqual([
      {
        id: data.villagers[1].id,
        parent: data.villagers[0].id,
        child: data.villagers[1].id,
      },
    ]);
    expect(buildAgentHandoffs(data, "unrelated").handoffs).toHaveLength(0);
  });
});

it("treats a retained lease expiry as reopening, never as a terminal result", () => {
  const data = delegationSnapshot();
  data.tasks = [];
  const expiry = data.villagers[1].history[1];
  expiry.type = "task_failed";
  expiry.payload.reason = " lease_expired ";
  const handoff = buildAgentHandoffs(data).handoffs[0];
  expect(handoff.state).toBe("open");
  expect(handoff.result).toBeNull();
});

it("uses Chronicle semantic ordering when a claim and a lease expiry share a timestamp", () => {
  const data = delegationSnapshot();
  data.tasks = [];
  const claimed = data.villagers[1].history[0];
  const expiry = {
    ...claimed,
    type: "task_failed",
    payload: { ...claimed.payload, reason: "lease_expired" },
  };
  data.villagers[1].history = [claimed, expiry];
  expect(buildAgentHandoffs(data).handoffs[0].state).toBe("claimed");
  data.villagers[1].history.reverse();
  expect(buildAgentHandoffs(data).handoffs[0].state).toBe("claimed");
});

it("gives a same-time completion precedence over failure independent of retained array order", () => {
  const data = delegationSnapshot();
  data.tasks = [];
  const done = data.villagers[1].history[1];
  const failed = {
    ...done,
    type: "task_failed",
    payload: { ...done.payload, reason: "Run failed" },
  };
  data.villagers[1].history.push(failed);
  expect(buildAgentHandoffs(data).handoffs[0]).toMatchObject({
    state: "done",
    result: { type: "task_done" },
  });
  data.villagers[1].history.reverse();
  expect(buildAgentHandoffs(data).handoffs[0]).toMatchObject({
    state: "done",
    result: { type: "task_done" },
  });
});
