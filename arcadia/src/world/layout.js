import { pendingApprovals } from "../contract/approvals.js";

const SPACING = 10;
const GROUP_SIZE = 12;
const CIVIC_PLOTS = [["square", [0, 0]], ["lodge:0", [-10, 0]],
  ["archive", [10, 0]], ["noticeboard", [0, -10]]];
const MAX_SAVED_ENTRIES = 10000;
const COLORS = ["#d88b61", "#668f87", "#8396b5", "#b393be", "#c6a34f", "#ae696d"];
const SKINS = ["#eac29f", "#bd8864", "#86583f", "#d8a580"];

function hash(value) {
  let result = 2166136261;
  for (const char of value) result = Math.imul(result ^ char.charCodeAt(0), 16777619);
  return result >>> 0;
}

function appearance(agent) {
  const seed = hash(agent.id);
  return {
    body: /^#[0-9a-f]{6}$/i.test(agent.accent) ? agent.accent : COLORS[seed % COLORS.length],
    hat: COLORS[(seed >>> 4) % COLORS.length],
    skin: SKINS[(seed >>> 8) % SKINS.length],
    variant: seed % 4,
  };
}

// Square rings keep old plots fixed while the village grows in all directions.
function* plots() {
  for (let ring = 1; ; ring += 1) {
    for (let x = -ring; x <= ring; x += 1) yield [x * SPACING, -ring * SPACING];
    for (let z = -ring + 1; z <= ring; z += 1) yield [ring * SPACING, z * SPACING];
    for (let x = ring - 1; x >= -ring; x -= 1) yield [x * SPACING, ring * SPACING];
    for (let z = ring - 1; z > -ring; z -= 1) yield [-ring * SPACING, z * SPACING];
  }
}

function front(building, slot = 0) {
  return [building.position[0] + (slot % 6 - 2.5) * 0.8,
    building.position[1] + 3 + Math.floor(slot / 6) * 0.8];
}

function route(from, source, to, target) {
  if (from[0] === to[0] && from[1] === to[1]) return [from];
  const sourceStreet = source.position[1] + 5;
  const targetStreet = target.position[1] + 5;
  const avenue = source.position[0] + 5;
  return [from, [from[0], sourceStreet], [avenue, sourceStreet],
    [avenue, targetStreet], [to[0], targetStreet], to]
    .filter((point, index, points) => index === 0 || point.some((v, axis) => v !== points[index - 1][axis]));
}

function validSavedState(value) {
  if (!value || value.version !== 1 || !Array.isArray(value.allocated) || !Array.isArray(value.groups)) return false;
  if (value.allocated.length < CIVIC_PLOTS.length || value.allocated.length > MAX_SAVED_ENTRIES || value.groups.length > MAX_SAVED_ENTRIES) return false;
  const id = item => typeof item === "string" && item.length > 0 && item.length <= 1024;
  const coordinate = item => Number.isFinite(item) && Math.abs(item) <= 100000;
  const plots = new Map();
  const positions = new Set();
  // A sparse forged allocation must not produce a giant terrain/garden loop.
  const maxReach = SPACING * (Math.ceil(Math.sqrt(value.allocated.length)) + 1);
  for (const entry of value.allocated) {
    if (!Array.isArray(entry) || entry.length !== 2 || !id(entry[0]) || plots.has(entry[0])) return false;
    const point = entry[1];
    if (!Array.isArray(point) || point.length !== 2 || !point.every(item => coordinate(item) && Math.abs(item) <= maxReach && item % SPACING === 0) || positions.has(String(point))) return false;
    plots.set(entry[0], point); positions.add(String(point));
  }
  for (const [key, point] of CIVIC_PLOTS) if (String(plots.get(key)) !== String(point)) return false;
  const groupNames = new Set();
  let total = 0;
  for (const entry of value.groups) {
    if (!Array.isArray(entry) || entry.length !== 2 || !id(entry[0]) || groupNames.has(entry[0]) || !Array.isArray(entry[1])) return false;
    groupNames.add(entry[0]);
    total += entry[1].length;
    if (total > MAX_SAVED_ENTRIES) return false;
    const ids = new Set();
    const indices = new Set();
    for (const member of entry[1]) {
      if (!Array.isArray(member) || member.length !== 2 || !id(member[0]) || ids.has(member[0]) ||
        !Number.isInteger(member[1]) || member[1] < 0 || member[1] >= entry[1].length || indices.has(member[1])) return false;
      ids.add(member[0]); indices.add(member[1]);
    }
  }
  if (value.bounds === null) return value.allocated.length === CIVIC_PLOTS.length && total === 0;
  const bounds = value.bounds;
  if (!bounds || ![bounds.minX, bounds.maxX, bounds.minZ, bounds.maxZ].every(coordinate) || bounds.minX >= bounds.maxX || bounds.minZ >= bounds.maxZ) return false;
  const points = [...plots.values()];
  return bounds.minX === Math.min(...points.map(point => point[0])) - 8 &&
    bounds.maxX === Math.max(...points.map(point => point[0])) + 8 &&
    bounds.minZ === Math.min(...points.map(point => point[1])) - 8 &&
    bounds.maxZ === Math.max(...points.map(point => point[1])) + 8;
}

