import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { createArtKit } from "./art.js";
import { createRoomLayout } from "./roomLayout.js";

/** A furnished cutaway room. Furniture is illustrative; only supplied agents appear. */
export function createInteriorRenderer(host, { onSelect, onError } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setClearColor(0xece7da, 1);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  const canvas = renderer.domElement;
  canvas.dataset.testid = "interior-canvas";
  canvas.dataset.ready = "false";
  canvas.style.cssText = "display:block;width:100%;height:100%;touch-action:none;";
  host.appendChild(canvas);
  const labels = document.createElement("div");
  labels.style.cssText = "position:absolute;inset:0;pointer-events:none;overflow:hidden;";
  host.appendChild(labels);
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-12, 12, 12, -12, 0.1, 500);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.minPolarAngle = 0.35;
  controls.maxPolarAngle = Math.PI / 2.5;
  // Stay on the open side of the dollhouse; a room never disappears behind a wall.
  controls.minAzimuthAngle = 0.1;
  controls.maxAzimuthAngle = Math.PI / 2 - 0.1;
  controls.minZoom = 0.5;
  controls.maxZoom = 5;
  controls.screenSpacePanning = false;
  controls.mouseButtons = { LEFT: THREE.MOUSE.PAN, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.ROTATE };
  controls.touches = { ONE: THREE.TOUCH.PAN, TWO: THREE.TOUCH.DOLLY_ROTATE };
  const ambient = new THREE.HemisphereLight(0xfff0d4, 0x7b7965, 2);
  const sun = new THREE.DirectionalLight(0xffe2b2, 3);
  sun.position.set(8, 18, 10);
  sun.castShadow = true;
  sun.shadow.mapSize.set(1024, 1024);
  sun.shadow.normalBias = 0.04;
  scene.add(ambient, sun, sun.target);
  const art = createArtKit();
  const root = new THREE.Group();
  scene.add(root);
  const geometries = new Map();
  const materials = new Map();
  const batchMaterials = new Map();
  const names = [];
  const fallbackLayout = createRoomLayout();
  const knownNames = new Map();
  let disposed = false, frame = 0, needsRender = true, signature = "", buildingId = null;
  let extent = { width: 16, depth: 13 }, state = null, lastCommand = null;
  let lastFocusAgentId = null;
  const projected = new THREE.Vector3();
  const colors = {
    floor: "#b98b5e", floorEdge: "#866247", plank: "#c59b6e", wall: "#ece0c8",
    trim: "#987353", timber: "#755240", cream: "#f3e5c8", brass: "#c9a260",
    teal: "#587f77", navy: "#415f69", window: "#d8e9dd", leaf: "#698f62",
    pot: "#b76b4f", paper: "#f4e6c6", ink: "#35444a", rust: "#b9694d",
  };
  function geometry(type) {
    if (!geometries.has(type)) geometries.set(type,
      type === "cylinder" ? new THREE.CylinderGeometry(0.5, 0.5, 1, 10) :
      type === "cone" ? new THREE.ConeGeometry(0.5, 1, 10) :
      type === "sphere" ? new THREE.IcosahedronGeometry(0.5, 1) :
      new THREE.BoxGeometry(1, 1, 1));
    return geometries.get(type);
  }
  function material(color) {
    const key = colors[color] || color;
    if (!materials.has(key)) materials.set(key, new THREE.MeshStandardMaterial({ color: key, roughness: 0.9 }));
    return materials.get(key);
  }
  function part(group, color, xyz, size, type = "box") {
    const mesh = new THREE.Mesh(geometry(type), material(color));
    mesh.position.set(...xyz); mesh.scale.set(...size);
    mesh.castShadow = true; mesh.receiveShadow = true;
    group.add(mesh);
    return mesh;
  }
  function label(text, position, always = false, compact = text, present = true) {
    const element = document.createElement("div");
    element.textContent = text;
    element.title = text;
    element.style.cssText = "position:absolute;transform:translate(-50%,-100%);padding:4px 7px;border-radius:5px;background:#fcf8edee;color:#334d42;font:10px/1.3 Cousine,monospace;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;border:1px solid #5b6b4529;";
    if (always) element.style.cssText += "font-size:12px;background:#35594eee;color:#fff3d9;";
    labels.appendChild(element);
    names.push({ element, position: new THREE.Vector3(...position), always, text, compact, present });
  }
  function plant(group, x, z, size = 1) {
    part(group, "pot", [x, 0.22 * size, z], [0.5 * size, 0.44 * size, 0.5 * size], "cylinder");
    part(group, "leaf", [x, 0.74 * size, z], [0.83 * size, 1.0 * size, 0.74 * size], "sphere");
  }
  function lamp(group, x, z, y = 0) {
    part(group, "brass", [x, y + 0.08, z], [0.3, 0.1, 0.3], "cylinder");
    part(group, "brass", [x, y + 0.35, z], [0.05, 0.57, 0.05], "cylinder");
    part(group, "cream", [x, y + 0.63, z], [0.52, 0.4, 0.52], "cone");
  }
  function desk(group, x, z) {
    part(group, "timber", [x, 0.83, z + 0.42], [2.1, 0.16, 0.95]);
    for (const side of [-1, 1]) part(group, "trim", [x + side * 0.84, 0.4, z + 0.42], [0.14, 0.8, 0.65]);
    part(group, "navy", [x - 0.28, 1.13, z + 0.55], [0.67, 0.43, 0.11]);
    part(group, "window", [x - 0.28, 1.14, z + 0.484], [0.55, 0.31, 0.025]);
    part(group, "brass", [x - 0.28, 0.96, z + 0.54], [0.18, 0.13, 0.18]);
    part(group, "paper", [x + 0.25, 0.922, z + 0.11], [0.4, 0.025, 0.27]);
    lamp(group, x + 0.73, z + 0.5, 0.93);
    part(group, "teal", [x, 0.25, z - 0.62], [0.62, 0.17, 0.58]);
    part(group, "timber", [x, 0.12, z - 0.62], [0.35, 0.24, 0.35]);
  }
  function bed(group, x, z, accent) {
    part(group, "timber", [x + 0.45, 0.2, z], [1.32, 0.4, 2.12]);
    part(group, "timber", [x + 0.45, 0.62, z - 1.0], [1.4, 0.87, 0.12]);
    part(group, "cream", [x + 0.45, 0.45, z], [1.24, 0.22, 1.95]);
    part(group, accent || "teal", [x + 0.45, 0.59, z + 0.29], [1.24, 0.09, 1.26]);
    part(group, "paper", [x + 0.45, 0.62, z - 0.67], [0.85, 0.18, 0.38]);
    part(group, "trim", [x - 0.62, 0.3, z - 0.67], [0.58, 0.6, 0.6]);
    lamp(group, x - 0.62, z - 0.67, 0.61);
  }
  function batch(groups) {
    const batches = new Map();
    for (const group of groups) {
      group.updateMatrixWorld(true);
      group.traverse(mesh => {
        if (!mesh.isMesh) return;
        const key = `${mesh.geometry.uuid}:${mesh.material.type}:${mesh.material.roughness}`;
        if (!batchMaterials.has(key)) {
          const mat = mesh.material.clone();
          mat.color.set(0xffffff); mat.emissive?.set(0x000000);
          batchMaterials.set(key, mat);
        }
        if (!batches.has(key)) batches.set(key, { geometry: mesh.geometry, material: batchMaterials.get(key), items: [] });
        batches.get(key).items.push({ matrix: mesh.matrixWorld.clone(), color: mesh.material.color.clone(), selection: group.userData.selection });
      });
    }
    for (const batch of batches.values()) {
      const mesh = new THREE.InstancedMesh(batch.geometry, batch.material, batch.items.length);
      mesh.userData.selections = batch.items.map(item => item.selection);
      batch.items.forEach((item, i) => { mesh.setMatrixAt(i, item.matrix); mesh.setColorAt(i, item.color); });
      mesh.castShadow = true; mesh.receiveShadow = true;
      mesh.computeBoundingSphere(); root.add(mesh);
    }
  }
  function rebuild(building, occupants, layout) {
    root.children.forEach(mesh => mesh.dispose()); root.clear();
    names.splice(0).forEach(name => name.element.remove());
    const workshop = building.kind === "workshop";
    const sleeping = building.kind === "home" || building.kind === "lodge";
    const count = occupants.length;
    const { width, depth } = layout;
    extent = { width, depth };
    const room = new THREE.Group();
    const objects = [room];
    part(room, "floorEdge", [0, -0.25, 0], [width + 0.2, 0.5, depth + 0.2]);
    part(room, "floor", [0, 0.008, 0], [width, 0.06, depth]);
    // Long boards, architectural trim and a low cutaway edge make the scale tactile.
    const boardCount = Math.ceil(depth / 0.65);
    for (let i = 0; i < boardCount; i++) part(room, i % 3 === 0 ? "plank" : "floor", [0, 0.044, -depth / 2 + (i + 0.5) * depth / boardCount], [width - 0.12, 0.025, depth / boardCount - 0.025]);
    part(room, "wall", [0, 1.7, -depth / 2], [width, 3.4, 0.2]);
    part(room, "wall", [-width / 2, 1.7, 0], [0.2, 3.4, depth]);
    part(room, "trim", [0, 0.2, -depth / 2 + 0.12], [width, 0.3, 0.09]);
    part(room, "trim", [-width / 2 + 0.12, 0.2, 0], [0.09, 0.3, depth]);
    part(room, "timber", [0, 3.45, -depth / 2], [width + 0.2, 0.15, 0.3]);
    part(room, "timber", [-width / 2, 3.45, 0], [0.3, 0.15, depth]);
    part(room, "trim", [width / 2, 0.16, 0], [0.12, 0.25, depth]);
    for (const side of [-1, 1]) part(room, "trim", [side * (width / 4 + 0.6), 0.16, depth / 2], [width / 2 - 1.2, 0.25, 0.12]);
    for (let x = -width / 2 + 2.2; x < width / 2 - 0.9; x += 4.0) {
      part(room, "timber", [x, 2.03, -depth / 2 + 0.13], [1.55, 1.47, 0.14]);
      part(room, "window", [x, 2.03, -depth / 2 + 0.21], [1.35, 1.28, 0.03]);
      part(room, "cream", [x, 2.03, -depth / 2 + 0.24], [0.065, 1.28, 0.05]);
      part(room, "cream", [x, 2.03, -depth / 2 + 0.24], [1.35, 0.065, 0.05]);
      part(room, "cream", [x, 1.26, -depth / 2 + 0.24], [1.8, 0.13, 0.35]);
    }
    // Built-in bookshelves occupy the back-left corner, outside the work aisles.
    part(room, "timber", [-width / 2 + 0.42, 1.1, -depth / 2 + 1.8], [0.65, 2.2, 2.2]);
    for (let row = 0; row < 3; row++) {
      part(room, "trim", [-width / 2 + 0.82, 0.45 + row * 0.65, -depth / 2 + 1.8], [0.18, 0.12, 2.1]);
      for (let book = 0; book < 6; book++) part(room, ["teal", "paper", "rust", "navy"][(book + row) % 4], [-width / 2 + 0.8, 0.7 + row * 0.65, -depth / 2 + 0.99 + book * 0.3], [0.2, 0.38 + book % 2 * 0.12, 0.21]);
    }
    plant(room, width / 2 - 0.8, -depth / 2 + 0.85, 1.2);
    plant(room, -width / 2 + 0.9, depth / 2 - 0.85);
    part(room, workshop ? "navy" : "rust", [0, 0.07, 0.3], [width - 2.8, 0.035, depth - 3.0]);
    part(room, workshop ? "teal" : "cream", [0, 0.093, 0.3], [width - 3.15, 0.015, depth - 3.35]);
    label(building.name || "Inside the village", [0, 3.7, -depth / 2], true);
    // Empty rooms remain furnished, without inventing occupants or activity.
    const stations = layout.stations.length ? layout.stations : [{ id: null, slot: 0, position: [0, 0], agent: null }];
    for (const station of stations) {
      const [x, z] = station.position;
      const occupant = station.agent;
      const furniture = new THREE.Group();
      if (occupant) furniture.userData.selection = { kind: "agent", id: occupant.id };
      const seed = [...(station.id || "")].reduce((value, char) => (Math.imul(value, 31) + char.charCodeAt(0)) >>> 0, 0);
      const accent = ["#668f87", "#b393be", "#c6a34f", "#ae696d"][seed % 4];
      if (sleeping) bed(furniture, x, z, accent);
      else {
        desk(furniture, x, z);
        part(furniture, accent, [x, 0.93, z + 0.06], [0.72, 0.025, 0.25]);
      }
      // The personal nameplate belongs to the pickable desk/bed, not the person mesh.
      part(furniture, accent, [x, 0.97, z + (sleeping ? -1.12 : 0.93)], [0.8, 0.16, 0.055]);
      objects.push(furniture);
      if (occupant) knownNames.set(occupant.id, occupant.name || occupant.id);
      const name = knownNames.get(station.id) || station.id;
      const stateLabel = occupant?.pendingApproval ? "awaiting approval" : occupant?.state;
      if (station.id) label(`${sleeping ? "Bed" : "Desk"} ${station.slot + 1} · ${name} · ${occupant ? stateLabel : "away"}`, [x, 1.75, z], false,
        occupant ? name : `${sleeping ? "Bed" : "Desk"} ${station.slot + 1} · away`, Boolean(occupant));
      if (!occupant) continue;
      const person = art.agent(occupant);
      const standingX = sleeping ? x - 0.68 : x;
      const standingZ = sleeping ? z + 0.8 : z - 0.62;
      person.position.set(standingX, sleeping ? 0.09 : 0.18, standingZ);
      person.rotation.y = sleeping ? Math.PI / 4 : 0;
      person.scale.setScalar(1.1);
      person.userData.selection = { kind: "agent", id: occupant.id };
      objects.push(person);
      const status = occupant.pendingApproval ? "#e4a151" : ({ working: "#578b61", resting: "#8fbaa4", stale: "#929690", failed: "#c16449", knocking: "#e4a151" }[occupant.state] || "#929690");
      part(furniture, status, [x + 0.48, 1.08, z + (sleeping ? -1.1 : 0.92)], [0.16, 0.16, 0.16], "sphere");
    }
    if (sleeping && layout.stations.length < 3) {
      part(room, "timber", [width / 2 - 2, 0.6, depth / 2 - 2], [1.55, 0.14, 1.0]);
      part(room, "trim", [width / 2 - 2, 0.3, depth / 2 - 2], [0.6, 0.6, 0.6]);
      lamp(room, width / 2 - 1.7, depth / 2 - 2, 0.69);
      part(room, "paper", [width / 2 - 2.3, 0.69, depth / 2 - 2], [0.45, 0.04, 0.35]);
    }
    batch(objects);
    const shadow = Math.max(width, depth) / 2 + 2;
    Object.assign(sun.shadow.camera, { left: -shadow, right: shadow, top: shadow, bottom: -shadow, far: Math.max(100, shadow * 5) });
    sun.shadow.camera.updateProjectionMatrix();
    sun.position.set(width * 0.4, Math.max(width, depth), depth * 0.5);
    needsRender = true;
  }
  function fit(reset = false, preserveSpan = false) {
    const width = Math.max(1, host.clientWidth), height = Math.max(1, host.clientHeight);
    renderer.setSize(width, height, false);
    const aspect = width / height;
    const span = preserveSpan ? camera.top - camera.bottom : Math.max((extent.width + extent.depth) * 0.52 + 4, (extent.width + extent.depth) * 0.74 / aspect) * 1.13;
    camera.left = -span * aspect / 2; camera.right = span * aspect / 2;
    camera.top = span / 2; camera.bottom = -span / 2;
    if (reset) {
      camera.zoom = 1;
      const distance = Math.max(extent.width, extent.depth);
      camera.position.set(distance * 0.8, distance * 0.95, distance * 1.1);
      controls.target.set(0, 0.7, 0);
    }
    camera.updateProjectionMatrix(); controls.update(); needsRender = true;
  }
  function update(next) {
    if (disposed || !next.building) return;
    state = next;
    const occupants = next.agents || [];
    const room = next.room || fallbackLayout.update(next.building.id, occupants);
    const nextSignature = JSON.stringify([next.building.id, next.building.kind, next.building.name, room.stations.map(s => [s.id, s.slot]), occupants.map(a => [a.id, a.name, a.appearance, a.state, a.pendingApproval])]);
    const changedBuilding = buildingId !== next.building.id;
    if (signature !== nextSignature) { signature = nextSignature; rebuild(next.building, occupants, room); fit(changedBuilding, !changedBuilding); }
    buildingId = next.building.id;
    const focusAgentId = next.focusAgentId || null;
    if (changedBuilding) lastFocusAgentId = null;
    if (focusAgentId && (changedBuilding || focusAgentId !== lastFocusAgentId)) {
      const station = room.stations.find(item => item.id === focusAgentId && item.agent);
      if (station) {
        const offset = camera.position.clone().sub(controls.target);
        controls.target.set(station.position[0], .7, station.position[1]);
        camera.position.copy(controls.target).add(offset);
        camera.zoom = THREE.MathUtils.clamp((camera.top - camera.bottom) / 12, .5, 5);
        camera.updateProjectionMatrix(); controls.update();
        lastFocusAgentId = focusAgentId;
      }
    }
    if (!focusAgentId) lastFocusAgentId = null;
    canvas.dataset.focusAgent = focusAgentId && room.stations.some(item => item.id === focusAgentId && item.agent) ? focusAgentId : "";
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, next.quality === "low" ? 1 : 1.75));
    renderer.shadowMap.enabled = next.quality !== "low";
    // Furniture and occupant poses stay still; motion preference also disables inertia.
    controls.enableDamping = !next.paused;
    if (next.cameraCommand && next.cameraCommand !== lastCommand) {
      lastCommand = next.cameraCommand;
      if (lastCommand.type === "reset") fit(true);
      if (lastCommand.type === "zoom-in" || lastCommand.type === "zoom-out") {
        camera.zoom = THREE.MathUtils.clamp(camera.zoom * (lastCommand.type === "zoom-in" ? 1.3 : 1 / 1.3), 0.5, 5);
        camera.updateProjectionMatrix();
      }
    }
    canvas.dataset.agents = String(occupants.length);
    canvas.dataset.stations = JSON.stringify(room.stations.map(({ id, slot, position, agent }) => ({ id, slot, position, present: Boolean(agent) })));
    canvas.dataset.building = next.building.id;
    canvas.dataset.paused = String(Boolean(next.paused));
    canvas.dataset.ready = "true";
    needsRender = true;
  }
  const changed = () => { needsRender = true; };
  controls.addEventListener("change", changed);
  const observer = new ResizeObserver(() => fit()); observer.observe(host);
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let pointerStart = null;
  const down = event => { pointerStart = [event.clientX, event.clientY]; };
  const up = event => {
    if (!pointerStart || Math.hypot(event.clientX - pointerStart[0], event.clientY - pointerStart[1]) > 5) { pointerStart = null; return; }
    pointerStart = null;
    const rect = canvas.getBoundingClientRect();
    pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    for (const hit of raycaster.intersectObject(root, true)) {
      const selection = hit.object.userData.selections?.[hit.instanceId];
      if (selection) { onSelect?.(selection); return; }
    }
  };
  const cancel = () => { pointerStart = null; };
  const lost = event => { event.preventDefault(); cancelAnimationFrame(frame); onError?.(new Error("The room lost its graphics connection. Everyone is still available in the occupant list.")); };
  canvas.addEventListener("pointerdown", down); canvas.addEventListener("pointerup", up);
  canvas.addEventListener("pointercancel", cancel); canvas.addEventListener("pointerleave", cancel);
  canvas.addEventListener("webglcontextlost", lost);
  function animate() {
    if (disposed) return;
    frame = requestAnimationFrame(animate);
    if (document.hidden) return;
    try {
      controls.update();
      if (!needsRender) return;
      const overview = camera.zoom < 1.5;
      const crowded = names.filter(name => !name.always).length > 20;
      const occupiedLabels = [];
      // Present people take precedence over reserved places when labels compete.
      for (const name of [...names].sort((a, b) => Number(b.always) - Number(a.always) || Number(b.present) - Number(a.present))) {
        name.element.textContent = overview ? name.compact : name.text;
        projected.copy(name.position).project(camera);
        const x = (projected.x * 0.5 + 0.5) * host.clientWidth;
        const y = (-projected.y * 0.5 + 0.5) * host.clientHeight;
        name.element.hidden = (!name.always && !name.present && crowded && overview) || projected.z < -1 || projected.z > 1 || x < 0 || x > host.clientWidth || y < 0 || y > host.clientHeight;
        name.element.style.left = `${x}px`; name.element.style.top = `${y}px`;
        if (!name.element.hidden) {
          const width = name.element.offsetWidth, height = name.element.offsetHeight;
          const box = { left: x - width / 2 - 3, right: x + width / 2 + 3, top: y - height - 3, bottom: y + 3 };
          if (!name.always && occupiedLabels.some(other => box.left < other.right && box.right > other.left && box.top < other.bottom && box.bottom > other.top)) name.element.hidden = true;
          else occupiedLabels.push(box);
        }
      }
      renderer.render(scene, camera);
      canvas.dataset.drawCalls = String(renderer.info.render.calls);
      needsRender = false;
    } catch (error) { cancelAnimationFrame(frame); onError?.(error); }
  }
  frame = requestAnimationFrame(animate);
  return {
    update,
    dispose() {
      if (disposed) return;
      disposed = true; cancelAnimationFrame(frame); observer.disconnect();
      controls.removeEventListener("change", changed); controls.dispose();
      canvas.removeEventListener("pointerdown", down); canvas.removeEventListener("pointerup", up);
      canvas.removeEventListener("pointercancel", cancel); canvas.removeEventListener("pointerleave", cancel);
      canvas.removeEventListener("webglcontextlost", lost);
      root.children.forEach(mesh => mesh.dispose()); root.clear();
      sun.shadow.dispose(); art.dispose();
      geometries.forEach(value => value.dispose()); materials.forEach(value => value.dispose()); batchMaterials.forEach(value => value.dispose());
      renderer.dispose(); renderer.forceContextLoss(); canvas.remove(); labels.remove();
    },
  };
}
