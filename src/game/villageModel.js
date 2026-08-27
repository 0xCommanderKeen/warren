function property(object, name) {
  return object.properties?.find((item) => item.name === name)?.value;
}

function layer(map, name, type) {
  const found = map.layers.find((item) => item.name === name && item.type === type);
  if (!found) throw new Error(`Village map is missing ${name}`);
  return found;
}

function collidingGids(map) {
  const gids = new Set();
  for (const tileset of map.tilesets) {
    for (const tile of tileset.tiles ?? []) {
      if (property(tile, "collides") === true) gids.add(tileset.firstgid + tile.id);
    }
  }
  return gids;
}

function walkability(map) {
  const terrain = layer(map, "Terrain", "tilelayer");
  const collision = layer(map, "Collision", "tilelayer");
  const collides = collidingGids(map);
  return terrain.data.map(
    (gid, index) => gid !== 0 && !collides.has(collision.data[index]),
  );
}

function tileIndex(map, point) {
  return Math.floor(point.y / map.tileheight) * map.width +
    Math.floor(point.x / map.tilewidth);
}

function traverseMap(map, start, finish) {
  const walkable = walkability(map);
  const queue = [start];
  const previous = new Map([[start, null]]);

  for (let cursor = 0; cursor < queue.length; cursor += 1) {
    const index = queue[cursor];
    if (index === finish) break;
    const x = index % map.width;
    const neighbors = [];
    if (x > 0) neighbors.push(index - 1);
    if (x < map.width - 1) neighbors.push(index + 1);
    if (index >= map.width) neighbors.push(index - map.width);
    if (index < map.width * (map.height - 1)) neighbors.push(index + map.width);
    for (const next of neighbors) {
      if (!walkable[next] || previous.has(next)) continue;
      previous.set(next, index);
      queue.push(next);
    }
  }
  return previous;
}

function routeBetween(map, from, to) {
  const start = tileIndex(map, from);
  const finish = tileIndex(map, to);
  const previous = traverseMap(map, start, finish);

  if (!previous.has(finish)) throw new Error(`No walkable route from ${from.name} to ${to.name}`);
  const route = [];
  for (let index = finish; index !== start; index = previous.get(index)) {
    route.push({
      x: (index % map.width) * map.tilewidth + map.tilewidth / 2,
      y: Math.floor(index / map.width) * map.tileheight + map.tileheight / 2,
    });
  }
  return route.reverse();
}

function placeObjects(map) {
  return layer(map, "Places", "objectgroup").objects;
}

function place(map, kind, value) {
  const found = placeObjects(map).find(
    (object) => property(object, "kind") === kind &&
      (value === undefined || property(object, "home") === value),
  );
  if (!found) throw new Error(`Village map is missing ${kind}${value ?? ""}`);
  return found;
}

export function validateReachability(map) {
  const walkable = walkability(map);
  const street = place(map, "street");
  const start = tileIndex(map, street);
  const seen = traverseMap(map, start);

  const total = walkable.filter(Boolean).length;
  const unreachable = total - seen.size;
  if (unreachable) {
    throw new Error(
      `Village map has ${unreachable} unreachable walkable ${unreachable === 1 ? "tile" : "tiles"}`,
    );
  }
  return { walkable: total, reachable: seen.size };
}

export function buildVillageModel(map, snapshotVillagers) {
  validateReachability(map);
  const lodge = place(map, "lodge");
  const work = place(map, "work");
  let visitorIndex = 0;

  return snapshotVillagers.map((villager) => {
    const isResident = villager.residency === "resident";
    const anchor = isResident ? place(map, "home", villager.home) : lodge;
    const door = isResident ? place(map, "door", villager.home) : place(map, "lodge-door");
    const offset = isResident ? 0 : visitorIndex++ * 16;
    const moving = villager.state === "working";

    return {
      ...villager,
      dwelling: {
        kind: isResident ? "home" : "lodge",
        label: anchor.name,
        x: anchor.x,
        y: anchor.y,
      },
      x: door.x + offset,
      y: door.y,
      moving,
      route: moving ? routeBetween(map, door, work) : [],
    };
  });
}