/** Own one instance per village feed; optional versioned storage keeps plots across visits. */
export function createVillageLayout(savedState = null) {
  const saved = validSavedState(savedState) ? savedState : null;
  const allocated = new Map((saved?.allocated ?? CIVIC_PLOTS).map(([id, point]) => [id, [...point]]));
  const occupied = new Set([...allocated.values()].map(String));
  const available = plots();
  const groups = new Map((saved?.groups ?? []).map(([id, members]) => [id, new Map(members)]));
  const previousDestinations = new Map();
  let landscapeBounds = saved?.bounds ? { ...saved.bounds } : null;

  function position(id) {
    if (!allocated.has(id)) {
      let next;
      do { next = available.next().value; } while (occupied.has(String(next)));
      allocated.set(id, next);
      occupied.add(String(next));
    }
    return [...allocated.get(id)];
  }

  function membership(group, id) {
    if (!groups.has(group)) groups.set(group, new Map());
    const members = groups.get(group);
    if (!members.has(id)) members.set(id, members.size);
    const index = members.get(id);
    return { batch: Math.floor(index / GROUP_SIZE), slot: index % GROUP_SIZE };
  }

  return {
    serialize() {
      // Presentation identity only: never store snapshots, history or agent messages.
      const state = { version: 1,
        allocated: [...allocated].map(([id, point]) => [id, [...point]]),
        groups: [...groups].map(([id, members]) => [id, [...members].map(entry => [...entry])]),
        bounds: landscapeBounds ? { ...landscapeBounds } : null };
      return validSavedState(state) ? state : null;
    },
    update(snapshot) {
      const buildings = new Map();
      function building(id, kind, name, project) {
        if (!buildings.has(id)) buildings.set(id, {
          id, kind, name, position: position(id), width: kind === "home" ? 4 : 5,
          depth: 4, agentIds: [], ...(project === undefined ? {} : { project }),
        });
        return buildings.get(id);
      }
      const square = building("square", "square", "Village square");
      building("lodge:0", "lodge", "Visitor lodge");
      building("archive", "archive", "Archive");
      building("noticeboard", "noticeboard", "Notice board");
      const pending = new Set(pendingApprovals(snapshot.approvals ?? []).map(item => item.agent_id));
      // Home numbers guide initial ordering, never become world coordinates.
      const people = [...(snapshot.villagers ?? [])].sort((a, b) => {
        const home = value => Number.isSafeInteger(value) && value >= 0 && value < 10000 ? value : 10000;
        return home(a.home) - home(b.home) || a.id.localeCompare(b.id);
      });
      const agents = people.map(agent => {
        let dwelling;
        let homeSlot = 2;
        if (agent.residency === "resident") {
          dwelling = building(`home:${agent.id}`, "home", `${agent.name}’s home`);
        } else {
          const { batch, slot } = membership("visitors", agent.id);
          dwelling = building(`lodge:${batch}`, "lodge", batch ? `Visitor lodge ${batch + 1}` : "Visitor lodge");
          homeSlot = slot;
        }
        dwelling.agentIds.push(agent.id);
        let destinationBuilding = dwelling;
        let destinationSlot = homeSlot;
        const project = agent.project?.trim();
        let workshop;
        if (project) {
          const { batch, slot } = membership(`project:${project}`, agent.id);
          workshop = building(`workshop:${project}:${batch}`, "workshop",
            batch ? `${project} · workshop ${batch + 1}` : `${project} workshop`, project);
          workshop.agentIds.push(agent.id);
          if (agent.state === "working") {
            destinationBuilding = workshop;
            destinationSlot = slot;
          }
        }
        if (pending.has(agent.id)) {
          const { batch, slot } = membership("attention", agent.id);
          destinationBuilding = batch ? building(`square:${batch}`, "square", `Gathering square ${batch + 1}`) : square;
          destinationBuilding.agentIds.push(agent.id);
          destinationSlot = slot;
        }
        const arrival = front(dwelling, homeSlot);
        const destination = front(destinationBuilding, destinationSlot);
        const previous = previousDestinations.get(agent.id);
        const unchanged = previous && previous.destination.every((value, axis) => value === destination[axis]);
        const journey = unchanged ? previous.route : route(previous?.destination ?? arrival,
          previous?.building ?? dwelling, destination, destinationBuilding);
        previousDestinations.set(agent.id, { destination, building: destinationBuilding, route: journey });
        return { ...agent, position: arrival, destination,
          route: journey,
          buildingId: destinationBuilding.id, appearance: appearance(agent) };
      });
      const list = [...buildings.values()];
      // Retain roads and terrain beneath journeys queued by the renderer, even after
      // their destination building disappears. Plot reservations already persist.
      const bounds = {
        minX: Math.min(landscapeBounds?.minX ?? Infinity, ...list.map(item => item.position[0] - 8)),
        maxX: Math.max(landscapeBounds?.maxX ?? -Infinity, ...list.map(item => item.position[0] + 8)),
        minZ: Math.min(landscapeBounds?.minZ ?? Infinity, ...list.map(item => item.position[1] - 8)),
        maxZ: Math.max(landscapeBounds?.maxZ ?? -Infinity, ...list.map(item => item.position[1] + 8)),
      };
      landscapeBounds = { ...bounds };
      const roads = [];
      // Shared street lattice guarantees routes remain clear even after new plots arrive.
      for (let x = Math.ceil((bounds.minX + 3) / 10) * 10 - 5; x < bounds.maxX; x += 10) {
        roads.push({ from: [x, bounds.minZ + 3], to: [x, bounds.maxZ - 3], width: 1.25 });
      }
      for (let z = Math.ceil((bounds.minZ + 3) / 10) * 10 - 5; z < bounds.maxZ; z += 10) {
        roads.push({ from: [bounds.minX + 3, z], to: [bounds.maxX - 3, z], width: 1.25 });
      }
      for (const item of list) roads.push({ from: [item.position[0], item.position[1] + 2],
        to: [item.position[0], item.position[1] + 5], width: 0.9 });
      return { buildings: list, agents, roads, bounds };
    },
  };
}
