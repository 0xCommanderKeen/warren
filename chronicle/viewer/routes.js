(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowRoutes = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // Capacity is a map promise, not an accident of how far an Array can grow.
  const CAPACITY = Object.freeze({ shared: 16, lodge: 32, knock: 16, plot: 16 });
  const MAX_EXTRA_POINTS = 256;
  const SHARED_COLUMNS = Object.freeze([-28, -20, -12, -4, 4, 12, 20, 28]);

  class Heap {
    constructor() { this.nodes = []; this.costs = []; }
    get size() { return this.nodes.length; }
    push(node, cost) {
      let i = this.nodes.length;
      this.nodes.push(node); this.costs.push(cost);
      while (i) {
        const parent = (i - 1) >> 1;
        if (this.costs[parent] <= this.costs[i]) break;
        this.swap(parent, i); i = parent;
      }
    }
    pop() {
      const top = this.nodes[0], last = this.nodes.length - 1;
      this.swap(0, last); this.nodes.pop(); this.costs.pop();
      let i = 0;
      for (;;) {
        const left = i * 2 + 1, right = left + 1;
        let smallest = i;
        if (left < this.nodes.length && this.costs[left] < this.costs[smallest]) smallest = left;
        if (right < this.nodes.length && this.costs[right] < this.costs[smallest]) smallest = right;
        if (smallest === i) break;
        this.swap(i, smallest); i = smallest;
      }
      return top;
    }
    swap(a, b) {
      [this.nodes[a], this.nodes[b]] = [this.nodes[b], this.nodes[a]];
      [this.costs[a], this.costs[b]] = [this.costs[b], this.costs[a]];
    }
  }

  function createSlotAllocator(capacityFor) {
    const buckets = new Map();
    function allocate(where, id) {
      const capacity = capacityFor(where);
      if (!Number.isInteger(capacity) || capacity < 1) return null;
      let slots = buckets.get(where);
      if (!slots) { slots = Array(capacity).fill(null); buckets.set(where, slots); }
      const held = slots.indexOf(id);
      if (held >= 0) return held;
      const free = slots.indexOf(null);
      if (free < 0) return null;
      slots[free] = id;
      return free;
    }
    function release(id, except) {
      for (const [where, slots] of buckets) {
        if (where === except) continue;
        const at = slots.indexOf(id);
        if (at >= 0) slots[at] = null;
      }
    }
    /* Reconcile a complete snapshot of claims as one transaction. Existing
       matching claims keep their slots; obsolete claims are released before
       missing claims are assigned, so a full A<->B exchange needs no spare
       slot. Sorting makes the result independent of snapshot iteration order. */
    function reconcile(claims, commit = true) {
      if (!Array.isArray(claims)) return null;
      const wanted = new Map();
      for (const claim of claims) {
        if (!claim || typeof claim.id !== "string" || !claim.id ||
          (claim.where !== null && typeof claim.where !== "string")) return null;
        if (claim.where === null) continue;
        const key = `${claim.where}\0${claim.id}`;
        if (wanted.has(key)) return null;
        wanted.set(key, claim);
      }
      const next = new Map();
      const allWhere = new Set([...buckets.keys(), ...[...wanted.values()].map(c => c.where)]);
      for (const where of [...allWhere].sort()) {
        const capacity = capacityFor(where);
        if (!Number.isInteger(capacity) || capacity < 1) return null;
        const old = buckets.get(where) || [];
        const slots = Array(capacity).fill(null);
        for (let slot = 0; slot < Math.min(old.length, capacity); slot++) {
          const id = old[slot];
          if (id !== null && wanted.has(`${where}\0${id}`)) slots[slot] = id;
        }
        const missing = [...wanted.values()].filter(c => c.where === where && !slots.includes(c.id))
          .sort((a, b) => a.id.localeCompare(b.id));
        for (const claim of missing) {
          const free = slots.indexOf(null);
          if (free < 0) return null;
          slots[free] = claim.id;
        }
        if (slots.some(id => id !== null)) next.set(where, slots);
      }
      if (commit) {
        buckets.clear();
        for (const [where, slots] of next) buckets.set(where, slots);
      }
      return claims.map(claim => claim.where === null ? 0 :
        next.get(claim.where).indexOf(claim.id));
    }
    return Object.freeze({ allocate, release, reconcile });
  }

  function createRoutingMap(options) {
    const width = options.width, height = options.height, tileSize = options.tileSize;
    const roadCost = options.roadCost || 10, grassCost = options.grassCost || 16;
    const turnCost = options.turnCost || 2;
    const places = options.places, plots = options.plots, ownDoor = options.ownDoor;
    const blocked = options.blocked, costAt = options.costAt || (() => grassCost);
    const walkable = (x, y) => x >= 0 && y >= 0 && x < width && y < height && !blocked(x, y);
    const tileAt = (x, y) => ({ x: Math.floor(x / tileSize), y: Math.floor(y / tileSize) });
    const validPoint = point => point && Number.isFinite(point.x) && Number.isFinite(point.y) &&
      point.x >= 0 && point.y >= 0 && point.x < width * tileSize && point.y < height * tileSize;
    const placeDoor = id => {
      const p = places[id];
      return p && { x: p.tx * tileSize + p.w * tileSize / 2, y: (p.ty + p.h) * tileSize };
    };
    const plotDoor = plot => {
      const p = plots[plot];
      return p && { x: p[0] * tileSize + 24, y: (p[1] + 3) * tileSize };
    };

    function capacity(dest) {
      if (!dest) return 0;
      if (dest.kind === "door") return CAPACITY.knock;
      if (dest.kind === "plot") return CAPACITY.plot;
      if (dest.kind === "building") return dest.id === "visitor-lodge" ? CAPACITY.lodge :
        (places[dest.id] ? CAPACITY.shared : 0);
      return 1;
    }

    function endpoint(dest, slot, state) {
      if (!dest || !Number.isInteger(slot) || slot < 0 || slot >= capacity(dest)) return null;
      if (dest.kind === "building") {
        const door = placeDoor(dest.id);
        const columns = dest.id === "visitor-lodge" ? SHARED_COLUMNS : SHARED_COLUMNS.slice(1, 7);
        return { x: door.x + columns[slot % columns.length],
          y: door.y + 6 + Math.floor(slot / columns.length) * 10, kind: dest.id };
      }
      if (dest.kind === "door") return { x: ownDoor.x + (slot % 8) * 10 - 35,
        y: ownDoor.y + 6 + Math.floor(slot / 8) * 10, kind: "knocking" };
      if (dest.kind === "plot") {
        const door = plotDoor(dest.plot);
        if (!door) return null;
        const offsets = [-6, -4, -2, 0, 2, 4, 6, 8];
        return { x: door.x + offsets[slot % 8],
          y: door.y + 2 + Math.floor(slot / 8) * 6, kind: "delegation" };
      }
      if (dest.kind === "home") {
        const door = plotDoor(dest.plot);
        if (!door) return null;
        return { x: door.x, y: door.y + (state === "working" ? 14 : 2), kind: state };
      }
      return null;
    }

    function findPath(sx, sy, gx, gy) {
      if (!walkable(sx, sy) || !walkable(gx, gy)) return null;
      const tiles = width * height, states = 5, noDirection = 4;
      const start = (sy * width + sx) * states + noDirection, goal = gy * width + gx;
      const distance = new Float64Array(tiles * states).fill(Infinity);
      const previous = new Int32Array(tiles * states).fill(-1);
      const done = new Uint8Array(tiles * states), open = new Heap();
      const directions = [[1,0],[-1,0],[0,1],[0,-1]];
      const estimate = (x, y) => roadCost * (Math.abs(x - gx) + Math.abs(y - gy));
      distance[start] = 0; open.push(start, estimate(sx, sy));
      let end = -1;
      while (open.size) {
        const current = open.pop();
        if (done[current]) continue;
        done[current] = 1;
        const tile = Math.floor(current / states);
        if (tile === goal) { end = current; break; }
        const incoming = current % states, x = tile % width, y = Math.floor(tile / width);
        for (let direction = 0; direction < 4; direction++) {
          const nx = x + directions[direction][0], ny = y + directions[direction][1];
          if (!walkable(nx, ny)) continue;
          const next = (ny * width + nx) * states + direction;
          const score = distance[current] + costAt(nx, ny) +
            (incoming !== noDirection && incoming !== direction ? turnCost : 0);
          if (score >= distance[next]) continue;
          distance[next] = score; previous[next] = current;
          open.push(next, score + estimate(nx, ny));
        }
      }
      if (end < 0) return null;
      const path = [];
      for (let at = end; at >= 0; at = previous[at]) {
        const tile = Math.floor(at / states);
        path.push({ x: tile % width, y: Math.floor(tile / width) });
        if (at === start) break;
      }
      return path.reverse();
    }

    function route(from, target) {
      if (!validPoint(from) || !validPoint(target)) return null;
      const start = tileAt(from.x, from.y), end = tileAt(target.x, target.y);
      if (!walkable(start.x, start.y) || !walkable(end.x, end.y)) return null;
      const tiles = findPath(start.x, start.y, end.x, end.y);
      if (!tiles) return null;
      const points = tiles.slice(1).map(t => ({ x: t.x * tileSize + tileSize / 2,
        y: t.y * tileSize + tileSize / 2 }));
      points.push({ x: target.x, y: target.y });
      const route = [], origin = { x: from.x, y: from.y };
      let prior = origin;
      for (let i = 0; i < points.length; i++) {
        const point = points[i], next = points[i + 1];
        if (next && ((prior.x === point.x && point.x === next.x) ||
          (prior.y === point.y && point.y === next.y))) continue;
        if (!walkable(tileAt(point.x, point.y).x, tileAt(point.x, point.y).y)) return null;
        route.push(point); prior = point;
      }
      return route;
    }

    function allocatableEndpoints() {
      const all = [];
      for (const id of Object.keys(places)) {
        const dest = { kind: "building", id };
        for (let slot = 0; slot < capacity(dest); slot++) all.push({ dest, slot, point: endpoint(dest, slot) });
      }
      for (let slot = 0; slot < CAPACITY.knock; slot++) {
        const dest = { kind: "door" }; all.push({ dest, slot, point: endpoint(dest, slot) });
      }
      for (let plot = 0; plot < plots.length; plot++) {
        for (const state of ["resting", "working"]) {
          const dest = { kind: "home", plot };
          all.push({ dest, slot: 0, point: endpoint(dest, 0, state) });
        }
        const dest = { kind: "plot", plot };
        for (let slot = 0; slot < capacity(dest); slot++) all.push({ dest, slot, point: endpoint(dest, slot) });
      }
      return all;
    }

    function validate(extraPoints = []) {
      const problems = [], endpoints = allocatableEndpoints();
      const points = [...endpoints];
      let extrasAreArray = false;
      try { extrasAreArray = Array.isArray(extraPoints); } catch (_) {}
      if (!extrasAreArray) problems.push("extra points must be an array");
      else {
        let extraCount;
        try { extraCount = extraPoints.length; } catch (_) {}
        if (!Number.isSafeInteger(extraCount) || extraCount < 0) {
          problems.push("extra points length invalid");
          extraCount = 0;
        } else if (extraCount > MAX_EXTRA_POINTS) {
          problems.push(`extra points exceed limit of ${MAX_EXTRA_POINTS}`);
          extraCount = MAX_EXTRA_POINTS;
        }
        for (let index = 0; index < extraCount; index++) {
          let point;
          let label = `extra:${index}`, x, y;
          try {
            point = extraPoints[index];
            if (point && typeof point === "object") {
              const name = point.name;
              if (typeof name === "string" && name) label = name;
              x = point.x; y = point.y;
            }
          } catch (_) {
            problems.push(`${label} invalid`); continue;
          }
          const normalized = { x, y };
          if (!validPoint(normalized)) { problems.push(`${label} invalid`); continue; }
          points.push({ point: normalized, label });
        }
      }
      let seed = null;
      for (const item of points) {
        if (!item.point) { problems.push("missing endpoint"); continue; }
        const tile = tileAt(item.point.x, item.point.y);
        const label = item.label ||
          `${item.dest.kind}:${item.dest.id ?? item.dest.plot ?? "knock"}:${item.slot}`;
        item.tile = tile; item.label = label;
        if (!walkable(tile.x, tile.y)) problems.push(`${label} blocked`);
        else if (!seed) seed = tile;
      }
      const seen = new Uint8Array(width * height), stack = [];
      if (seed) {
        const at = seed.y * width + seed.x;
        seen[at] = 1; stack.push(at);
      }
      while (stack.length) {
        const at = stack.pop(), x = at % width, y = Math.floor(at / width);
        for (const [dx, dy] of [[1,0],[-1,0],[0,1],[0,-1]]) {
          const nx = x + dx, ny = y + dy, next = ny * width + nx;
          if (!walkable(nx, ny) || seen[next]) continue;
          seen[next] = 1; stack.push(next);
        }
      }
      let walkableTiles = 0, reachableTiles = 0;
      for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
        if (walkable(x, y)) walkableTiles++;
        if (seen[y * width + x]) reachableTiles++;
      }
      for (const item of points) {
        if (item.tile && walkable(item.tile.x, item.tile.y) &&
            !seen[item.tile.y * width + item.tile.x]) problems.push(`${item.label} unreachable`);
      }
      const connected = reachableTiles === walkableTiles;
      if (!connected) problems.push("map disconnected");
      return { endpoints, problems, connected,
        reachableTiles, walkableTiles };
    }

    return Object.freeze({ capacity, endpoint, route, findPath, allocatableEndpoints,
      validate, tileAt, walkable });
  }

  return { CAPACITY, createSlotAllocator, createRoutingMap };
});
