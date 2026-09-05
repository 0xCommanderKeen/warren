const taskTypes = new Set([
  "task_claimed",
  "task_done",
  "task_failed",
  "task_session_finished",
]);
const text = (value) => typeof value === "string" && value.trim().length > 0;

const leaseExpired = (event) =>
  event.type === "task_failed" &&
  event.payload.reason?.trim() === "lease_expired";
// Match Chronicle's semantic tie order: lease reopening precedes claims, then closes.
const taskRank = (event) =>
  leaseExpired(event)
    ? 1
    : event.type === "task_claimed"
      ? 2
      : event.type === "task_failed"
        ? 3
        : event.type === "task_done"
          ? 4
          : 5;
function taskIdentity(event) {
  const payload = event.payload;
  return [
    event.type,
    event.ts,
    event.agent_id,
    event.project,
    payload.task_id,
    payload.title,
    payload.claimant || "",
    JSON.stringify(payload.required_skills || []),
    JSON.stringify(payload.artifacts || []),
    payload.reason || "",
    payload.parent_task_id || "",
  ].join("\0");
}
const compareText = (left, right) => (left < right ? -1 : left > right ? 1 : 0);
const compareTaskEvents = (left, right) =>
  compareText(left.ts, right.ts) ||
  taskRank(left) - taskRank(right) ||
  compareText(taskIdentity(left), taskIdentity(right));

/** Reconstruct only explicit retained identities; proximity, prose, and projects are never joins. */
export function buildAgentHandoffs(snapshot, agentId = null) {
  const all = new Map();
  for (const agent of snapshot.villagers)
    for (const event of agent.history || []) {
      const key = JSON.stringify([
        event.ts,
        event.source,
        event.agent_id,
        event.type,
        event.payload,
      ]);
      if (!all.has(key)) all.set(key, { ...event, key });
    }
  const events = [...all.values()].sort(
    (a, b) => a.ts.localeCompare(b.ts) || a.key.localeCompare(b.key),
  );
  const origins = new Map(),
    unlinked = [];
  for (const event of events.filter(
    (event) => event.type === "task_delegated",
  )) {
    const payload = event.payload;
    if (
      event.source !== "steward" ||
      !text(payload.task_id) ||
      !text(payload.from) ||
      !text(payload.to) ||
      payload.from !== event.agent_id
    ) {
      unlinked.push({
        id: event.key,
        event,
        reason: "Delegation endpoints could not be verified.",
      });
      continue;
    }
    if (!origins.has(payload.task_id)) origins.set(payload.task_id, []);
    origins.get(payload.task_id).push(event);
  }
  const handoffs = [];
  const accepted = new Set();
  for (const [taskId, candidates] of origins) {
    const endpoints = new Set(
      candidates.map((event) =>
        JSON.stringify([
          event.payload.from,
          event.payload.to,
          event.payload.parent_task_id,
        ]),
      ),
    );
    if (endpoints.size !== 1) {
      candidates.forEach((event) =>
        unlinked.push({
          id: event.key,
          event,
          reason:
            "Conflicting delegation identities; no relationship inferred.",
        }),
      );
      continue;
    }
    const origin = candidates[0],
      { from, to, title, route, parent_task_id: parentTaskId } = origin.payload;
    const transitions = events
      .filter(
        (event) =>
          taskTypes.has(event.type) &&
          event.payload.task_id === taskId &&
          event.source === "steward" &&
          event.agent_id === to &&
          event.payload.claimant === to &&
          event.ts >= origin.ts,
      )
      .sort(compareTaskEvents);
    transitions.forEach((event) => accepted.add(event.key));
    const row = snapshot.tasks.find((task) => task.id === taskId);
    const matchingRow =
      row &&
      row.posted_by === from &&
      row.assignee === to &&
      (!row.claimant || row.claimant === to);
    // A late session report is explicitly NOT a terminal task fact.
    const terminal = transitions
      .filter(
        (event) =>
          event.type === "task_done" ||
          (event.type === "task_failed" && !leaseExpired(event)),
      )
      .at(-1);
    const latest = transitions
      .filter((event) => event.type !== "task_session_finished")
      .at(-1);
    const state = matchingRow
      ? row.state
      : row
        ? "unconfirmed"
        : latest
          ? {
              task_claimed: "claimed",
              task_done: "done",
              task_failed: leaseExpired(latest) ? "open" : "failed",
            }[latest.type]
          : "unconfirmed";
    handoffs.push({
      id: taskId,
      from,
      to,
      title,
      route,
      parentTaskId: parentTaskId || null,
      origin,
      transitions,
      state,
      stateSource: matchingRow ? "task ledger" : "retained history",
      result: terminal
        ? {
            type: terminal.type,
            ts: terminal.ts,
            artifacts: Array.isArray(terminal.payload.artifacts)
              ? terminal.payload.artifacts.filter(text)
              : [],
            reason:
              typeof terminal.payload.reason === "string"
                ? terminal.payload.reason
                : null,
            runId: terminal.payload.run_id || null,
          }
        : null,
      updatedAt: matchingRow
        ? row.updated_at
        : transitions.at(-1)?.ts || origin.ts,
    });
  }
  for (const event of events)
    if (taskTypes.has(event.type) && !accepted.has(event.key)) {
      unlinked.push({
        id: event.key,
        event,
        reason: origins.has(event.payload.task_id)
          ? "Task record does not match the delegated recipient or retained origin."
          : "No matching handoff origin is retained for this task record.",
      });
    }
  const blocked = snapshot.approvals
    .filter((approval) =>
      ["rejected_delegation", "unreadable_delegation"].includes(
        approval.action,
      ),
    )
    .map((approval) => ({
      id: approval.request_id,
      from: approval.agent_id,
      recipient:
        typeof approval.detail?.to === "string" ? approval.detail.to : null,
      title:
        typeof approval.detail?.title === "string"
          ? approval.detail.title
          : approval.message,
      reason:
        typeof approval.detail?.problem === "string"
          ? approval.detail.problem
          : typeof approval.detail?.reason === "string"
            ? approval.detail.reason
            : approval.message,
      state: approval.state,
      openedAt: approval.opened_at,
    }));
  const lineage = snapshot.villagers.flatMap((agent) => {
    const parents = new Set(
      [
        agent.lineage?.parent_agent_id,
        ...(agent.history || []).map((event) => event.payload?.parent_agent_id),
      ].filter(text),
    );
    // Session lineage is displayed separately; it proves no task or reply delivery.
    return parents.size === 1
      ? [{ id: agent.id, parent: [...parents][0], child: agent.id }]
      : [];
  });
  return {
    handoffs: handoffs
      .filter(
        (item) => !agentId || item.from === agentId || item.to === agentId,
      )
      .sort(
        (a, b) =>
          b.updatedAt.localeCompare(a.updatedAt) || a.id.localeCompare(b.id),
      ),
    blocked: blocked.filter((item) => !agentId || item.from === agentId),
    unlinked: unlinked.filter(
      (item) => !agentId || item.event.agent_id === agentId,
    ),
    lineage: lineage.filter(
      (item) => !agentId || item.parent === agentId || item.child === agentId,
    ),
  };
}
