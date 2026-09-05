const LIMIT = 10000;

function valid(value) {
  if (!value || value.version !== 1 || !Array.isArray(value.rooms) || value.rooms.length > 1000) return false;
  const roomIds = new Set();
  const isId = id => typeof id === "string" && id.length > 0 && id.length <= 512;
  let total = 0;
  for (const room of value.rooms) {
    if (!Array.isArray(room) || room.length !== 2 || !isId(room[0]) || roomIds.has(room[0]) || !Array.isArray(room[1])) return false;
    roomIds.add(room[0]); total += room[1].length;
    if (total > LIMIT) return false;
    const ids = new Set(), slots = new Set();
    for (const member of room[1]) {
      if (!Array.isArray(member) || member.length !== 2 || !isId(member[0]) || ids.has(member[0]) ||
        !Number.isInteger(member[1]) || member[1] < 0 || member[1] >= LIMIT || slots.has(member[1])) return false;
      ids.add(member[0]); slots.add(member[1]);
    }
  }
  return true;
}

function point(slot) {
  if (slot === 0) return [0, 0];
  const ring = Math.ceil((Math.sqrt(slot + 1) - 1) / 2);
  const offset = slot - (2 * ring - 1) ** 2;
  const side = 2 * ring;
  const coordinates = offset < side ? [-ring + offset, -ring] :
    offset < side * 2 ? [ring, -ring + offset - side] :
    offset < side * 3 ? [ring - (offset - side * 2), ring] :
    [-ring, ring - (offset - side * 3)];
  return coordinates.map(value => value * 3.6);
}

export function createRoomLayout(saved = null) {
  const rooms = new Map((valid(saved) ? saved.rooms : []).map(([id, members]) => [id, new Map(members)]));
  return {
    update(buildingId, agents, villagers = agents) {
      if (!rooms.has(buildingId)) rooms.set(buildingId, new Map());
      const members = rooms.get(buildingId);
      const current = new Map(agents.map(agent => [agent.id, agent]));
      if (buildingId === "workshop") {
        // Room absence is not session departure: resting/knocking agents still
        // reserve their desk until Chronicle removes them from the live roster.
        const live = new Set(villagers.map(agent => agent.id));
        for (const id of members.keys()) if (!live.has(id)) members.delete(id);
      }
      const occupied = new Set(members.values());
      let slot = 0;
      for (const id of [...current.keys()].sort()) if (!members.has(id)) {
        while (occupied.has(slot)) slot++;
        members.set(id, slot);
        occupied.add(slot);
      }
      const stations = [...members].map(([id, slot]) => ({ id, slot, position: point(slot), agent: current.get(id) ?? null }));
      const reach = stations.reduce((max, station) => Math.max(max, ...station.position.map(Math.abs)), 0);
      return { stations, width: Math.max(11, reach * 2 + 6), depth: Math.max(11, reach * 2 + 6) };
    },
    serialize() {
      const state = { version: 1, rooms: [...rooms].map(([id, members]) => [id, [...members].map(member => [...member])]) };
      return valid(state) ? state : null;
    },
  };
}
