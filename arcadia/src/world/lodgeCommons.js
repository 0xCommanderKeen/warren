/** A front sitting room extends the lodge without moving any reserved bed. */
export function lodgeCommons(agents, width, bedroomDepth) {
  const groups = new Map();
  for (const agent of agents) {
    const project = agent.project?.trim() || "No project recorded";
    if (!groups.has(project)) groups.set(project, []);
    groups.get(project).push(agent.id);
  }
  const columns = Math.max(1, Math.floor((width - 2) / 4.2));
  const entries = [...groups].sort(([a], [b]) => a.localeCompare(b));
  const rows = Math.max(1, Math.ceil(entries.length / columns));
  return { extension: rows * 3.2 + 2,
    tables: (entries.length ? entries : [["Common room", []]]).map(([project, agentIds], index) => ({
      project, agentIds,
      position: [(index % columns - (columns - 1) / 2) * 4.2, bedroomDepth / 2 + 2 + Math.floor(index / columns) * 3.2],
    })) };
}
