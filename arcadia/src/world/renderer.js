import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import { createArtKit } from "./art.js";
import { createMotion, retargetMotion, advanceMotion, isOutside } from "./motion.js";
import { buildingOccupancy } from "./occupancy.js";
import { daylightAt } from "./daylight.js";
import { readCameraPreferences, saveCameraPreferences } from "./viewPreferences.js";

const keyOf = selection => selection ? `${selection.kind}:${selection.id}` : "";

// One scene owns rendering and motion; React supplies complete presentation state.
export function createVillageRenderer(host, { onSelect, onError }) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "default" });
  renderer.setClearColor(0xece9dd, 1);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  const canvas = renderer.domElement;
  canvas.dataset.renderer = "three";
  canvas.dataset.ready = "false";
  host.appendChild(canvas);
  const labels = document.createElement("div");
  labels.className = "world-labels";
  host.appendChild(labels);
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-30, 30, 30, -30, .1, 600);
  camera.position.set(35, 38, 42);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = .09;
  controls.minPolarAngle = .3;
  controls.maxPolarAngle = Math.PI / 2.5;
  controls.minZoom = .25;
  controls.maxZoom = 5;
  controls.enablePan = true;
  controls.screenSpacePanning = false;
  controls.mouseButtons = { LEFT: THREE.MOUSE.PAN, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.ROTATE };
  controls.touches = { ONE: THREE.TOUCH.PAN, TWO: THREE.TOUCH.DOLLY_ROTATE };
  const ambient = new THREE.HemisphereLight(0xfff5dd, 0x6d805e, 2.5);
  const sun = new THREE.DirectionalLight(0xffe4b3, 3);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  sun.shadow.normalBias = .06;
  sun.shadow.bias = -.0002;
  scene.add(ambient, sun, sun.target);
  const art = createArtKit();
  const agents = new Map();
  const buildingLabels = new Map();
  let occupancy = new Map();
  const staticRoot = new THREE.Group();
  scene.add(staticRoot);
  const dynamicRoot = new THREE.Group();
  scene.add(dynamicRoot);
  const dynamicBatches = new Map();
  const dynamicMaterials = new Map();
  let state = { world: null, selection: null, paused: false, quality: "high", follow: false };
  let disposed = false, frame = 0, lastTime = 0, lastLighting = 0;
  let staticSignature = "", lastCommand = null, lastSelection = "", hover = null;
  let targetCamera = null, worldCenter = new THREE.Vector3(), worldSpan = 40;
  let fittedExtent = { width: 40, height: 40 };
  let first = true, frameCount = 0, frameSampleAt = performance.now(), colorsDirty = true;
  let saveTimer = null, savePending = false;
  const cameraPose = () => ({ position: camera.position.toArray(), target: controls.target.toArray(), zoom: camera.zoom });
  function publishCamera() { canvas.dataset.camera = JSON.stringify(cameraPose()); }
  function saveCamera() {
    saveTimer = null;
    if (disposed || first || host.clientWidth < 2 || host.clientHeight < 2) return;
    saveCameraPreferences(cameraPose()); savePending = false;
  }
  function queueCameraSave() {
    if (first || host.clientWidth < 2 || host.clientHeight < 2) return;
    savePending = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveCamera, 350);
  }
  function cameraChanged() {
    publishCamera();
    if (savePending) queueCameraSave();
  }
  controls.addEventListener("change", cameraChanged);
  controls.addEventListener("end", queueCameraSave);
  const projected = new THREE.Vector3();
  const hiddenInstance = new THREE.Matrix4().makeScale(0, 0, 0);
  const ownResources = new Set();
  const material = (color, extra = {}) => {
    const m = new THREE.MeshStandardMaterial({ color, roughness: 1, ...extra });
    ownResources.add(m); return m;
  };
  const grass = material(0x8da675), soil = material(0xbda080), path = material(0xcebc9a);
  const water = material(0x79b2ba, { roughness: .3, metalness: .15 });
  const pebble = material(0xbcbba0);
  const windows = [0x65827a, 0xffd591, 0x42564e].map(color => {
    const m = new THREE.MeshBasicMaterial({ color }); ownResources.add(m); return m;
  });
  const box = new THREE.BoxGeometry(1, 1, 1); ownResources.add(box);
  const pebbleGeometry = new THREE.DodecahedronGeometry(.22, 0); ownResources.add(pebbleGeometry);
  const poolGeometry = new THREE.CircleGeometry(1, 40); ownResources.add(poolGeometry);
  const dotGeometry = new THREE.SphereGeometry(.075, 8, 6); ownResources.add(dotGeometry);
  const statusColors = { working:0x578b61, resting:0x8fbaa4, stale:0x929690, failed:0xc16449, knocking:0xe4a151 };
  const dotMaterial = material(0xffffff);
  const ringGeometry = new THREE.RingGeometry(.66, .77, 40); ownResources.add(ringGeometry);
  const ringMaterial = new THREE.MeshBasicMaterial({ color: 0xf8d28e, side: THREE.DoubleSide }); ownResources.add(ringMaterial);
  const ring = new THREE.Mesh(ringGeometry, ringMaterial);
  ring.rotation.x = -Math.PI / 2; ring.visible = false; scene.add(ring);
  let groundGeometry = null;
  const label = (name, selected = false) => {
    const element = document.createElement("div");
    element.className = `world-label${selected ? " selected" : ""}`;
    element.textContent = name; labels.appendChild(element); return element;
  };
  const selectedLabel = label("", true); selectedLabel.hidden = true;
  let lighting = daylightAt();

  function buildingLabel(model) {
    return ["home", "lodge", "workshop", "square"].includes(model.kind)
      ? `${model.name} · ${occupancy.get(model.id).summary}` : model.name;
  }

  function batch(groups) {
    const batches = new Map();
    for (const group of groups) {
      group.updateMatrixWorld(true);
      group.traverse(object => {
        if (!object.isMesh) return;
        const materials = Array.isArray(object.material) ? object.material : [object.material];
        const batchKey = `${object.geometry.uuid}:${materials.map(m => m.uuid).join(":")}`;
        if (!batches.has(batchKey)) batches.set(batchKey, { geometry: object.geometry, material: object.material, items: [] });
        batches.get(batchKey).items.push({ matrix: object.matrixWorld.clone(), selection: group.userData.selection || null, shadow: object.castShadow });
      });
    }
    for (const { geometry, material: mat, items } of batches.values()) {
      const mesh = new THREE.InstancedMesh(geometry, mat, items.length);
      mesh.userData.selections = items.map(item => item.selection);
      items.forEach((item, i) => mesh.setMatrixAt(i, item.matrix));
      mesh.castShadow = items.some(item => item.shadow);
      mesh.receiveShadow = true;
      mesh.computeBoundingSphere();
      staticRoot.add(mesh);
    }
  }

  function rebuildLandscape(world) {
    staticRoot.children.forEach(mesh => mesh.dispose?.());
    staticRoot.clear();
    groundGeometry?.dispose();
    const { minX, maxX, minZ, maxZ } = world.bounds;
    const width = maxX - minX + 15, depth = maxZ - minZ + 15;
    worldCenter.set((minX + maxX) / 2, 0, (minZ + maxZ) / 2);
    worldSpan = Math.max(width, depth);
    const forward = new THREE.Vector3(.75, .85, .95).normalize();
    const right = new THREE.Vector3().crossVectors(forward, new THREE.Vector3(0, 1, 0)).normalize();
    const up = new THREE.Vector3().crossVectors(right, forward).normalize();
    fittedExtent = {
      width: Math.abs(right.x) * width + Math.abs(right.z) * depth,
      height: Math.abs(up.x) * width + Math.abs(up.z) * depth + 5 * Math.abs(up.y),
    };
    groundGeometry = new RoundedBoxGeometry(width, 2, depth, 3, 1);
    const island = new THREE.Mesh(groundGeometry, [soil, soil, grass, soil, soil, soil]);
    island.position.copy(worldCenter).y = -1;
    island.receiveShadow = true;
    staticRoot.add(island);
    const objects = [];
    for (const road of world.roads) {
      const dx = road.to[0] - road.from[0], dz = road.to[1] - road.from[1];
      const roadMesh = new THREE.Mesh(box, path);
      roadMesh.scale.set(road.width || 1.2, .05, Math.hypot(dx, dz) + .2);
      roadMesh.position.set((road.from[0] + road.to[0]) / 2, .015, (road.from[1] + road.to[1]) / 2);
      roadMesh.rotation.y = Math.atan2(dx, dz);
      const group = new THREE.Group(); group.add(roadMesh); objects.push(group);
    }
    for (const model of world.buildings) {
      const building = art.building(model);
      building.position.set(model.position[0], .06, model.position[1]);
      building.userData.selection = { kind: "building", id: model.id };
      const occupied = world.agents.some(agent => agent.buildingId === model.id && ["resting", "working"].includes(agent.state));
      building.traverse(mesh => { if (mesh.userData.window) mesh.material = windows[lighting.night ? (occupied ? 1 : 2) : 0]; });
      objects.push(building);
      if (!buildingLabels.has(model.id)) buildingLabels.set(model.id, label(model.name));
      buildingLabels.get(model.id).textContent = buildingLabel(model);
    }
    for (const [id, el] of buildingLabels) {
      if (!world.buildings.some(b => b.id === id)) { el.remove(); buildingLabels.delete(id); }
    }
    // A planted edge frames each new neighborhood while keeping navigation clear.
    const random = n => { const value = Math.sin(n * 127.1 + 31.7) * 43758.5453; return value - Math.floor(value); };
    const perimeter = Math.min(100, Math.round((width + depth) * .7));
    for (let i = 0; i < perimeter; i++) {
      const side = i % 4, along = random(i + 1);
      const x = side < 2 ? minX - 4 + along * (width - 7) : (side === 2 ? minX - 4 : maxX + 4);
      const z = side >= 2 ? minZ - 4 + along * (depth - 7) : (side === 0 ? minZ - 4 : maxZ + 4);
      const tree = art.tree(i);
      tree.position.set(x, .02, z);
      tree.scale.setScalar(.65 + random(i + 10) * .7);
      tree.rotation.y = random(i + 4) * Math.PI * 2;
      objects.push(tree);
    }
    // Small pools and stones sit at the landscape edge, away from streets and plots.
    const pool = new THREE.Mesh(poolGeometry, water);
    pool.rotation.x = -Math.PI / 2;
    pool.scale.set(2.8, 1.6, 1);
    pool.position.set(minX - 3, .02, maxZ + 3);
    staticRoot.add(pool);
    for (let i = 0; i < 14; i++) {
      const stone = new THREE.Mesh(pebbleGeometry, pebble);
      const angle = i / 14 * Math.PI * 2;
      stone.position.set(minX - 3 + Math.cos(angle) * 2.9, .12, maxZ + 3 + Math.sin(angle) * 1.8);
      stone.scale.setScalar(.7 + random(i) * .8);
      const group = new THREE.Group(); group.add(stone); objects.push(group);
    }
    // Unoccupied plots become pocket gardens; new homes replace their own garden only.
    for (let x = minX + 8; x <= maxX - 8; x += 10) {
      for (let z = minZ + 8; z <= maxZ - 8; z += 10) {
        if (world.buildings.some(b => Math.abs(b.position[0] - x) < 1 && Math.abs(b.position[1] - z) < 1)) continue;
        const seed = Math.abs(x * 13 + z * 7);
        for (let i = 0; i < 3; i++) {
          const tree = art.tree(seed + i);
          tree.position.set(x + (i - 1) * 1.5, .02, z + (i % 2 ? 1.4 : -.7));
          tree.scale.setScalar(.8 + random(seed + i) * .3); objects.push(tree);
        }
        const bench = new THREE.Mesh(box, soil);
        bench.scale.set(1.4, .25, .45); bench.position.set(x, .2, z + 2.5);
        const group = new THREE.Group(); group.add(bench); objects.push(group);
      }
    }
    for (const building of world.buildings.filter(b => ["home", "lodge", "archive"].includes(b.kind))) {
      for (const side of [-1, 1]) {
        const tree = art.tree(building.position[0] + building.position[1] + side);
        tree.position.set(building.position[0] + side * 3.3, .02, building.position[1] - 2.7);
        tree.scale.setScalar(.72); objects.push(tree);
      }
    }
    batch(objects);
    const shadowRange = Math.max(width, depth) / 2 + 6;
    Object.assign(sun.shadow.camera, { left: -shadowRange, right: shadowRange, top: shadowRange, bottom: -shadowRange, far: 160 });
    sun.shadow.camera.updateProjectionMatrix();
    sun.target.position.copy(worldCenter);
    applyLighting();
    if (first) {
      resetCamera(true);
      const saved = readCameraPreferences(world.bounds);
      if (saved) {
        camera.position.fromArray(saved.position); controls.target.fromArray(saved.target);
        camera.zoom = saved.zoom; camera.updateProjectionMatrix(); controls.update();
      }
      first = false; publishCamera();
    }
  }

  function applyLighting() {
    ambient.intensity = .85 + lighting.daylight * .85;
    sun.intensity = .4 + lighting.daylight * 2.6;
    sun.color.set(lighting.daylight < .85 ? 0xffc499 : 0xffeed1);
    sun.position.set(worldCenter.x + lighting.sun[0], lighting.sun[1], worldCenter.z + lighting.sun[2]);
    renderer.setClearColor(new THREE.Color(0x424e62).lerp(new THREE.Color(0xece9dd), lighting.daylight), 1);
    canvas.dataset.phase = lighting.phase;
  }

  function resetCamera(immediate = false) {
    const aspect = host.clientWidth / Math.max(1, host.clientHeight);
    const span = Math.max(fittedExtent.height, fittedExtent.width / aspect) * 1.08;
    camera.left = -span * aspect / 2; camera.right = span * aspect / 2;
    camera.top = span / 2; camera.bottom = -span / 2;
    camera.zoom = 1; camera.updateProjectionMatrix();
    const offset = new THREE.Vector3(worldSpan * .75, worldSpan * .85, worldSpan * .95);
    if (immediate) {
      camera.position.copy(worldCenter).add(offset);
      controls.target.copy(worldCenter); controls.update();
    } else targetCamera = { target: worldCenter.clone(), position: worldCenter.clone().add(offset) };
  }

  function selectionPosition() {
    const sel = state.selection;
    if (!sel || !state.world) return null;
    if (sel.kind === "agent") return agents.get(sel.id)?.group.position;
    const model = state.world.buildings.find(building => building.id === sel.id);
    return model ? new THREE.Vector3(model.position[0], 0, model.position[1]) : null;
  }

  function focusSelection() {
    const position = selectionPosition();
    if (!position) return;
    camera.zoom = Math.min(5, Math.max(camera.zoom, (camera.top - camera.bottom) / 24));
    camera.updateProjectionMatrix();
    const offset = camera.position.clone().sub(controls.target);
    targetCamera = { target: position.clone(), position: position.clone().add(offset) };
  }

  function rebuildAgentBatches() {
    dynamicRoot.children.forEach(mesh => mesh.dispose());
    dynamicRoot.clear(); dynamicBatches.clear();
    for (const [id, entry] of agents) {
      entry.group.updateMatrixWorld(true);
      entry.group.traverse(object => {
        if (!object.isMesh) return;
        const source = object.material;
        const key = `${object.geometry.uuid}:${source.type}:${source.roughness}:${source.metalness}`;
        if (!dynamicMaterials.has(key)) {
          const mat = source.clone(); mat.color.set(0xffffff); mat.emissive?.set(0x000000);
          dynamicMaterials.set(key, mat); ownResources.add(mat);
        }
        if (!dynamicBatches.has(key)) dynamicBatches.set(key, { geometry: object.geometry, material: dynamicMaterials.get(key), items: [] });
        dynamicBatches.get(key).items.push({ id, object, color: source.color.clone(), entry });
      });
    }
    for (const batch of dynamicBatches.values()) {
      batch.mesh = new THREE.InstancedMesh(batch.geometry, batch.material, batch.items.length);
      batch.mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      batch.mesh.userData.selections = batch.items.map(item => ({ kind: "agent", id: item.id }));
      batch.mesh.castShadow = true; batch.mesh.receiveShadow = true;
      // Moving instances may leave their previous bounding volume between snapshots.
      batch.mesh.frustumCulled = false;
      dynamicRoot.add(batch.mesh);
    }
  }

  function updateAgentBatches() {
    for (const entry of agents.values()) entry.group.updateMatrixWorld(true);
    for (const batch of dynamicBatches.values()) {
      batch.items.forEach((item, index) => {
        batch.mesh.setMatrixAt(index, isOutside(item.entry.model, item.entry.motion) ? item.object.matrixWorld : hiddenInstance);
        if (colorsDirty) {
          const color = item.object.userData.status ? new THREE.Color(statusColors[item.entry.model.state] || statusColors.resting) : item.color.clone();
          if (item.entry.model.state === "stale") color.lerp(new THREE.Color(0xbac0ae), .55);
          batch.mesh.setColorAt(index, color);
        }
      });
      batch.mesh.instanceMatrix.needsUpdate = true;
      if (colorsDirty && batch.mesh.instanceColor) batch.mesh.instanceColor.needsUpdate = true;
      batch.mesh.computeBoundingSphere();
    }
    colorsDirty = false;
  }

  function update(next) {
    if (disposed) return;
    const previous = state;
    state = next;
    colorsDirty = true;
    if (!next.world) return;
    const { world } = next;
    occupancy = new Map(world.buildings.map(building => [building.id, buildingOccupancy(world, building)]));
    const signature = JSON.stringify([world.buildings.map(({id,kind,name,position,width,depth}) => ({id,kind,name,position,width,depth})), world.roads, world.bounds, world.agents.filter(a => ["resting", "working"].includes(a.state)).map(a => a.buildingId), lighting.night]);
    if (signature !== staticSignature) { staticSignature = signature; rebuildLandscape(world); }
    let rosterChanged = false;
    for (const [id] of agents) {
      if (world.agents.some(agent => agent.id === id)) continue;
      agents.delete(id); rosterChanged = true;
    }
    for (const model of world.agents) {
      let entry = agents.get(model.id);
      if (!entry) {
        entry = { model, motion: createMotion(model.destination) };
        agents.set(model.id, entry);
      }
      if (!entry.group || JSON.stringify(entry.model.appearance) !== JSON.stringify(model.appearance)) {
        entry.group = art.agent(model);
        entry.group.userData.selection = { kind: "agent", id: model.id };
        const dot = new THREE.Mesh(dotGeometry, dotMaterial);
        dot.position.set(.3, 1.18, 0); dot.userData.status = true;
        entry.group.add(dot); rosterChanged = true;
      }
      // Hidden exterior views reconcile immediately instead of replaying old travel on return.
      retargetMotion(entry.motion, model, next.paused || !host.clientWidth || !host.clientHeight);
      entry.group.position.set(entry.motion.position[0], .07, entry.motion.position[1]);
      entry.model = model;
    }
    if (rosterChanged) rebuildAgentBatches();
    updateAgentBatches();
    if (previous.quality !== next.quality) {
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, next.quality === "low" ? 1 : 1.75));
      renderer.shadowMap.enabled = next.quality !== "low";
      resize();
    }
    const selectionKey = keyOf(next.selection);
    if (selectionKey !== lastSelection) { lastSelection = selectionKey; focusSelection(); }
    if (next.cameraCommand && next.cameraCommand !== lastCommand) {
      lastCommand = next.cameraCommand;
      const type = lastCommand.type;
      if (type === "reset") resetCamera();
      if (type === "focus") focusSelection();
      if (type === "zoom-in" || type === "zoom-out") {
        camera.zoom = THREE.MathUtils.clamp(camera.zoom * (type === "zoom-in" ? 1.3 : 1 / 1.3), .25, 5);
        camera.updateProjectionMatrix();
        publishCamera(); queueCameraSave();
      }
      if (type === "rotate-left" || type === "rotate-right") {
        const offset = camera.position.clone().sub(controls.target).applyAxisAngle(new THREE.Vector3(0, 1, 0), type === "rotate-left" ? Math.PI / 4 : -Math.PI / 4);
        targetCamera = { target: controls.target.clone(), position: controls.target.clone().add(offset) };
      }
    }
    canvas.dataset.agents = String(agents.size);
    canvas.dataset.buildings = String(world.buildings.length);
    canvas.dataset.paused = String(Boolean(next.paused));
    canvas.dataset.ready = "true";
  }

  function resize() {
    if (disposed) return;
    const width = Math.max(1, host.clientWidth), height = Math.max(1, host.clientHeight);
    renderer.setSize(width, height, false);
    const half = Math.max(fittedExtent.height, fittedExtent.width / (width / height)) * .54;
    camera.top = half; camera.bottom = -half;
    camera.left = -half * width / height; camera.right = half * width / height;
    camera.updateProjectionMatrix();
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
  const observer = new ResizeObserver(resize); observer.observe(host); resize();
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let pointerStart = null, lastHoverAt = 0;
  function pick(event) {
    const rect = canvas.getBoundingClientRect();
    pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    for (const hit of raycaster.intersectObjects([staticRoot, dynamicRoot], true)) {
      if (hit.object.isInstancedMesh) {
        const selection = hit.object.userData.selections?.[hit.instanceId];
        if (selection && (selection.kind !== "agent" || isOutside(agents.get(selection.id).model, agents.get(selection.id).motion))) return selection;
      }
      for (let parent = hit.object; parent; parent = parent.parent) if (parent.userData.selection) return parent.userData.selection;
    }
    return null;
  }
  const down = event => { pointerStart = { x: event.clientX, y: event.clientY }; targetCamera = null; };
  const up = event => {
    if (pointerStart && Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) < 5) onSelect?.(pick(event));
    pointerStart = null;
  };
  const move = event => {
    if (pointerStart || performance.now() - lastHoverAt < 100) return;
    lastHoverAt = performance.now(); hover = pick(event); canvas.style.cursor = hover ? "pointer" : "grab";
  };
  const leave = () => { hover = null; pointerStart = null; };
  const contextLost = event => { event.preventDefault(); onError?.(new Error("The 3D view lost its graphics connection. Your village list is still available.")); };
  canvas.addEventListener("pointerdown", down); canvas.addEventListener("pointerup", up);
  canvas.addEventListener("pointermove", move); canvas.addEventListener("pointerleave", leave);
  canvas.addEventListener("webglcontextlost", contextLost);

  function placeLabel(element, point, elevation = 0) {
    projected.set(point.x, point.y + elevation, point.z).project(camera);
    const x = (projected.x * .5 + .5) * host.clientWidth;
    const y = (-projected.y * .5 + .5) * host.clientHeight;
    element.hidden = projected.z < -1 || projected.z > 1 || x < 0 || x > host.clientWidth || y < 0 || y > host.clientHeight;
    element.style.left = `${x}px`; element.style.top = `${y}px`;
  }
  function animate(time) {
    if (disposed) return;
    frame = requestAnimationFrame(animate);
    const elapsed = time - lastTime;
    if (document.hidden || !host.clientWidth || !host.clientHeight || elapsed < (state.quality === "low" ? 32 : 15)) return;
    const dt = Math.min(.08, elapsed / 1000); lastTime = time;
    try {
      if (time - lastLighting > 60000) {
        const wasNight = lighting.night; lighting = daylightAt(); lastLighting = time; applyLighting();
        if (lighting.night !== wasNight && state.world) { staticSignature = ""; update(state); }
      }
      for (const entry of agents.values()) {
        const { group, model } = entry;
        let walking = false;
        if (!state.paused) {
          const movement = advanceMotion(entry.motion, dt);
          walking = movement.walking;
          group.position.x = entry.motion.position[0];
          group.position.z = entry.motion.position[1];
          if (walking) group.rotation.y = movement.heading;
        }
        if (!state.paused) {
          group.position.y = .07 + (walking ? Math.abs(Math.sin(time * .012)) * .07 : model.state === "working" ? Math.sin(time * .004) * .018 : 0);
          const limbs = [...(group.userData.legs || []), ...(group.userData.arms || [])];
          limbs.forEach((limb, i) => { limb.rotation.x = walking ? Math.sin(time * .012 + i * Math.PI) * .4 : 0; });
        }
      }
      if (targetCamera) {
        camera.position.lerp(targetCamera.position, 1 - Math.exp(-dt * 6));
        controls.target.lerp(targetCamera.target, 1 - Math.exp(-dt * 6));
        if (camera.position.distanceTo(targetCamera.position) < .03) {
          camera.position.copy(targetCamera.position); controls.target.copy(targetCamera.target);
          targetCamera = null; queueCameraSave();
        }
      } else if (state.follow && state.selection?.kind === "agent" && !pointerStart) {
        const position = selectionPosition();
        if (position) {
          const delta = position.clone().sub(controls.target).multiplyScalar(1 - Math.exp(-dt * 4));
          controls.target.add(delta); camera.position.add(delta);
        }
      }
      controls.update();
      const selPosition = selectionPosition();
      ring.visible = Boolean(selPosition) && (state.selection?.kind !== "agent" || isOutside(agents.get(state.selection.id).model, agents.get(state.selection.id).motion));
      if (selPosition) { ring.position.copy(selPosition).y = .1; ring.scale.setScalar(state.selection.kind === "agent" ? 1 : 3); }
      const visibleLabels = camera.zoom > 1.4;
      const compactOverview = host.clientWidth < 600 && !visibleLabels;
      for (const model of state.world?.buildings || []) {
        const el = buildingLabels.get(model.id);
        if (!el) continue;
        const text = buildingLabel(model);
        if (el.textContent !== text) el.textContent = text;
        if ((compactOverview && model.kind !== "square") || (!visibleLabels && !["square", "lodge", "archive", "noticeboard", "workshop"].includes(model.kind)) || (state.selection?.kind === "building" && state.selection.id === model.id)) { el.hidden = true; continue; }
        placeLabel(el, new THREE.Vector3(model.position[0], 0, model.position[1]), model.kind === "square" ? 1 : 3.2);
      }
      const shown = hover || state.selection;
      selectedLabel.hidden = !shown;
      if (shown?.kind === "agent") {
        const entry = agents.get(shown.id);
        if (entry && isOutside(entry.model, entry.motion)) { selectedLabel.textContent = `${entry.model.name} · ${entry.model.state}`; placeLabel(selectedLabel, entry.group.position, 1.65); }
        else selectedLabel.hidden = true;
      } else if (shown?.kind === "building") {
        const model = state.world?.buildings.find(b => b.id === shown.id);
        if (model) { selectedLabel.textContent = `${model.name} · ${occupancy.get(model.id).summary}${occupancy.get(model.id).preview ? "\n" + occupancy.get(model.id).preview : ""}`; placeLabel(selectedLabel, new THREE.Vector3(model.position[0], 0, model.position[1]), 3.4); }
      }
      updateAgentBatches();
      renderer.render(scene, camera);
      frameCount++;
      if (time - frameSampleAt > 1000) {
        canvas.dataset.fps = String(Math.round(frameCount * 1000 / (time - frameSampleAt)));
        canvas.dataset.drawCalls = String(renderer.info.render.calls);
        canvas.dataset.outsideAgents = String([...agents.values()].filter(entry => isOutside(entry.model, entry.motion)).length);
        frameSampleAt = time; frameCount = 0;
      }
    } catch (error) { cancelAnimationFrame(frame); onError?.(error); }
  }
  frame = requestAnimationFrame(animate);

  function dispose() {
    if (disposed) return;
    if (savePending) saveCamera();
    clearTimeout(saveTimer);
    controls.removeEventListener("change", cameraChanged);
    controls.removeEventListener("end", queueCameraSave);
    disposed = true; cancelAnimationFrame(frame); observer.disconnect(); controls.dispose();
    canvas.removeEventListener("pointerdown", down); canvas.removeEventListener("pointerup", up);
    canvas.removeEventListener("pointermove", move); canvas.removeEventListener("pointerleave", leave);
    canvas.removeEventListener("webglcontextlost", contextLost);
    staticRoot.children.forEach(mesh => mesh.dispose?.());
    groundGeometry?.dispose();
    dynamicRoot.children.forEach(mesh => mesh.dispose());
    art.dispose(); ownResources.forEach(resource => resource.dispose());
    sun.shadow.dispose(); renderer.dispose(); renderer.forceContextLoss(); canvas.remove(); labels.remove();
  }
  return { update, dispose };
}
