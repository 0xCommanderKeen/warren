const ACTIVE_STATES = new Set(["active", "working", "knocking", "thinking"]);

export function agentUuid(id) {
  const value = String(id || "");
  const separator = value.indexOf(":");
  return separator < 0 ? value : value.slice(separator + 1);
}

export function eventFeed(villagers) {
  return villagers
    .flatMap((villager) =>
      (villager.history || []).map((event, index) => ({
        ...event,
        agent_id: event.agent_id || villager.id,
        agent_name: villager.name,
        _stable: `${villager.id}:${index}`,
      })),
    )
    .sort(
      (left, right) =>
        Date.parse(right.ts) - Date.parse(left.ts) ||
        left._stable.localeCompare(right._stable),
    );
}

export function viewModel(snapshot) {
  const byFile = new Map(snapshot.residents.map((item) => [item.file, item]));
  const byAgent = new Map(
    snapshot.residents
      .filter((item) => item.match?.agent_id)
      .map((item) => [item.match.agent_id, item]),
  );
  const people = snapshot.villagers.map((villager, index) => {
    const manifest = byFile.get(villager.resident_file) || byAgent.get(villager.id);
    const angle =
      (index / Math.max(snapshot.villagers.length, 1)) * Math.PI * 2 - Math.PI / 2;
    const radius = 31 + (index % 3) * 7;
    return {
      ...villager,
      manifest,
      hasPage: villager.residency === "resident" && Boolean(manifest),
      role: manifest?.meta?.role,
      capabilities: manifest?.capabilities,
      body: manifest?.body,
      x: 50 + Math.cos(angle) * radius,
      y: 50 + Math.sin(angle) * radius,
    };
  });
  return {
    snapshot,
    people,
    tasks: snapshot.tasks,
    approvals: snapshot.approvals,
    artifacts: snapshot.artifacts || [],
    journals: snapshot.journals || [],
    routines: snapshot.routines || [],
    diagnostics: snapshot.diagnostics || [],
    events: eventFeed(people),
    active: people.filter((person) => ACTIVE_STATES.has(person.state)).length,
  };
}

export function payloadSummary(event) {
  const payload = event.payload || {};
  return (
    payload.message ||
    payload.tool ||
    payload.command ||
    payload.task ||
    payload.status ||
    Object.entries(payload)
      .slice(0, 3)
      .map(([key, value]) =>
        `${key}: ${typeof value === "object" ? JSON.stringify(value) : value}`,
      )
      .join(" · ")
  );
}

export function related(items, id, predicate) {
  return (items || []).filter((item) =>
    predicate ? predicate(item, id) : item.agent_id === id,
  );
}
