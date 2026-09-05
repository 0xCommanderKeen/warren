/** Current projected location, never dwelling membership or historical attendance. */
export function buildingOccupancy(world, building) {
  const agents = world.agents.filter(agent => agent.buildingId === building.id);
  const count = agents.length;
  const summary = building.kind === "workshop" ? `${count} working`
    : building.kind === "square" ? (count ? `${count} need you` : "Quiet")
    : ["home", "lodge"].includes(building.kind) ? `${count} inside`
    : building.kind === "archive" ? "Village records" : "Village requests";
  const names = agents.slice(0, 3).map(agent => agent.name).join(", ");
  return { agents, count, summary, preview: names + (count > 3 ? ` +${count - 3} more` : "") };
}
