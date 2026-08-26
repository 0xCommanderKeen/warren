"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const zlib = require("node:zlib");
const { spawn, spawnSync } = require("node:child_process");
const { abortError, cdp, delay, stop } = require("./browser-process");
const approvals = require("../viewer/approval-knocks.js");

const ROOT = path.resolve(__dirname, "..");
const SNAPSHOT_DIR = path.join(__dirname, "snapshots", "fleet-layout");
// CI run 32891390327 measured 32 completed phases plus the phone phase at 180s;
// the remaining desktop phase normally costs another 3–12s there. This 240s
// parent bound covers that measured cold-run orchestration with headroom. Every
// server, condition and CDP operation below retains its strict 1–30s deadline.
const E2E_TIMEOUT_MS = 240000;
const SERVER_READY_TIMEOUT_MS = 8000;
const SERVER_REQUEST_TIMEOUT_MS = 1000;
const BROWSER_CONDITION_TIMEOUT_MS = 10000;
const CDP_OPERATION_TIMEOUT_MS = 10000;
// A cold CI Chrome can need longer to create and attach its first target. Keep
// steady-state CDP failures fast while bounding only the startup handshake.
const CHROME_STARTUP_HANDSHAKE_TIMEOUT_MS = 30000;
// Baselines are immutable in ordinary runs. This deliberately conspicuous opt-in is
// the only write path, so a rendering regression cannot bless itself in CI.
const UPDATE_BASELINES = process.env.BURROW_UPDATE_VISUAL_BASELINES === "1";
const SNAPSHOT_PLATFORMS = new Set(["darwin", "linux"]);
// A >12 channel delta filters subpixel edge noise. Allowing only 0.15% such pixels
// (and MAE 0.35) still fails a shifted glyph, control, panel edge, or canvas feature.
const VISUAL_TOLERANCE = Object.freeze({ channelDelta: 12, changedRatio: 0.0015,
  meanAbsoluteError: 0.35 });

function chromeExecutable() {
  const candidates = [process.env.CHROME_BIN, process.env.GOOGLE_CHROME_BIN,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium"];
  for (const candidate of candidates) if (candidate && fs.existsSync(candidate)) return candidate;
  for (const command of ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]) {
    const found = spawnSync("which", [command], { encoding: "utf8" });
    if (found.status === 0 && found.stdout.trim()) return found.stdout.trim();
  }
  throw new Error("Chrome is required for the production 320px layout test; set CHROME_BIN " +
    "or install google-chrome (expected on GitHub Ubuntu runners)");
}

async function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(error => error ? reject(error) : resolve(port));
    });
  });
}

async function waitForServer(port, process, signal) {
  const deadline = Date.now() + SERVER_READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (signal.aborted) throw abortError(signal);
    if (process.exitCode !== null) throw new Error(`Burrow server exited with ${process.exitCode}`);
    const ready = await new Promise((resolve, reject) => {
      const request = http.get(`http://127.0.0.1:${port}/`, response => {
        signal.removeEventListener("abort", onAbort);
        response.resume(); resolve(response.statusCode === 200);
      });
      const onAbort = () => { request.destroy(); reject(abortError(signal)); };
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();
      request.setTimeout(SERVER_REQUEST_TIMEOUT_MS, () => request.destroy());
      request.once("error", error => {
        signal.removeEventListener("abort", onAbort);
        if (signal.aborted) reject(abortError(signal)); else resolve(false);
      });
    });
    if (ready) return;
    await delay(50, signal);
  }
  throw new Error(`Burrow server did not become ready within ${SERVER_READY_TIMEOUT_MS}ms`);
}

async function eventually(evaluate, expression, signal, phase = "browser condition",
  timeoutMs = BROWSER_CONDITION_TIMEOUT_MS) {
  signal ??= evaluate.signal;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const remaining = Math.max(1, deadline - Date.now());
    let result;
    try {
      result = await evaluate("Runtime.evaluate", { expression, returnByValue: true,
        awaitPromise: true }, { timeout: remaining });
    } catch (error) {
      throw new Error(`${phase} failed before its ${timeoutMs}ms deadline: ${expression}; ` +
        error.message, { cause: error });
    }
    if (result.result.value) return result.result.value;
    await delay(50, signal);
  }
  throw new Error(`${phase} timed out after ${timeoutMs}ms: ${expression}`);
}

function decodePng(png) {
  assert.equal(png.toString("ascii", 1, 4), "PNG");
  const width = png.readUInt32BE(16), height = png.readUInt32BE(20);
  const bitDepth = png[24], colorType = png[25];
  assert.equal(bitDepth, 8, "snapshot uses supported 8-bit pixels");
  const channels = colorType === 6 ? 4 : colorType === 2 ? 3 : 0;
  assert.ok(channels, `snapshot uses supported RGB/RGBA pixels, got PNG type ${colorType}`);
  const chunks = [];
  for (let offset = 8; offset < png.length;) {
    const length = png.readUInt32BE(offset), type = png.toString("ascii", offset + 4, offset + 8);
    if (type === "IDAT") chunks.push(png.subarray(offset + 8, offset + 8 + length));
    offset += 12 + length;
  }
  const packed = zlib.inflateSync(Buffer.concat(chunks));
  const stride = width * channels, pixels = Buffer.alloc(stride * height);
  const paeth = (a, b, c) => { const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c); return pa <= pb && pa <= pc ? a : pb <= pc ? b : c; };
  for (let y = 0, source = 0; y < height; y++) {
    const filter = packed[source++], row = y * stride, previous = row - stride;
    for (let x = 0; x < stride; x++) {
      const raw = packed[source++], left = x >= channels ? pixels[row + x - channels] : 0;
      const up = y ? pixels[previous + x] : 0;
      const upperLeft = y && x >= channels ? pixels[previous + x - channels] : 0;
      const predictor = filter === 0 ? 0 : filter === 1 ? left : filter === 2 ? up :
        filter === 3 ? Math.floor((left + up) / 2) : filter === 4 ? paeth(left, up, upperLeft) : NaN;
      assert.ok(Number.isFinite(predictor), `unsupported PNG filter ${filter}`);
      pixels[row + x] = (raw + predictor) & 255;
    }
  }
  return { width, height, channels, pixels };
}

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function encodePng(width, height, rgba) {
  const signature = Buffer.from("89504e470d0a1a0a", "hex");
  const chunk = (type, data) => {
    const name = Buffer.from(type), result = Buffer.alloc(data.length + 12);
    result.writeUInt32BE(data.length); name.copy(result, 4); data.copy(result, 8);
    result.writeUInt32BE(crc32(Buffer.concat([name, data])), data.length + 8);
    return result;
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width); header.writeUInt32BE(height, 4);
  header[8] = 8; header[9] = 6;
  const rows = Buffer.alloc((width * 4 + 1) * height);
  for (let y = 0; y < height; y++) rgba.copy(rows, y * (width * 4 + 1) + 1,
    y * width * 4, (y + 1) * width * 4);
  return Buffer.concat([signature, chunk("IHDR", header),
    chunk("IDAT", zlib.deflateSync(rows, { level: 9 })), chunk("IEND", Buffer.alloc(0))]);
}

function rgbaPixels(image) {
  if (image.channels === 4) return image.pixels;
  const rgba = Buffer.alloc(image.width * image.height * 4);
  for (let source = 0, target = 0; source < image.pixels.length; source += 3, target += 4) {
    image.pixels.copy(rgba, target, source, source + 3); rgba[target + 3] = 255;
  }
  return rgba;
}

function compareSnapshot(name, actualPng, platform = process.platform) {
  const artifacts = fs.mkdtempSync(path.join(os.tmpdir(), `burrow-visual-${name}-${platform}-`));
  fs.writeFileSync(path.join(artifacts, "actual.png"), actualPng);
  try {
    assert.ok(SNAPSHOT_PLATFORMS.has(platform),
      `visual baselines are supported only on darwin and linux, got ${platform}`);
    const baselinePath = path.join(SNAPSHOT_DIR, `${name}.${platform}.png`);
    if (UPDATE_BASELINES) {
      assert.notEqual(process.env.CI, "true",
        "refusing to update visual baselines in CI; review and commit platform output explicitly");
      fs.mkdirSync(SNAPSHOT_DIR, { recursive: true });
      fs.writeFileSync(baselinePath, actualPng);
      fs.rmSync(artifacts, { recursive: true });
      return { updated: true };
    }
    assert.ok(fs.existsSync(baselinePath),
      `missing visual baseline ${baselinePath}; actual=${path.join(artifacts, "actual.png")}; ` +
      "create/review it only on that platform with BURROW_UPDATE_VISUAL_BASELINES=1");
    const actual = decodePng(actualPng), expectedPng = fs.readFileSync(baselinePath);
    fs.writeFileSync(path.join(artifacts, "expected.png"), expectedPng);
    const expected = decodePng(expectedPng);
    assert.deepEqual([actual.width, actual.height], [expected.width, expected.height],
      `${name} visual dimensions differ; artifacts=${artifacts}`);
    const actualRgba = rgbaPixels(actual), expectedRgba = rgbaPixels(expected);
    const diff = Buffer.alloc(actualRgba.length); let changed = 0, absolute = 0, maximum = 0;
    for (let offset = 0; offset < actualRgba.length; offset += 4) {
      let pixelDelta = 0;
      for (let channel = 0; channel < 3; channel++) {
        const delta = Math.abs(actualRgba[offset + channel] - expectedRgba[offset + channel]);
        absolute += delta; maximum = Math.max(maximum, delta); pixelDelta = Math.max(pixelDelta, delta);
        diff[offset + channel] = delta;
      }
      diff[offset + 3] = 255;
      if (pixelDelta > VISUAL_TOLERANCE.channelDelta) changed++;
    }
    fs.writeFileSync(path.join(artifacts, "diff.png"), encodePng(actual.width, actual.height, diff));
    const pixels = actual.width * actual.height;
    const metrics = { changedPixels: changed, pixels,
      changedRatio: changed / pixels, meanAbsoluteError: absolute / (pixels * 3), maximumDelta: maximum };
    assert.ok(metrics.changedRatio <= VISUAL_TOLERANCE.changedRatio &&
      metrics.meanAbsoluteError <= VISUAL_TOLERANCE.meanAbsoluteError,
    `${name} visual mismatch: ${JSON.stringify(metrics)}; ` +
      `tolerance=${JSON.stringify(VISUAL_TOLERANCE)}; artifacts=${artifacts}`);
    fs.rmSync(artifacts, { recursive: true });
    return metrics;
  } catch (error) {
    if (!String(error.message).includes("artifacts=")) error.message += `; artifacts=${artifacts}`;
    throw error;
  }
}

function pixelEvidence(png, regions) {
  const image = decodePng(png);
  const color = (x, y) => {
    const offset = (y * image.width + x) * image.channels;
    return image.pixels.toString("hex", offset, offset + 3);
  };
  const evidence = regions.map(([x0, y0, x1, y1]) => {
    const colors = new Set(); let light = 0;
    const known = { "142317": 0, "17130f": 0, "e8dfc8": 0, "d2a15c": 0 };
    for (let y = y0; y < y1; y += 3) for (let x = x0; x < x1; x += 3) {
      const value = color(x, y); colors.add(value);
      if (value in known) known[value]++;
      const offset = (y * image.width + x) * image.channels;
      if (image.pixels[offset] + image.pixels[offset + 1] + image.pixels[offset + 2] > 500) light++;
    }
    return { colors: colors.size, light, known };
  });
  return { ...image, evidence };
}

async function pressTab(send, shift = false) {
  const modifiers = shift ? 8 : 0;
  await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9, modifiers });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9, modifiers });
}

test("production fleet browser fixture runs isolated interaction and visual phases",
  { timeout: E2E_TIMEOUT_MS }, async t => {
  const { signal } = t;
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "burrow-layout-"));
  const events = path.join(temporary, "events.jsonl");
  const frozenNow = Date.parse("2025-01-15T12:00:00.000Z");
  const currentEvent = { v: 0, ts: new Date(frozenNow).toISOString(),
    source: "claude-code", agent_id: "layout-resident", project: "burrow",
    type: "tool_called", payload: { tool: "Read",
      detail: "a/very/long/production/path/that/must/wrap/without/forcing/the/ledger/wider/than/the/viewport.md" } };
  const agedEvent = { ...currentEvent, ts: new Date(frozenNow - 13 * 60 * 60 * 1000).toISOString(),
    agent_id: "aged-worker", type: "idle", payload: {} };
  const agedAttention = { ...agedEvent, agent_id: "aged-knocker", type: "needs_human",
    payload: { message: "A retained request still needs a truthful destination" } };
  fs.writeFileSync(events, [agedAttention, agedEvent, currentEvent]
    .map(value => JSON.stringify(value)).join("\n") + "\n");
  const villagers = path.join(temporary, "villagers");
  fs.mkdirSync(villagers);
  fs.writeFileSync(path.join(villagers, "unsafe.resident.json"), JSON.stringify({
    manifest_version: 1, match: { agent_id: "inactive-resident" }, home: 4,
    soul: { name: "Safe Name", char: "Monk", accent: "#a68a4f", role: "keeper",
      description: "Safe public description." },
    skills: [{ id: "summary", status_ref: "config:summary", access_token: "browser-secret" }],
    memory: { ref: "file:///memory.md", status_ref: "config:memory" }, routes: [], app_grants: [],
  }));
  fs.writeFileSync(path.join(villagers, "routine.resident.json"), JSON.stringify({
    manifest_version: 1, match: { project: "quiet-project" }, home: 5,
    soul: { name: "Routine Keeper", char: "Monk", accent: "#a68a4f", role: "keeper",
      description: "Keeps declared standing work visible while away." },
    skills: [], memory: { ref: "config:memory", status_ref: "config:memory" },
    routes: [], app_grants: [], routines: [{ id: "daily-summary", schedule: "0 7 * * *",
      schedule_tz: "Europe/Ljubljana", enabled: true, steward_resident: "life-agent" }],
  }));
  const port = await freePort();
  const server = spawn(process.env.PYTHON || "python3", ["serve.py", String(port)], {
    cwd: ROOT, env: { ...process.env, BURROW_EVENTS: events, BURROW_VILLAGERS: villagers },
    stdio: ["ignore", "ignore", "pipe"],
  });
  let chrome, testFailure;
  try {
    await waitForServer(port, server, signal);
    chrome = spawn(chromeExecutable(), ["--headless=new", "--disable-gpu", "--no-sandbox",
      "--force-color-profile=srgb", "--disable-lcd-text", "--font-render-hinting=none",
      "--remote-debugging-pipe", `--user-data-dir=${path.join(temporary, "chrome")}`, "about:blank"],
    { detached: process.platform !== "win32",
      stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"] });
    let chromeErrors = "";
    chrome.stderr.on("data", chunk => { chromeErrors += chunk; });
    const send = cdp(chrome, { signal, operationTimeout: CDP_OPERATION_TIMEOUT_MS });
    const target = await send("Target.createTarget", { url: "about:blank" },
      { browser: true, timeout: CHROME_STARTUP_HANDSHAKE_TIMEOUT_MS });
    const attached = await send("Target.attachToTarget",
      { targetId: target.targetId, flatten: true },
      { browser: true, timeout: CHROME_STARTUP_HANDSHAKE_TIMEOUT_MS });
    send.setSession(attached.sessionId);
    await send("Emulation.setDeviceMetricsOverride", { width: 320, height: 800,
      deviceScaleFactor: 1, mobile: false });
    await send("Page.enable");
    await send("Runtime.enable");
    await send("Emulation.setEmulatedMedia", { features: [
      { name: "prefers-reduced-motion", value: "no-preference" },
      { name: "prefers-contrast", value: "no-preference" },
      { name: "prefers-color-scheme", value: "dark" },
    ] });
    await send("Page.addScriptToEvaluateOnNewDocument", { source: `{
      const NativeDate = Date; let now = ${frozenNow};
      class FrozenDate extends NativeDate { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } }
      FrozenDate.parse = NativeDate.parse; FrozenDate.UTC = NativeDate.UTC; window.Date = FrozenDate;
      window.__advanceBurrowClock = milliseconds => { now += milliseconds; return now; };
      let randomState = 0x42c0ffee;
      Math.random = () => ((randomState = (1664525 * randomState + 1013904223) >>> 0) / 4294967296);
      window.setInterval = () => 0; window.clearInterval = () => {};
      Object.defineProperty(window, 'EventSource', { configurable: true, value: undefined });
      addEventListener('DOMContentLoaded', () => {
        const style = document.createElement('style');
        style.textContent = '@font-face{font-family:BurrowSnapshot;src:url(/assets/fonts/CousineSnapshot.ttf) format("truetype");font-style:normal;font-weight:400;font-display:block}html body,html button,html input,html select{font-family:BurrowSnapshot,monospace!important}';
        document.head.appendChild(style);
      }, { once: true });
    }` });
    const resetPage = async (width = 320, height = 800) => {
      await send("Emulation.setDeviceMetricsOverride", { width, height,
        deviceScaleFactor: 1, mobile: false });
      await send("Page.navigate", { url: `http://127.0.0.1:${port}/` });
      await eventually(send, "document.readyState === 'complete' && !!window.BurrowFleetController",
        signal, `reset production page at ${width}x${height}`);
    };
    const waitForTelemetry = () => eventually(send, `(() => {
      if (typeof runtime === 'undefined') return false;
      const snapshot = runtime.snapshot();
      return BurrowRoutines.telemetryAvailability(snapshot.transport, snapshot.cursor).ok;
    })()`, signal, "production telemetry readiness");

    await t.test("production map routing validates promised capacity without all-pairs A*", async () => {
      await resetPage();
    const routing = await eventually(send, `(() => {
      if (!scene || !scene.routing) return false;
      let findPathCalls = 0;
      const productionRouting = scene.routing;
      scene.routing = { ...productionRouting, findPath: (...args) => {
        findPathCalls++; return productionRouting.findPath(...args);
      }};
      const check = scene.checkMap();
      scene.routing = productionRouting;
      const lodge = { kind: 'building', id: 'visitor-lodge' };
      const endpoints = Array.from({ length: scene.routing.capacity(lodge) },
        (_, slot) => scene.routing.endpoint(lodge, slot));
      return { problems: check.problems, findPathCalls, lodgeCapacity: endpoints.length,
        distinctLodgeEndpoints: new Set(endpoints.map(p => p.x + ',' + p.y)).size,
        validatedEndpoints: scene.routing.validate().endpoints.length };
    })()`);
    assert.deepEqual(routing.problems, [], `production map routing failed: ${routing.problems}`);
    assert.equal(routing.findPathCalls, 0, "production map self-check must not run all-pairs A*");
    assert.equal(routing.lodgeCapacity, 32);
    assert.equal(routing.distinctLodgeEndpoints, 32);
    assert.ok(routing.validatedEndpoints >= 8 * 4 + 32 + 16,
      "production self-check did not cover the promised shared/lodge/knock fleet");
    });

    await t.test("village lifecycle updates remain atomic across routing failures", async () => {
      await resetPage();
    const lifecycle = await eventually(send, `(() => {
      if (!scene || !scene.routing || !scene.slotAllocator) return false;
      scene.updateVillage([]);
      const villager = (id, place, residency = 'resident') => ({ id, name: id,
        char: 'Villager', accent: '#abcdef', project: 'routing-test', residency,
        home: residency === 'resident' ? 0 : null, base: residency === 'visitor'
          ? 'visitor-lodge' : 'resident-home', state: 'working', place, doing: 'test', lastTs: 1 });

      const first = villager('first-visitor', 'library', 'visitor');
      const firstAccepted = scene.updateVillage([first]);
      const firstViz = scene.viz.get(first.id);
      const lodge = { kind: 'building', id: 'visitor-lodge' };
      const lodgeStarts = Array.from({ length: scene.routing.capacity(lodge) },
        (_, slot) => scene.routing.endpoint(lodge, slot));
      const startsAtLodge = lodgeStarts.some(point =>
        point.x === firstViz.cont.x && point.y === firstViz.cont.y);
      const visitorJourney = { accepted: firstAccepted, startsAtLodge,
        hasTween: Boolean(firstViz.walk), destination: firstViz.dest && firstViz.dest.id,
        targetDiffers: firstViz.target.x !== firstViz.cont.x || firstViz.target.y !== firstViz.cont.y,
        residentDoor: scene.routing.endpoint({ kind: 'home', plot: 0 }, 0, first.state),
        actualStart: { x: firstViz.cont.x, y: firstViz.cont.y } };

      scene.updateVillage([]);
      const left = Array.from({ length: 16 }, (_, i) => villager('left-' + i, 'library'));
      const right = Array.from({ length: 16 }, (_, i) => villager('right-' + i, 'workshop'));
      const seeded = scene.updateVillage([...left, ...right]);
      const exchange = [...left.map(v => ({ ...v, place: 'workshop', lastTs: 2 })),
        ...right.map(v => ({ ...v, place: 'library', lastTs: 2 }))];
      const exchanged = scene.updateVillage(exchange);
      const exchangeCorrect = exchange.every(v => scene.viz.get(v.id).dest.id === v.place);

      const watched = scene.viz.get('left-0');
      const beforeOverflow = { count: scene.viz.size, lastTs: watched.lastTs,
        chip: watched.chip.el.outerHTML, walk: watched.walk, x: watched.cont.x, y: watched.cont.y,
        target: watched.target, dest: watched.dest, slotKey: watched.slotKey,
        state: watched.state, home: watched.home, texture: watched.spr.texture.key };
      const overflow = [...exchange,
        villager('overflow', 'workshop')];
      const overflowAccepted = scene.updateVillage(overflow);
      const afterOverflow = { count: scene.viz.size, lastTs: watched.lastTs,
        chip: watched.chip.el.outerHTML, walk: watched.walk, x: watched.cont.x, y: watched.cont.y,
        target: watched.target, dest: watched.dest, slotKey: watched.slotKey,
        state: watched.state, home: watched.home, texture: watched.spr.texture.key };

      const originalRoute = scene.routing.route;
      const beforeFailure = { walk: watched.walk, x: watched.cont.x, y: watched.cont.y,
        target: watched.target, dest: watched.dest, slotKey: watched.slotKey,
        state: watched.state, home: watched.home, texture: watched.spr.texture.key,
        lastTs: watched.lastTs, chip: watched.chip.el.outerHTML };
      scene.routing = { ...scene.routing, route: () => null };
      const failed = scene.updateVillage(exchange.map(v => v.id === 'left-0'
        ? { ...v, place: 'library', char: 'Monk', name: 'mutated', lastTs: 99 } : v));
      scene.routing = { ...scene.routing, route: originalRoute };
      const afterFailure = { walk: watched.walk, x: watched.cont.x, y: watched.cont.y,
        target: watched.target, dest: watched.dest, slotKey: watched.slotKey,
        state: watched.state, home: watched.home, texture: watched.spr.texture.key,
        lastTs: watched.lastTs, chip: watched.chip.el.outerHTML };
      let allocatorCalls = 0, routeCalls = 0, spawnCalls = 0, walkCalls = 0, tweenCalls = 0;
      const allocator = scene.slotAllocator, routingNow = scene.routing;
      const originalSpawn = scene.spawn, originalWalkTo = scene.walkTo;
      const originalTweenAdd = scene.tweens.add, originalTweenChain = scene.tweens.chain;
      scene.slotAllocator = { ...allocator, reconcile: (...args) => {
        allocatorCalls++; return allocator.reconcile(...args);
      }};
      scene.routing = { ...routingNow, route: (...args) => {
        routeCalls++; return routingNow.route(...args);
      }};
      scene.spawn = (...args) => { spawnCalls++; return originalSpawn.apply(scene, args); };
      scene.walkTo = (...args) => { walkCalls++; return originalWalkTo.apply(scene, args); };
      scene.tweens.add = (...args) => { tweenCalls++; return originalTweenAdd.apply(scene.tweens, args); };
      scene.tweens.chain = (...args) => { tweenCalls++; return originalTweenChain.apply(scene.tweens, args); };
      const beforeDuplicate = { containers: scene.children.list.length,
        labels: document.querySelector('#labels').children.length, chips: scene.chips.length,
        viz: scene.viz.size,
        plot: scene.plotOf.get('left-0'), watched: JSON.stringify({ lastTs: watched.lastTs,
          target: watched.target, dest: watched.dest, slotKey: watched.slotKey,
          state: watched.state, chip: watched.chip.el.outerHTML }) };
      const duplicateAccepted = scene.updateVillage([
        { ...exchange[0], place: 'library', lastTs: 101 },
        { ...exchange[0], place: 'workshop', lastTs: 102 },
        ...exchange.slice(1),
      ]);
      const afterDuplicate = { containers: scene.children.list.length,
        labels: document.querySelector('#labels').children.length, chips: scene.chips.length,
        viz: scene.viz.size,
        plot: scene.plotOf.get('left-0'), watched: JSON.stringify({ lastTs: watched.lastTs,
          target: watched.target, dest: watched.dest, slotKey: watched.slotKey,
          state: watched.state, chip: watched.chip.el.outerHTML }) };
      scene.slotAllocator = allocator; scene.routing = routingNow;
      scene.spawn = originalSpawn; scene.walkTo = originalWalkTo;
      scene.tweens.add = originalTweenAdd; scene.tweens.chain = originalTweenChain;
      return { visitorJourney, residentHomeWasNotStart:
          visitorJourney.actualStart.x !== visitorJourney.residentDoor.x ||
          visitorJourney.actualStart.y !== visitorJourney.residentDoor.y,
        seeded, exchanged, exchangeCorrect, overflowAccepted,
        overflowAtomic: Object.keys(beforeOverflow).every(key => beforeOverflow[key] === afterOverflow[key]),
        failed, failureAtomic: Object.keys(beforeFailure).every(key => beforeFailure[key] === afterFailure[key]),
        duplicateAccepted, duplicateAtomic: JSON.stringify(beforeDuplicate) === JSON.stringify(afterDuplicate),
        duplicateCalls: { allocatorCalls, routeCalls, spawnCalls, walkCalls, tweenCalls } };
    })()`);
    assert.deepEqual(lifecycle.visitorJourney,
      { accepted: true, startsAtLodge: true, hasTween: true, destination: "library",
        targetDiffers: true, residentDoor: lifecycle.visitorJourney.residentDoor,
        actualStart: lifecycle.visitorJourney.actualStart });
    assert.equal(lifecycle.residentHomeWasNotStart, true);
    assert.equal(lifecycle.seeded, true);
    assert.equal(lifecycle.exchanged, true, "a full destination exchange needs no spare slot");
    assert.equal(lifecycle.exchangeCorrect, true);
    assert.equal(lifecycle.overflowAccepted, false);
    assert.equal(lifecycle.overflowAtomic, true, "overflow partially mutated the production scene");
    assert.equal(lifecycle.failed, false);
    assert.equal(lifecycle.failureAtomic, true, "failed active reroute mutated the production scene");
    assert.equal(lifecycle.duplicateAccepted, false);
    assert.equal(lifecycle.duplicateAtomic, true,
      "duplicate snapshot left an orphan container/chip or mutated the production scene");
    assert.deepEqual(lifecycle.duplicateCalls,
      { allocatorCalls: 0, routeCalls: 0, spawnCalls: 0, walkCalls: 0, tweenCalls: 0 });
    });

    await t.test("fleet controls preserve logical focus across rerenders and detail views", async () => {
      await resetPage();
    await eventually(send, "window.dispatchEvent(new Event('resize')); " +
      "document.querySelector('#fleet-open').click(); " +
      "document.querySelector('#panel.open.ledger .ledger-filters') && true");
    const searchFocus = await eventually(send, `(async () => {
      const search = document.querySelector('[data-fleet-focus="filter:query"]');
      if (!search) return false;
      search.focus(); search.value = 'needle'; search.setSelectionRange(1, 4, 'forward');
      search.dispatchEvent(new Event('input', { bubbles: true }));
      await new Promise(resolve => setTimeout(resolve, 1100));
      return { key: document.activeElement.dataset.fleetFocus,
        start: document.activeElement.selectionStart, end: document.activeElement.selectionEnd };
    })()`);
    assert.deepEqual(searchFocus, { key: "filter:query", start: 1, end: 4 });

    const tabFocus = await eventually(send, `(async () => {
      const tab = document.querySelector('[data-fleet-tab="activity"]');
      tab.focus(); tab.dispatchEvent(new KeyboardEvent('keydown',
        { key: 'ArrowRight', bubbles: true }));
      await new Promise(resolve => setTimeout(resolve, 1100));
      return document.activeElement.dataset.fleetFocus;
    })()`);
    assert.equal(tabFocus, "tab:attention");

    await eventually(send, "document.querySelector('[data-fleet-tab=\"residents\"]').click(); " +
      "!!document.querySelector('.invalid-resident .status-mark.invalid')");
    const capabilityFocus = await eventually(send, `(async () => {
      const row = document.querySelector('.cap-row:not(:disabled)'); row.focus();
      const key = row.dataset.fleetFocus;
      await new Promise(resolve => setTimeout(resolve, 1100));
      return { expected: key, actual: document.activeElement.dataset.fleetFocus };
    })()`);
    assert.equal(capabilityFocus.actual, capabilityFocus.expected);
    await eventually(send, "document.activeElement.click(); " +
      "document.querySelector('#panel').getAttribute('aria-label') === 'Villager details' && " +
      "document.querySelector('#panel-body').textContent.includes('inactive · no current signal')");
    await eventually(send, "document.querySelector('#fleet-open').click(); " +
      "document.querySelector('[data-fleet-tab=\"activity\"]').click(); " +
      "document.querySelector('[name=\"query\"]').value = ''; " +
      "document.querySelector('[name=\"query\"]').dispatchEvent(new Event('input', { bubbles: true })); " +
      "!!document.querySelector('.ledger-entry')");
    await eventually(send, "document.querySelector('[data-agent=\"aged-worker\"]').click(); " +
      "document.querySelector('#panel-body').textContent.includes('latest retained event') && " +
      "document.querySelector('#panel-body').textContent.includes('inactive · no current signal')");
    await eventually(send, "document.querySelector('#fleet-open').click(); " +
      "document.querySelector('[data-fleet-tab=\"attention\"]').click(); " +
      "document.querySelector('[data-agent=\"aged-knocker\"]').click(); " +
      "document.querySelector('#panel-body').textContent.includes('retained request still needs')");
    await eventually(send, "document.querySelector('#fleet-open').click(); " +
      "document.querySelector('[data-fleet-tab=\"activity\"]').click(); true");
    });

    await t.test("320px ledger stays bounded and live status remains stable", async () => {
      await resetPage();
      await eventually(send, "document.querySelector('#fleet-open').click(); " +
        "document.querySelector('[data-fleet-tab=\"activity\"]').click(); " +
        "!!document.querySelector('.ledger-entry')");
    await send("Runtime.evaluate", { expression:
      "new Promise(resolve => setTimeout(resolve, 300))", awaitPromise: true });
    const result = await eventually(send, `(() => {
      const panel = document.querySelector('#panel.open.ledger');
      if (!panel || !document.querySelector('.ledger-entry')) return false;
      const root = document.documentElement;
      return { viewport: innerWidth, documentWidth: root.scrollWidth,
        panelClientWidth: panel.clientWidth, panelScrollWidth: panel.scrollWidth,
        panelLeft: panel.getBoundingClientRect().left,
        panelRight: panel.getBoundingClientRect().right,
        tabs: document.querySelectorAll('[role="tab"]').length,
        filters: document.querySelectorAll('.ledger-filters input, .ledger-filters select').length,
        entryText: document.querySelector('.ledger-entry').textContent,
        offenders: [...document.querySelectorAll('body *')].filter(element => {
          const box = element.getBoundingClientRect();
          return box.right > innerWidth + 1 || box.width > innerWidth + 1;
        }).slice(0, 8).map(element => ({ tag: element.tagName, id: element.id,
          className: String(element.className || ''), box: element.getBoundingClientRect().toJSON() })),
        scrollOffenders: [...document.querySelectorAll('#panel, #panel *')]
          .filter(element => element.scrollWidth > element.clientWidth + 1).slice(0, 8)
          .map(element => ({ tag: element.tagName, id: element.id,
            className: String(element.className || ''), client: element.clientWidth,
            scroll: element.scrollWidth })) };
    })()`);
    assert.equal(result.viewport, 320);
    assert.ok(result.tabs >= 3 && result.filters === 5, "the open production ledger rendered");
    assert.match(result.entryText, /very\/long\/production\/path/);
    assert.ok(result.panelLeft >= 0 && result.panelRight <= result.viewport,
      `ledger escaped the viewport: ${result.panelLeft}px..${result.panelRight}px; ` +
      JSON.stringify(result.offenders));
    assert.ok(result.panelScrollWidth <= result.panelClientWidth,
      `ledger overflowed: ${result.panelScrollWidth}px > ${result.panelClientWidth}px; ` +
      JSON.stringify(result.scrollOffenders));
    const announcements = await eventually(send, `(async () => {
      const live = document.querySelector('#panel-status');
      const initial = live.textContent; let mutations = 0;
      const observer = new MutationObserver(records => { mutations += records.length; });
      observer.observe(live, { childList: true, characterData: true, subtree: true });
      await new Promise(resolve => setTimeout(resolve, 2200)); observer.disconnect();
      return { initial, final: live.textContent, mutations,
        liveLedgerBodies: document.querySelectorAll('#panel-body section[aria-live]').length };
    })()`);
    assert.deepEqual(announcements, { initial: announcements.initial, final: announcements.initial,
      mutations: 0, liveLedgerBodies: 0 });
    assert.match(announcements.initial, /^Activity: \d+ items?\.$/);
    });

    await t.test("mood glyph shares truthful details across panel, resident, and Visitor views", async () => {
      const moodAgent = "codex:mood-browser", visitorAgent = "codex:mood-visitor";
      const at = minutes => new Date(frozenNow + minutes * 60_000).toISOString();
      const ordinary = (agent, minutes, type, payload, source = "codex") =>
        ({v:0,ts:at(minutes),source,agent_id:agent,project:"mood-project",type,payload});
      const moodEvents = [
        ordinary(moodAgent,-120,"tool_called",{tool:"Read"}),
        ordinary(moodAgent,-100,"task_started",{prompt:"Inspect mood"},"claude-code"),
        ordinary(moodAgent,-90,"tool_called",{tool:"Read"}),
        ordinary(moodAgent,-75,"tool_called",{tool:"Read"}),
        ordinary(moodAgent,-60,"tool_called",{tool:"Read"}),
        ordinary(moodAgent,-45,"tool_called",{tool:"Read"}),
        ordinary(moodAgent,-31,"heartbeat",{}),
        ordinary(visitorAgent,-480,"needs_human",{message:"Choose",request_id:"mood-browser-r",
          action:"deploy",detail:null,options:["approve","deny"]}),
        ordinary(visitorAgent,-31,"tool_called",{tool:"Read"}),
      ];
      fs.writeFileSync(events, [...moodEvents, currentEvent].map(JSON.stringify).join("\n") + "\n");
      const moodManifest = path.join(villagers, "mood.resident.json");
      fs.writeFileSync(moodManifest, JSON.stringify({manifest_version:1,
        match:{agent_id:moodAgent},home:6,soul:{name:"Mood Keeper",char:"Monk",
          accent:"#a68a4f",role:"observer",description:"Reads retained operational evidence."},
        skills:[],memory:{ref:"config:mood",status_ref:"config:mood"},routes:[],app_grants:[]}));
      await resetPage();
      const initial = await eventually(send, `(async()=>{
        await runtime.poll();await runtime.refreshResidents();
        const resident=villagers.find(v=>v.id==='${moodAgent}');
        const visitor=villagers.find(v=>v.id==='${visitorAgent}');
        if(!resident||!visitor||!resident.mood||!visitor.mood)return false;
        openPanel(resident.id);await new Promise(r=>requestAnimationFrame(r));
        const details=document.querySelector('#panel-body details[data-mood]');
        if(!details)return false;
        const summary=details.querySelector('summary'),css=getComputedStyle(details);
        window.__moodBefore=JSON.stringify(resident.mood);
        return {resident:resident.mood,visitor:visitor.mood,summary:summary.textContent,
          aria:summary.getAttribute('aria-label'),terms:details.querySelectorAll('dt').length,
          staleClass:details.classList.contains('mood-stale'),opacity:css.opacity,
          animation:css.animationName,transition:css.transitionDuration};
      })()`, signal, "production mood panel");
      assert.equal(initial.resident.status, "steady");
      assert.equal(initial.visitor.status, "blocked");
      assert.equal(initial.terms, 5);
      assert.match(initial.aria, /Mood Keeper: mood steady; open observed-signal breakdown/);
      assert.equal(initial.staleClass, true);
      assert.equal(initial.opacity, "0.45");
      assert.equal(initial.animation, "none");
      assert.equal(initial.transition, "0s");

      const summaryExpression = "document.querySelector('#panel-body details[data-mood] summary')";
      await send("Runtime.evaluate", {expression:`${summaryExpression}.click()`});
      assert.equal(await eventually(send,
        "document.querySelector('#panel-body details[data-mood]').open"), true, "click opens");
      await send("Runtime.evaluate", {expression:`${summaryExpression}.focus()`});
      await send("Input.dispatchKeyEvent", {type:"rawKeyDown",key:"Escape",code:"Escape",windowsVirtualKeyCode:27});
      await send("Input.dispatchKeyEvent", {type:"keyUp",key:"Escape",code:"Escape",windowsVirtualKeyCode:27});
      const escaped = await eventually(send, `(()=>{const d=document.querySelector('#panel-body details[data-mood]');
        return !d.open&&document.activeElement===d.querySelector('summary')})()`);
      assert.equal(escaped, true, "Escape closes and returns focus");
      await send("Input.dispatchKeyEvent", {type:"keyDown",key:"Enter",code:"Enter",windowsVirtualKeyCode:13});
      await send("Input.dispatchKeyEvent", {type:"keyUp",key:"Enter",code:"Enter",windowsVirtualKeyCode:13});
      assert.equal(await eventually(send,
        "document.querySelector('#panel-body details[data-mood]').open"), true, "Enter opens");
      await send("Input.dispatchKeyEvent", {type:"keyDown",key:" ",code:"Space",windowsVirtualKeyCode:32});
      await send("Input.dispatchKeyEvent", {type:"keyUp",key:" ",code:"Space",windowsVirtualKeyCode:32});
      assert.equal(await eventually(send,
        "!document.querySelector('#panel-body details[data-mood]').open"), true, "Space toggles closed");
      const hover = await eventually(send, `(()=>{const d=document.querySelector('#panel-body details[data-mood]');
        d.open=false;d.querySelector('summary').dispatchEvent(new PointerEvent('pointerover',
          {bubbles:true,pointerType:'mouse'}));
        return d.open})()`);
      assert.equal(hover, true, "pointer hover reveals details");
      const touch = await eventually(send, `(()=>{const d=document.querySelector('#panel-body details[data-mood]');
        const summary=d.querySelector('summary');d.open=false;
        summary.dispatchEvent(new PointerEvent('pointerover',{bubbles:true,pointerType:'touch'}));
        const afterOver=d.open;
        summary.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerType:'touch'}));
        summary.click();return {afterOver,afterTap:d.open}})()`);
      assert.deepEqual(touch, {afterOver:false,afterTap:true},
        "touch pointerover does not consume the first native summary tap");

      const shared = await eventually(send, `(async()=>{
        window.__advanceBurrowClock(4*60*60*1000);runtime.tick();
        await new Promise(r=>requestAnimationFrame(r));
        const unchanged=window.__moodBefore===JSON.stringify(villagers.find(v=>v.id==='${moodAgent}').mood);
        openPanel('fleet-ledger');fleetTab='residents';renderPanel(Date.now());
        await new Promise(r=>requestAnimationFrame(r));
        const cards=[...document.querySelectorAll('.resident-card')];
        const resident=cards.find(card=>card.textContent.includes('Mood Keeper'));
        const lodge=cards.find(card=>card.textContent.includes('Visitor lodge'));
        return {unchanged,residentMood:resident&&resident.querySelector('.mood-status')?.textContent,
          visitorMood:lodge&&[...lodge.querySelectorAll('.mood-status')].map(x=>x.textContent),
          inactiveMood:cards.find(card=>card.textContent.includes('Safe Name'))?.querySelector('[data-mood]')||null};
      })()`, signal, "shared resident and visitor mood renderer");
      assert.deepEqual(shared, {unchanged:true,residentMood:"steady",
        visitorMood:["blocked","not enough observed"],inactiveMood:null});
      fs.unlinkSync(moodManifest);
      fs.writeFileSync(events, [currentEvent, agedEvent, agedAttention]
        .map(value => JSON.stringify(value)).join("\n") + "\n");
    });

    await t.test("inactive routine detail and transient masked Steward credentials are production UI", async () => {
      const routineStarted = { v:0, ts:new Date(frozenNow - 60_000).toISOString(),
        source:"steward", agent_id:"layout-routine", project:"quiet-project",
        type:"routine_started", payload:{routine:"daily-summary",run_id:"watchdog",trigger:"schedule"} };
      const routineFailed = { ...routineStarted, ts:new Date(frozenNow - 30_000).toISOString(),
        type:"routine_failed", payload:{routine:"daily-summary",run_id:"watchdog",
          error:"run never reported back"} };
      const routineFinished = { ...routineStarted, ts:routineFailed.ts,
        type:"routine_finished", payload:{routine:"daily-summary",run_id:"watchdog",
          outcome:"late finish",duration_s:30,artifacts:[]} };
      const delayedStarted = {...routineStarted};
      const olderStarted = { ...routineStarted, ts:new Date(frozenNow - 120_000).toISOString(),
        payload:{routine:"daily-summary",run_id:"older",trigger:"schedule"} };
      const olderFailed = { ...routineFailed, ts:new Date(frozenNow - 90_000).toISOString(),
        payload:{routine:"daily-summary",run_id:"older",error:"<img src=x onerror=window.__unsafe=1>"} };
      fs.appendFileSync(events, [olderStarted, olderFailed, routineStarted, routineFailed,
        routineFinished, delayedStarted]
        .map(value => JSON.stringify(value)).join("\n") + "\n");
      await resetPage();
      const evidence = await eventually(send, `(async () => {
        await runtime.refreshResidents();
        document.querySelector('#fleet-open').click();
        document.querySelector('[data-fleet-tab="routines"]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        const row = [...document.querySelectorAll('.ledger-entry')].find(item => item.textContent.includes('daily-summary'));
        if (!row) return false;
        row.querySelector('[data-agent]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        const routineVisibleState = document.querySelector('#panel-body').textContent.includes('routine failed');
        const linkedObservedOwner = document.querySelector('#panel-body').textContent.includes('layout-routine');
        const routineVisible = document.querySelector('#panel-body').textContent.includes('Routine ledger');
        // Opening the panel and the SSE/poll publication are independent. Keep
        // this whole scenario behind the production DOM's eventual owner fact
        // so slower Linux runners cannot capture an intermediate resident link.
        if (!routineVisibleState || !linkedObservedOwner || !routineVisible) return false;
        let promptCalls = 0; window.prompt = () => { promptCalls++; return null; };
        const firstCredentialRequest = requestStewardConfig();
        const secondCredentialRequest = requestStewardConfig();
        await new Promise(resolve => requestAnimationFrame(resolve));
        const dialog = document.querySelector('#steward-auth'), token = document.querySelector('#steward-token');
        const result = { routineVisibleState, linkedObservedOwner, routineVisible, open: dialog.open, type: token.type,
          autocomplete: token.autocomplete, promptCalls,
          singleFlight: firstCredentialRequest === secondCredentialRequest,
          secretInPanel: document.querySelector('#panel-body').textContent.includes('browser-secret') };
        token.value = 'ephemeral-secret';
        document.querySelector('#steward-auth button[value="cancel"]').click();
        result.cancelledTogether = (await Promise.all([firstCredentialRequest, secondCredentialRequest]))
          .every(value => value === null);
        await new Promise(resolve => requestAnimationFrame(resolve));
        result.closed = !dialog.open; result.cleared = token.value === '';
        window.fetch = async (url, options) => {
          window.__stewardFetch = { url: String(url), authorization: options.headers.Authorization,
            credentials: options.credentials };
          return { status: 200, json: async () => ({ routines: [{ resident: 'life-agent',
            routine: 'daily-summary', next_fire: '2026-08-26T07:00:00+02:00',
            enabled: true, retired: false }] }) };
        };
        document.querySelector('#fleet-open').click();
        document.querySelector('[data-fleet-tab="routines"]').click();
        document.querySelector('[data-steward-connect]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        token.value = 'fetch-only-secret';
        document.querySelector('#steward-auth button[value="connect"]').click();
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        result.directFetch = window.__stewardFetch;
        result.nextFireLoaded = document.querySelector('#panel-body').textContent.includes('declared by Steward, not observed');
        result.fetchTokenCleared = token.value === '';
        result.fetchTokenNotRendered = !document.documentElement.innerHTML.includes('fetch-only-secret');
        const routineRow = [...document.querySelectorAll('.ledger-entry')]
          .find(item => item.textContent.includes('daily-summary'));
        routineRow.querySelector('[data-agent]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        result.enabledRun = !document.querySelector('[data-run-routine]').disabled;
        result.unknownDuration = document.querySelector('#panel-body').textContent.includes('duration unknown');
        result.inspectableFailures = [...document.querySelectorAll('.routine-card summary')]
          .filter(summary => summary.textContent.startsWith('Inspect failure for ')).map(summary => summary.textContent);
        result.failureErrors = [...document.querySelectorAll('.routine-card .routine-error')].map(error => error.textContent);
        result.failureMarkupEscaped = !document.querySelector('.routine-card .routine-error img') && !window.__unsafe;
        window.fetch = async () => ({ status: 200, json: async () => ({ routines: [{
          resident: 'life-agent', routine: 'daily-summary', next_fire: null,
          enabled: false, retired: false }] }) });
        await refreshStewardDeclarations();
        result.disabledRun = document.querySelector('[data-run-routine]').disabled;
        window.fetch = async () => ({ status: 200, json: async () => ({ routines: [{
          resident: 'life-agent', routine: 'daily-summary', next_fire: null,
          enabled: true, retired: true }] }) });
        await refreshStewardDeclarations();
        result.retiredRun = document.querySelector('[data-run-routine]').disabled;
        let resolveOlder, resolveNewer, declarationCalls = 0;
        const olderResponse = new Promise(resolve => { resolveOlder = resolve; });
        const newerResponse = new Promise(resolve => { resolveNewer = resolve; });
        window.fetch = async () => (++declarationCalls === 1 ? olderResponse : newerResponse);
        const olderRefresh = refreshStewardDeclarations();
        const newerRefresh = refreshStewardDeclarations();
        resolveNewer({status:200,json:async()=>({routines:[{resident:'life-agent',
          routine:'daily-summary',next_fire:'2026-08-28T07:00:00Z',enabled:true,retired:false}]})});
        await newerRefresh;
        resolveOlder({status:200,json:async()=>({routines:[{resident:'life-agent',
          routine:'daily-summary',next_fire:null,enabled:false,retired:false}]})});
        await olderRefresh;
        result.latestDeclarationWins = stewardDeclarations.state === 'loaded' &&
          stewardDeclarations.byRoutine.get(JSON.stringify(['life-agent','daily-summary'])).enabled === true &&
          stewardDeclarations.byRoutine.get(JSON.stringify(['life-agent','daily-summary'])).next_fire ===
            '2026-08-28T07:00:00Z' &&
          !document.querySelector('[data-run-routine]').disabled;
        return result;
      })()`, signal, "inactive routine and Steward credential dialog");
      fs.writeFileSync(events, [currentEvent, agedEvent, agedAttention]
        .map(value => JSON.stringify(value)).join("\n") + "\n");
      assert.deepEqual(evidence, { routineVisibleState: true, linkedObservedOwner: true, routineVisible: true, open: true,
        type: "password", autocomplete: "off", promptCalls: 0, singleFlight: true,
        secretInPanel: false, cancelledTogether: true, closed: true, cleared: true,
        directFetch: { url: "http://127.0.0.1:8801/routines", authorization: "Bearer fetch-only-secret",
          credentials: "omit" }, nextFireLoaded: true, fetchTokenCleared: true,
        fetchTokenNotRendered: true, enabledRun: true, unknownDuration: true,
        inspectableFailures: ["Inspect failure for watchdog", "Inspect failure for older"],
        failureErrors: ["run never reported back", "<img src=x onerror=window.__unsafe=1>"],
        failureMarkupEscaped: true,
        disabledRun: true, retiredRun: true, latestDeclarationWins: true });
    });

    await t.test("resident identity panel renders strict remote states and visitor truth", async () => {
      await resetPage();
      const result = await eventually(send, `(async () => {
        await runtime.poll();
        await runtime.refreshResidents();
        const resident = fleetView.residents.find(item => item.file === 'routine.resident.json');
        const visitorResident = villagers.find(item => item.id === 'layout-resident');
        if (!resident || !visitorResident || visitorResident.residency !== 'visitor') return false;
        const charter = {mission:'Keep the quiet work moving.',duties:['Read the inbox'],
          rules:['Never invent a result'],escalation:{kind:'policy',when:['A person must decide'],
          how:'needs_human',note:null}};
        identityState = {status:'loaded',lastSuccessAt:${frozenNow},diagnostics:[],residents:new Map([
          [resident.file,{status:'configured',remoteId:'life-agent',charter,lastSuccessAt:${frozenNow},
            localFingerprint:BurrowIdentity.localFingerprint(resident),
            stale:false,journal:{status:'loaded',entries:[
              {date:'2025-01-15',routine:'close-of-day',text:'Newest note.'},
              {date:'2025-01-14',routine:'close-of-day',text:'Older note.'}],
              lastSuccessAt:${frozenNow},stale:false,diagnostic:null}}]])};
        stewardConfig={url:'http://127.0.0.1:8801',token:'identity-memory-only'};
        openPanel('@resident/' + resident.file);
        await new Promise(resolve => requestAnimationFrame(resolve));
        const body = document.querySelector('#panel-body');
        const configured = {mission:body.textContent.includes('Keep the quiet work moving.'),
          distinct:!!body.querySelector('.declared-identity') && !!body.querySelector('.journal-section'),
          order:[...body.querySelectorAll('.journal-entry time')].map(item=>item.dateTime).join(','),
          bound:body.textContent.includes('latest 7'),
          credentials:!!body.querySelector('[data-steward-change]') &&
            !!body.querySelector('[data-steward-clear]')};
        const refresh = body.querySelector('[data-identity-refresh]'); refresh.focus();
        renderPanel(Date.now());
        const focusPreserved = document.activeElement.matches('[data-identity-refresh]');
        const record = identityState.residents.get(resident.file);
        record.journal = {...record.journal,status:'unreachable',stale:true,
          diagnostic:'Steward journal unreachable: network down'};
        renderPanel(Date.now());
        const unreachable = body.textContent.includes('Steward unreachable') &&
          body.textContent.includes('Last successful fetch') && body.textContent.includes('Cached entries are stale');
        record.status='authentication'; record.stale=true;
        renderPanel(Date.now());
        const cachedAuthentication = body.textContent.includes('Steward rejected these credentials') &&
          body.textContent.includes('Cached declaration as of');
        record.status='error';
        renderPanel(Date.now());
        const charterCopy = body.querySelector('.declared-identity').textContent;
        const cachedDefinitive = charterCopy.includes('Steward definitively refused the latest charter read') &&
          charterCopy.includes('Cached declaration as of') && !charterCopy.includes('Steward unreachable');
        record.status='configured'; record.stale=false;
        record.journal = {status:'loaded',entries:[],lastSuccessAt:${frozenNow},stale:false,diagnostic:null};
        renderPanel(Date.now());
        const empty = body.textContent.includes('has written nothing yet');
        record.status='invalid'; record.charter=null; record.diagnostic='bad charter';
        record.journal={status:'malformed',entries:[{date:'2025-01-13',routine:'close-of-day',
          text:'Cached malformed note.'}],lastSuccessAt:${frozenNow},stale:true,diagnostic:'bad journal'};
        renderPanel(Date.now());
        const malformed = body.textContent.includes('Could not read charter') &&
          body.textContent.includes('Could not read journal') && body.textContent.includes('close-of-day');
        const local = {...resident};
        const remote = {residents:[{id:'life-agent',agent_id:null,project:'quiet-project',
          charter:{mission:'M',duties:['D'],rules:['R'],escalation:'ask'}}],errors:[]};
        const reply = (payload,status=200) => ({ok:status>=200&&status<300,status,
          json:async()=>payload});
        const statusFor = async (status,code) => {
          const state = await BurrowIdentity.refresh(BurrowIdentity.createState(),
            {url:'http://steward',token:'secret'},[local],async url=>url.endsWith('/residents') ?
              reply(remote) : reply({detail:{error:code,message:'exact '+code}},status),${frozenNow});
          return BurrowIdentity.recordFor(state,local).journal.status;
        };
        const contract = {authentication:await statusFor(401,'invalid_token'),
          absent:await statusFor(404,'unknown_resident'),
          unreadable:await statusFor(409,'journal_unreadable')};
        const exactRecord = identityState.residents.get(resident.file);
        contract.rotationQuarantined = BurrowIdentity.recordFor(identityState,
          {...resident,home:resident.home+1}) === null;
        contract.exactRemote = exactRecord.remoteId === 'life-agent';
        exactRecord.status='configured'; exactRecord.charter=charter;
        exactRecord.lastSuccessAt=${frozenNow}; exactRecord.stale=false;
        exactRecord.journal={status:'loaded',entries:[],lastSuccessAt:${frozenNow},
          stale:false,diagnostic:null,localFingerprint:exactRecord.localFingerprint,
          remoteId:exactRecord.remoteId};
        reconcileIdentityLocals([resident], false);
        const feedUnavailable = BurrowIdentity.recordFor(identityState,resident);
        contract.localFeedUnavailable = feedUnavailable.status === 'local-unavailable' &&
          feedUnavailable.stale === true && feedUnavailable.journal.status === 'local-unavailable' &&
          body.textContent.includes("Burrow's resident manifest feed is unavailable") &&
          body.textContent.includes('Cached declaration as of');
        const originalFetch = window.fetch;
        let gatedFetches = 0;
        window.fetch = async url => {
          gatedFetches += 1;
          const configuredRemote = {id:'life-agent',agent_id:resident.match.agent_id || null,
            project:resident.match.project || null,
            charter:{mission:'Recovered',duties:['Read'],rules:['Tell truth'],escalation:'ask'}};
          return String(url).endsWith('/residents') ? reply({residents:[configuredRemote],errors:[]}) :
            reply({resident:'life-agent',entries:[]});
        };
        fleetView = {...fleetView,directoryAvailable:false,residents:[resident]};
        await refreshIdentity();
        contract.manualRefreshGated = gatedFetches === 0 &&
          BurrowIdentity.recordFor(identityState,resident).status === 'local-unavailable';
        forgetStewardConfig();
        contract.credentialClearPreservesLocalUnavailable =
          BurrowIdentity.recordFor(identityState,resident).status === 'local-unavailable';
        stewardConfig={url:'http://127.0.0.1:8801',token:'replacement-identity-token'};
        fleetView = {...fleetView,directoryAvailable:true,residents:[resident]};
        await refreshIdentity();
        contract.recoversAfterLocalFeed = gatedFetches === 2 &&
          BurrowIdentity.recordFor(identityState,resident).status === 'configured';
        window.fetch = originalFetch;
        selectedId = null;
        const recoveredRotated = {...resident,home:resident.home+1};
        reconcileIdentityLocals([recoveredRotated], true);
        contract.recoveryQuarantinesUnseenRotation =
          BurrowIdentity.recordFor(identityState,recoveredRotated) === null;
        forgetStewardConfig();
        openPanel('layout-resident');
        await new Promise(resolve => requestAnimationFrame(resolve));
        const visitor = body.textContent.includes('Temporary lodging') &&
          body.textContent.includes('no resident soul or manifest') &&
          !body.querySelector('.declared-identity') && !body.querySelector('.journal-section');
        return {configured,focusPreserved,unreachable,cachedAuthentication,cachedDefinitive,
          empty,malformed,contract,visitor};
      })()`, signal, "resident identity states");
      assert.deepEqual(result, { configured: { mission: true, distinct: true,
        order: "2025-01-15,2025-01-14", bound: true, credentials: true }, focusPreserved: true,
        unreachable: true, cachedAuthentication: true, cachedDefinitive: true,
        empty: true, malformed: true,
        contract:{authentication:"authentication",absent:"missing",unreadable:"malformed",
          rotationQuarantined:true,exactRemote:true,localFeedUnavailable:true,
          manualRefreshGated:true,credentialClearPreservesLocalUnavailable:true,
          recoversAfterLocalFeed:true,
          recoveryQuarantinesUnseenRotation:true},visitor: true });
    });

    await t.test("job form stays non-optimistic until exact production event acknowledgement", async () => {
      await resetPage();
      await waitForTelemetry();
      const pending = await eventually(send, `(async () => {
        stewardConfig = {url:'http://127.0.0.1:8801',token:'job-only-secret'};
        const originalFetch = window.fetch.bind(window);
        window.__jobPost = null;
        window.fetch = async (url, options = {}) => {
          if (String(url) === 'http://127.0.0.1:8801/jobs') {
            window.__jobPost = {url:String(url),authorization:options.headers.Authorization,
              credentials:options.credentials,body:JSON.parse(options.body)};
            return {status:202,json:async()=>({status:'accepted',task_id:'exact-job',request_id:'request-job'})};
          }
          return originalFetch(url, options);
        };
        document.querySelector('[data-panel-target="job-board"]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        const form = document.querySelector('[data-post-job]');
        const set = (name, value) => { const input=form.elements[name]; input.value=value;
          input.dispatchEvent(new Event('input',{bubbles:true})); };
        set('title','Exact job'); set('detail','Only event truth');
        set('required_skills','research, write-journal');
        form.requestSubmit();
        await new Promise(resolve => setTimeout(resolve,20));
        const ack = jobAcks.latest();
        return {post:window.__jobPost,state:ack && ack.state,
          optimistic:document.querySelector('[data-task-id="exact-job"]') !== null,
          tokenInput:document.querySelector('#steward-token').value,
          tokenRendered:document.querySelector('#panel-body').textContent.includes('job-only-secret')};
      })()`, signal, "direct job POST pending acknowledgement");
      assert.deepEqual(pending, {post:{url:"http://127.0.0.1:8801/jobs",
        authorization:"Bearer job-only-secret",credentials:"omit",
        body:{title:"Exact job",detail:"Only event truth",required_skills:["research","write-journal"]}},
        state:"pending",optimistic:false,tokenInput:"",tokenRendered:false});

      const unrelated = {v:0,ts:new Date(frozenNow + 1).toISOString(),source:"steward",
        agent_id:"steward:api",project:"steward",type:"task_posted",
        payload:{task_id:"other-job",title:"Other",required_skills:[],posted_by:"api"}};
      fs.appendFileSync(events, JSON.stringify(unrelated) + "\n");
      const stillPending = await eventually(send, `(async () => {
        await runtime.poll();
        const ack=jobAcks.latest();
        return ack && {state:ack.state,other:!!document.querySelector('[data-task-id="other-job"]'),
          exact:!!document.querySelector('[data-task-id="exact-job"]')};
      })()`, signal, "unrelated job event does not acknowledge");
      assert.deepEqual(stillPending,{state:"pending",other:true,exact:false});

      const exact = {...unrelated,ts:new Date(frozenNow + 2).toISOString(),
        payload:{task_id:"exact-job",title:"Exact job",required_skills:["research","write-journal"],posted_by:"api"}};
      fs.appendFileSync(events, JSON.stringify(exact) + "\n");
      const acknowledged = await eventually(send, `(async () => {
        await runtime.poll();
        const ack=jobAcks.latest();
        return ack && {state:ack.state,exact:!!document.querySelector('[data-task-id="exact-job"]'),
          status:document.querySelector('.job-ack')?.textContent};
      })()`, signal, "exact task_posted production acknowledgement");
      assert.deepEqual(acknowledged,{state:"acknowledged",exact:true,
        status:"Posted — confirmed by the matching task_posted event."});
      fs.writeFileSync(events, [currentEvent, agedEvent, agedAttention]
        .map(value => JSON.stringify(value)).join("\n") + "\n");
    });

    await t.test("town hall nursery stays pending until the resident's first real event", async () => {
      await resetPage();
      await waitForTelemetry();
      const opened = await eventually(send, `(() => {
        document.querySelector('[data-panel-target="nursery"]').click();
        const form=document.querySelector('[data-create-resident]');
        if(!form)return false;
        const submit=form.querySelector('[type=submit]');submit.focus();
        const focused=document.activeElement===submit;
        form.requestSubmit(submit);
        return {spriteAuthority:BurrowNursery.CHARS===CHARS,
          frozen:Object.isFrozen(BurrowNursery.CHARS),focused};
      })()`, signal, "open production nursery");
      assert.deepEqual(opened,{spriteAuthority:true,frozen:true,focused:true});
      const invalid = await eventually(send, `(() => {
        const form=document.querySelector('[data-create-resident]');
        const expected=['name','role','mission','duties','rules','escalation'];
        const fields=expected.map(name=>{
          const input=form.elements[name],error=document.querySelector('#nursery-'+name+'-error');
          return {name,invalid:input.getAttribute('aria-invalid'),
            described:(input.getAttribute('aria-describedby')||'').split(/\\s+/).includes('nursery-'+name+'-error'),
            live:!!error&&error.textContent.length>0};
        });
        return {active:document.activeElement?.name||document.activeElement?.tagName,
          notice:document.querySelector('[role=alert]')?.textContent,fields};
      })()`, signal, "keyboard submit exposes and focuses complete local validation");
      assert.equal(invalid.active,"name");
      assert.equal(invalid.fields.length,6);
      assert.ok(invalid.fields.every(item=>item.invalid==='true'&&item.described&&item.live));
      await eventually(send, `(() => {
        const form=document.querySelector('[data-create-resident]');
        const values={name:'Quill Keeper',char:'Monk',accent:'#4f7ea6',role:'note keeper',
          mission:'Keep notes.',duties:'Tidy notes',rules:'Never delete',
          escalation:'Ask first',skills:'research',runner:'codex'};
        for(const [name,value] of Object.entries(values)){
          form.elements[name].value=value;
          form.elements[name].dispatchEvent(new Event('input',{bubbles:true}));
        }
        return !document.querySelector('.nursery-error') &&
          ![...form.elements].some(field=>field.getAttribute&&field.getAttribute('aria-invalid')==='true');
      })()`, signal, "open and populate production nursery");
      for (const name of ["name", "role", "skills"]) {
        const preserved = await eventually(send, `(() => {
          const input=document.querySelector('[data-create-resident] [name="${name}"]');
          window.__nurseryFocusNode=input;
          input.focus(); input.setSelectionRange(1,Math.min(4,input.value.length),'forward');
          // Runtime event and tracker callbacks converge on this exact
          // production render seam. Keep this assertion synchronous so no
          // fixture poll can race the focus check.
          renderPanel(Date.now());
          const replacement=document.querySelector('[data-create-resident] [name="${name}"]');
          return {changed:replacement!==window.__nurseryFocusNode,active:document.activeElement===replacement,
            start:replacement.selectionStart,end:replacement.selectionEnd,expectedEnd:Math.min(4,replacement.value.length),
            activeName:document.activeElement?.getAttribute('name')||document.activeElement?.tagName};
        })()`, signal, `nursery ${name} caret survives production rerender`);
        assert.deepEqual(preserved,{changed:true,active:true,start:1,end:4,
          expectedEnd:4,activeName:name});
      }
      const before = await eventually(send, `(async () => {
        let form=document.querySelector('[data-create-resident]');
        form.elements.mission.focus(); form.elements.mission.setSelectionRange(2,5,'forward');
        renderPanel(Date.now());
        form=document.querySelector('[data-create-resident]');
        const focused=form.elements.mission;
        window.__nurseryFocusStable=document.activeElement===focused && focused.selectionStart===2 && focused.selectionEnd===5;
        const originalFetch=window.fetch;
        window.__nurseryOriginalFetch=originalFetch;
        window.fetch=async (url,options) => {
          if(String(url)==='http://steward.test/residents') return {status:201,ok:true,
            async json(){return {status:'accepted',request_id:'nursery-request',id:'quill-keeper',
              changed:true,message:'accepted',declare:{written:true},register:{ok:true,problems:[]}};}};
          return originalFetch(url,options);
        };
        stewardConfig={url:'http://steward.test',token:'memory-only'};
        form.requestSubmit();
        await new Promise(resolve=>setTimeout(resolve,0));
        const item=nurseryTracker.latest();
        return item && {state:item.state,agent:item.agent_id,
          feedback:document.querySelector('.nursery-feedback')?.textContent,
          projected:villagers.some(v=>v.id==='codex:quill-keeper'),
          auth:document.querySelector('#steward-token').value,focusStable:window.__nurseryFocusStable};
      })()`, signal, "nursery pending acceptance");
      assert.deepEqual(before, {state:"pending",agent:"codex:quill-keeper",
        feedback:"pending · Steward accepted; waiting for Quill Keeper to wake.",
        projected:false,auth:"",focusStable:true});
      fs.appendFileSync(events, JSON.stringify({v:0,ts:new Date(frozenNow + 1).toISOString(),
        source:"codex",agent_id:"codex:quill-keeper",project:"notes",type:"task_started",
        payload:{prompt:"Wake honestly"}}) + "\n");
      const alive = await eventually(send, `(async () => {
        window.fetch=window.__nurseryOriginalFetch;
        await runtime.poll();
        const item=nurseryTracker.latest();
        return item && {state:item.state,
          feedback:document.querySelector('.nursery-feedback')?.textContent,
          residency:villagers.find(v=>v.id==='codex:quill-keeper')?.residency};
      })()`, signal, "nursery first-event acknowledgement");
      assert.deepEqual(alive, {state:"alive",
        feedback:"alive · Quill Keeper woke — confirmed by their first real event.",
        residency:"visitor"});

      const submitPreflight = await eventually(send, `(async () => {
        const form=document.querySelector('[data-create-resident]');
        form.elements.name.value='Offline Keeper';
        form.elements.name.dispatchEvent(new Event('input',{bubbles:true}));
        const prior=nurseryTracker.latest(),originalSnapshot=runtime.snapshot;
        window.__nurseryOriginalSnapshot=originalSnapshot;
        let residentPosts=0;
        const originalFetch=window.fetch;
        window.__nurseryPreflightFetch=originalFetch;
        window.fetch=async(url,options={})=>{
          if(String(url)==='http://steward.test/residents')residentPosts++;
          return originalFetch(url,options);
        };
        runtime.snapshot=()=>({...originalSnapshot(),transport:'disconnected'});
        const submit=form.querySelector('[data-nursery-submit]');submit.focus();
        form.requestSubmit(submit);
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const notice=document.querySelector('[data-nursery-notice]');
        const retained=nurseryTracker.latest();
        return {residentPosts,role:notice?.getAttribute('role'),notice:notice?.textContent,
          sameItem:retained===prior,state:retained?.state,
          priorFeedback:document.querySelector('.nursery-feedback')?.textContent,
          submitFocused:document.activeElement===document.querySelector('[data-nursery-submit]'),
          inputEnabled:!document.querySelector('[name=name]').disabled};
      })()`, signal, "new nursery submit exposes telemetry preflight over prior history");
      assert.deepEqual(submitPreflight,{residentPosts:0,role:"alert",
        notice:"Nursery unavailable: telemetry is disconnected; no request was sent",
        sameItem:true,state:"alive",
        priorFeedback:"alive · Quill Keeper woke — confirmed by their first real event.",
        submitFocused:true,inputEnabled:true});
      await eventually(send, `(() => {
        runtime.snapshot=window.__nurseryOriginalSnapshot;
        window.fetch=window.__nurseryPreflightFetch;
        return runtime.snapshot().transport!=='disconnected';
      })()`, signal, "restore nursery telemetry after current submit notice");

      const preparationFailure = await eventually(send, `(async () => {
        const prior=nurseryTracker.latest(),originalBegin=nurseryTracker.begin;
        nurseryTracker.begin=()=>{throw new Error('<img src=x onerror=alert(1)>');};
        const submit=document.querySelector('[data-nursery-submit]');submit.focus();
        document.querySelector('[data-create-resident]').requestSubmit(submit);
        await new Promise(resolve=>requestAnimationFrame(resolve));
        nurseryTracker.begin=originalBegin;
        const notice=document.querySelector('[data-nursery-notice]');
        return {role:notice?.getAttribute('role'),text:notice?.textContent,
          injected:notice?.querySelectorAll('img').length,sameItem:nurseryTracker.latest()===prior,
          state:nurseryTracker.latest()?.state,
          submitFocused:document.activeElement===document.querySelector('[data-nursery-submit]')};
      })()`, signal, "nursery request preparation failure remains escaped over prior history");
      assert.deepEqual(preparationFailure,{role:"alert",
        text:"Nursery request could not be prepared: <img src=x onerror=alert(1)>; no request was sent.",
        injected:0,sameItem:true,state:"alive",submitFocused:true});

      const cancelledCredentials = await eventually(send, `(async () => {
        const prior=nurseryTracker.latest();stewardConfig=null;
        const submit=document.querySelector('[data-nursery-submit]');submit.focus();
        document.querySelector('[data-create-resident]').requestSubmit(submit);
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const dialog=document.querySelector('#steward-auth');
        if(!dialog.open)return false;
        dialog.querySelector('button[value=cancel]').click();
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const notice=document.querySelector('[data-nursery-notice]');
        const result={role:notice?.getAttribute('role'),text:notice?.textContent,
          dialogOpen:dialog.open,sameItem:nurseryTracker.latest()===prior,
          state:nurseryTracker.latest()?.state,
          submitFocused:document.activeElement===document.querySelector('[data-nursery-submit]')};
        stewardConfig={url:'http://steward.test',token:'memory-only'};
        return result;
      })()`, signal, "cancelled nursery credentials remain visible over prior history");
      assert.deepEqual(cancelledCredentials,{role:"alert",
        text:"Steward credentials were not provided; no resident request was sent.",
        dialogOpen:false,sameItem:true,state:"alive",submitFocused:true});

      const ambiguous = await eventually(send, `(async () => {
        const form=document.querySelector('[data-create-resident]');
        form.elements.name.value='Retry Keeper';
        form.elements.name.dispatchEvent(new Event('input',{bubbles:true}));
        form.elements.mission.focus(); form.elements.mission.setSelectionRange(1,4,'forward');
        window.__nurseryRetryBodies=[];
        const original=window.fetch.bind(window); window.__nurseryBaseFetch=original;
        window.fetch=async(url,options={})=>{
          if(String(url)==='http://steward.test/residents'){
            window.__nurseryRetryBodies.push(JSON.parse(options.body));
            throw new TypeError('connection disappeared after write');
          }
          return original(url,options);
        };
        form.requestSubmit(); await new Promise(resolve=>setTimeout(resolve,0));
        const item=nurseryTracker.latest(),retry=document.querySelector('[data-nursery-retry]');
        return item && {state:item.state,agent:item.agent_id,retry:!!retry,
          retryFocused:document.activeElement===retry,disabled:document.querySelector('[name=name]').disabled,
          body:window.__nurseryRetryBodies[0],projected:villagers.some(v=>v.id==='codex:retry-keeper')};
      })()`, signal, "nursery ambiguous delivery offers immutable retry");
      assert.equal(ambiguous.state,"unreachable");
      assert.equal(ambiguous.agent,"codex:retry-keeper");
      assert.equal(ambiguous.retry,true); assert.equal(ambiguous.retryFocused,true);
      assert.equal(ambiguous.disabled,true); assert.equal(ambiguous.projected,false);
      assert.equal(ambiguous.body.name,"Retry Keeper");

      const retryPreflight = await eventually(send, `(async () => {
        const prior=nurseryTracker.latest(),originalSnapshot=runtime.snapshot;
        window.__nurseryRetrySnapshot=originalSnapshot;
        const stable=originalSnapshot(),parts=stable.cursor.split(':');
        parts[parts.length-1]=String(Number(parts[parts.length-1])+1);
        let reads=0;
        runtime.snapshot=()=>reads++===0?stable:{...stable,cursor:parts.join(':')};
        const beforeBodies=window.__nurseryRetryBodies.length;
        const retry=document.querySelector('[data-nursery-retry]');retry.focus();retry.click();
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const notice=document.querySelector('[data-nursery-notice]');
        const retained=nurseryTracker.latest(),currentRetry=document.querySelector('[data-nursery-retry]');
        return {requestDelta:window.__nurseryRetryBodies.length-beforeBodies,
          role:notice?.getAttribute('role'),notice:notice?.textContent,sameItem:retained===prior,
          state:retained?.state,priorMessage:document.querySelector('.nursery-feedback')?.textContent,
          retryText:currentRetry?.textContent,retryDescribed:currentRetry?.getAttribute('aria-describedby'),
          retryFocused:document.activeElement===currentRetry,fieldsDisabled:document.querySelector('[name=name]').disabled};
      })()`, signal, "ambiguous nursery retry exposes changed-cursor preflight over prior truth");
      assert.deepEqual(retryPreflight,{requestDelta:0,role:"alert",
        notice:"Telemetry changed while authorizing; the original declaration was not retried.",
        sameItem:true,state:"unreachable",
        priorMessage:"unreachable · Steward is unreachable. The request outcome is unknown; retry is available only with the exact original declaration. check current telemetry and retry exact original declaration",
        retryText:"check current telemetry and retry exact original declaration",
        retryDescribed:"nursery-current-notice",retryFocused:true,fieldsDisabled:true});
      await eventually(send, `(() => {
        runtime.snapshot=window.__nurseryRetrySnapshot;
        return document.querySelector('[data-nursery-retry]')?.textContent.includes('check current telemetry');
      })()`, signal, "restore telemetry while retaining current retry notice");

      const converged = await eventually(send, `(async () => {
        const disabled=document.querySelector('[name=name]');
        disabled.value='Edited After Ambiguity';
        disabled.dispatchEvent(new Event('input',{bubbles:true}));
        const original=window.__nurseryBaseFetch;
        window.fetch=async(url,options={})=>{
          if(String(url)==='http://steward.test/residents'){
            window.__nurseryRetryBodies.push(JSON.parse(options.body));
            return {status:201,ok:true,async json(){return {status:'accepted',request_id:'retry-replay',
              id:'retry-keeper',changed:true,message:'declaration unchanged; provisioning converged',
              declare:{written:false},register:{ok:true,problems:[]}};}};
          }
          return original(url,options);
        };
        document.querySelector('[data-nursery-retry]').click();
        await new Promise(resolve=>setTimeout(resolve,0));
        const item=nurseryTracker.latest();
        if(!item||item.state!=='converged')return false;
        return {state:item.state,bodies:window.__nurseryRetryBodies,
          feedback:document.querySelector('.nursery-feedback')?.textContent,
          projected:villagers.some(v=>v.id==='codex:retry-keeper'),
          overflow:document.querySelector('#panel').scrollWidth-document.querySelector('#panel').clientWidth};
      })()`, signal, "lost response converges on exact-body retry");
      assert.equal(converged.state,"converged");
      assert.deepEqual(converged.bodies[1],converged.bodies[0],"retry wire body is byte-shape immutable");
      assert.match(converged.feedback,/no new wake is claimed/);
      assert.equal(converged.projected,false); assert.ok(converged.overflow<=0);

      const validationDetail=[{type:'extra_forbidden',loc:['body','api_key'],
        msg:'Extra inputs are not permitted',input:'<secret>'}];
      const rejected = await eventually(send, `(async () => {
        const form=document.querySelector('[data-create-resident]');
        form.elements.name.value='Validation Keeper';
        form.elements.name.dispatchEvent(new Event('input',{bubbles:true}));
        const original=window.__nurseryBaseFetch,detail=${JSON.stringify(validationDetail)};
        window.fetch=async(url,options={})=>String(url)==='http://steward.test/residents'?
          {status:422,ok:false,async json(){return {detail};}}:original(url,options);
        form.requestSubmit(); await new Promise(resolve=>setTimeout(resolve,0));
        const item=nurseryTracker.latest(); if(!item||item.state!=='validation')return false;
        const feedback=document.querySelector('.nursery-feedback');
        return {state:item.state,text:feedback.textContent,children:feedback.querySelectorAll('script').length,
          disabled:document.querySelector('[name=name]').disabled,
          projected:villagers.some(v=>v.id==='codex:validation-keeper')};
      })()`, signal, "FastAPI 422 detail is safe and exact in production UI");
      assert.equal(rejected.state,"validation");
      assert.ok(rejected.text.includes(JSON.stringify(validationDetail)));
      assert.equal(rejected.children,0); assert.equal(rejected.disabled,false);
      assert.equal(rejected.projected,false);

      const uncertainHttp = await eventually(send, `(async () => {
        const form=document.querySelector('[data-create-resident]');
        form.elements.name.value='Uncertain Keeper';
        form.elements.name.dispatchEvent(new Event('input',{bubbles:true}));
        const original=window.__nurseryBaseFetch;window.__nursery503Bodies=[];
        window.fetch=async(url,options={})=>{
          if(String(url)==='http://steward.test/residents'){
            window.__nursery503Bodies.push(JSON.parse(options.body));
            return {status:503,ok:false,async json(){return {detail:{
              error:'overloaded',message:'temporary maintenance'}};}};
          }
          return original(url,options);
        };
        form.requestSubmit();await new Promise(resolve=>setTimeout(resolve,0));
        const item=nurseryTracker.latest(),feedback=document.querySelector('.nursery-feedback');
        if(!item||item.state!=='ambiguous')return false;
        return {state:item.state,role:feedback.getAttribute('role'),text:feedback.textContent,
          rejected:/Steward rejected/i.test(feedback.textContent),retry:!!feedback.querySelector('[data-nursery-retry]'),
          disabled:document.querySelector('[name=name]').disabled,
          projected:villagers.some(v=>v.id==='codex:uncertain-keeper'),posts:window.__nursery503Bodies.length};
      })()`, signal, "503 creation response stays explicitly outcome-unknown in production UI");
      assert.equal(uncertainHttp.state,"ambiguous"); assert.equal(uncertainHttp.role,"alert");
      assert.match(uncertainHttp.text,/temporary maintenance/);
      assert.match(uncertainHttp.text,/may have been created/);
      assert.match(uncertainHttp.text,/outcome is unknown/);
      assert.match(uncertainHttp.text,/exact original declaration/);
      assert.equal(uncertainHttp.rejected,false); assert.equal(uncertainHttp.retry,true);
      assert.equal(uncertainHttp.disabled,true); assert.equal(uncertainHttp.projected,false);
      assert.equal(uncertainHttp.posts,1);
      const uncertainRejected = await eventually(send, `(async () => {
        const disabled=document.querySelector('[name=name]');
        disabled.value='Mutated After 503';
        disabled.dispatchEvent(new Event('input',{bubbles:true}));
        const original=window.__nurseryBaseFetch;
        window.fetch=async(url,options={})=>{
          if(String(url)==='http://steward.test/residents'){
            window.__nursery503Bodies.push(JSON.parse(options.body));
            return {status:409,ok:false,async json(){return {detail:{
              error:'resident_not_declared',message:'resident differs exactly'}};}};
          }
          return original(url,options);
        };
        document.querySelector('[data-nursery-retry]').click();
        await new Promise(resolve=>setTimeout(resolve,0));
        const item=nurseryTracker.latest();if(!item||item.state!=='rejected')return false;
        return {state:item.state,bodies:window.__nursery503Bodies,
          text:document.querySelector('.nursery-feedback')?.textContent,
          enabled:!document.querySelector('[name=name]').disabled,
          projected:villagers.some(v=>v.id==='codex:uncertain-keeper')};
      })()`, signal, "503 retry sends only the immutable original declaration");
      assert.equal(uncertainRejected.state,"rejected");
      assert.deepEqual(uncertainRejected.bodies[1],uncertainRejected.bodies[0]);
      assert.match(uncertainRejected.text,/resident differs exactly/);
      assert.equal(uncertainRejected.enabled,true);assert.equal(uncertainRejected.projected,false);

      const deployment = await eventually(send, `(async () => {
        const form=document.querySelector('[data-create-resident]');
        form.elements.name.value='Broken Keeper';
        form.elements.name.dispatchEvent(new Event('input',{bubbles:true}));
        const original=window.__nurseryBaseFetch;
        window.fetch=async(url,options={})=>String(url)==='http://steward.test/residents'?
          {status:201,ok:true,async json(){return {status:'accepted',request_id:'broken-request',
            id:'broken-keeper',changed:true,declare:{written:true},
            message:'the container is up, but the schedule check did not pass — see register.problems',
            register:{ok:false,problems:['runner binary not found: codex']}};}}:original(url,options);
        form.requestSubmit(); await new Promise(resolve=>setTimeout(resolve,0));
        const item=nurseryTracker.latest(); if(!item||item.state!=='deployment-failed')return false;
        window.fetch=original;
        return {state:item.state,text:document.querySelector('.nursery-feedback').textContent,
          blocked:nurseryTracker.blocks(),projected:villagers.some(v=>v.id==='codex:broken-keeper')};
      })()`, signal, "deployment failure is immediate and non-optimistic");
      assert.equal(deployment.state,"deployment-failed");
      assert.match(deployment.text,/runner binary not found: codex/);
      assert.equal(deployment.blocked,false); assert.equal(deployment.projected,false);

      const resetBusy = await eventually(send, `(async () => {
        const form=document.querySelector('[data-create-resident]');
        form.elements.name.value='Reset Keeper';
        form.elements.name.dispatchEvent(new Event('input',{bubbles:true}));
        const original=window.__nurseryBaseFetch; window.__nurseryResetOriginal=original;
        let resolveRequest; window.__nurseryResetResponse=new Promise(resolve=>{resolveRequest=resolve;});
        window.__resolveNurseryReset=resolveRequest;
        window.fetch=async(url,options={})=>String(url)==='http://steward.test/residents'?
          window.__nurseryResetResponse:original(url,options);
        form.requestSubmit(); await new Promise(resolve=>setTimeout(resolve,0));
        const item=nurseryTracker.latest(); if(!item||!item.httpPending)return false;
        nurseryTracker.observe({reset:true,
          cursor:'v1:abcdefabcdefabcdefabcdefabcdefab:9:8:7:20',events:[]},validateEvent);
        const rendered=document.querySelector('[data-create-resident]');
        return {state:item.state,httpPending:item.httpPending,busy:nurseryTracker.busy(),
          ariaBusy:rendered.getAttribute('aria-busy'),disabled:rendered.elements.name.disabled};
      })()`, signal, "reset during nursery HTTP remains accessibly busy");
      assert.deepEqual(resetBusy,{state:"ambiguous",httpPending:true,busy:true,
        ariaBusy:"true",disabled:true});
      const resetSettled = await eventually(send, `(async () => {
        window.__resolveNurseryReset({status:409,ok:false,async json(){return {detail:{
          error:'resident_not_declared',message:'reset test refused'}};}});
        await new Promise(resolve=>setTimeout(resolve,0));
        window.fetch=window.__nurseryResetOriginal;
        const item=nurseryTracker.latest();
        return item && item.state==='rejected' && !item.httpPending && !nurseryTracker.busy();
      })()`, signal, "reset nursery request settles definitively");
      assert.equal(resetSettled,true);

      const capacity = await eventually(send, `(async () => {
        nurseryTracker.entries.clear();
        const snapshot=runtime.snapshot(),created=[];
        for(let index=0;index<BurrowNursery.MAX_TRACKED;index++){
          const tracked=nurseryTracker.begin(snapshot.cursor,{...nurseryDraft,
            name:'Capacity Keeper '+index});
          if(!tracked.ok)return false;
          tracked.item.state='silent';tracked.item.httpPending=false;
          tracked.item.message=tracked.item.name+' is still waiting for exact wake evidence.';
          created.push(tracked.item);
        }
        renderPanel(Date.now());
        let posts=0;const original=window.__nurseryBaseFetch;
        window.fetch=async(url,options={})=>{
          if(String(url)==='http://steward.test/residents')posts++;
          return original(url,options);
        };
        const form=document.querySelector('[data-create-resident]');
        form.elements.name.value='Capacity Ninth';
        form.elements.name.dispatchEvent(new Event('input',{bubbles:true}));
        const submit=document.querySelector('[data-nursery-submit]');submit.focus();
        document.querySelector('[data-create-resident]').requestSubmit(submit);
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const notice=document.querySelector('[data-nursery-notice]');
        window.fetch=original;
        return {posts,role:notice?.getAttribute('role'),text:notice?.textContent,
          size:nurseryTracker.entries.size,same:created.every(item=>nurseryTracker.entries.get(item.key)===item),
          noNinth:![...nurseryTracker.entries.values()].some(item=>item.name==='Capacity Ninth'),
          fieldsEnabled:!document.querySelector('[name=name]').disabled,
          submitFocused:document.activeElement===document.querySelector('[data-nursery-submit]')};
      })()`, signal, "all-reconcilable nursery capacity refuses accessibly without a POST");
      assert.equal(capacity.posts,0);assert.equal(capacity.role,"alert");
      assert.match(capacity.text,/all 8 tracked declarations still await exact wake evidence/);
      assert.match(capacity.text,/no request was sent/);
      assert.equal(capacity.size,8);assert.equal(capacity.same,true);assert.equal(capacity.noNinth,true);
      assert.equal(capacity.fieldsEnabled,true);assert.equal(capacity.submitFocused,true);
    });

    await t.test("structured approval waits for exact production closing evidence", async () => {
      const approval = {v:0,ts:new Date(frozenNow).toISOString(),source:"codex",
        agent_id:"codex:approval",project:"life",type:"needs_human",payload:{
          message:"May I send the note?",request_id:"approval-1",action:"send_email",
          detail:{to:"anna@example.com",subject:"Thursday"},
          options:["approve","approve"]}};
      const intervening={...approval,ts:new Date(frozenNow+1).toISOString(),
        type:"tool_called",payload:{tool:"Read"}};
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention,approval,intervening]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
      await resetPage();
      await waitForTelemetry();
      const pending = await eventually(send, `(async()=>{
        stewardConfig={url:'http://127.0.0.1:8801',token:'approval-only-secret'};
        const originalFetch=window.fetch.bind(window);window.__approvalPost=null;
        window.fetch=async(url,options={})=>{
          if(String(url)==='http://127.0.0.1:8801/approvals/approval-1'){
            window.__approvalPost={url:String(url),authorization:options.headers.Authorization,
              credentials:options.credentials,body:JSON.parse(options.body)};
            return {status:202,json:async()=>({request_id:'approval-1',status:'recorded',decision:'approve'})};
          }
          return originalFetch(url,options);
        };
        openPanel('codex:approval');await new Promise(resolve=>requestAnimationFrame(resolve));
        const before={title:document.querySelector('.approval-card h3')?.textContent,
          detail:document.querySelector('.approval-detail')?.textContent,
          options:[...document.querySelectorAll('[data-approval-option]')].map(item=>item.textContent),
          indexes:[...document.querySelectorAll('[data-approval-option]')].map(item=>item.dataset.approvalIndex),
          diagnostics:document.querySelectorAll('.approval-diagnostic').length,
          state:villagers.find(v=>v.id==='codex:approval')?.state};
        const repeated=[...document.querySelectorAll('[data-approval-option="approve"]')];
        repeated[1].focus();renderChrome(${frozenNow+1000});
        const focusedIndex=document.activeElement?.dataset?.approvalIndex;
        document.activeElement.click();
        while(!window.__approvalPost)await new Promise(resolve=>setTimeout(resolve,5));
        await new Promise(resolve=>setTimeout(resolve,20));
        return {before,focusedIndex,post:window.__approvalPost,state:approvalAcks.get('approval-1').state,
          stillKnocking:villagers.find(v=>v.id==='codex:approval')?.state,
          disabled:[...document.querySelectorAll('[data-approval-option]')].every(item=>item.disabled),
          pendingText:document.querySelector('.approval-feedback')?.textContent,
          tokenInput:document.querySelector('#steward-token').value,
          tokenRendered:document.querySelector('#panel-body').textContent.includes('approval-only-secret')};
      })()`,signal,"production structured approval pending");
      assert.deepEqual(pending,{before:{title:"send_email",
        detail:'{\n  "to": "anna@example.com",\n  "subject": "Thursday"\n}',
        options:["approve","approve"],indexes:["0","1"],diagnostics:0,state:"knocking"},
        focusedIndex:"1",
        post:{url:"http://127.0.0.1:8801/approvals/approval-1",
          authorization:"Bearer approval-only-secret",credentials:"omit",body:{decision:"approve"}},
        state:"pending",stillKnocking:"knocking",disabled:true,
        pendingText:"Steward recorded the decision; waiting for the exact closing event…",
        tokenInput:"",tokenRendered:false});

      const orphan={...approval,ts:new Date(frozenNow+2).toISOString(),source:"steward",
        agent_id:"codex:nobody",type:"needs_human_resolved",payload:{request_id:"other",
          decision:"deny",decided_by:"api",action:"send_email"}};
      fs.appendFileSync(events,JSON.stringify(orphan)+"\n");
      const unrelated=await eventually(send,`(async()=>{await runtime.poll();return {
        ack:approvalAcks.get('approval-1').state,
        villager:villagers.find(v=>v.id==='codex:approval')?.state};})()`,signal,
        "orphan decision does not close production approval");
      assert.deepEqual(unrelated,{ack:"pending",villager:"knocking"});

      const closing={...orphan,ts:new Date(frozenNow+3).toISOString(),agent_id:"codex:approval",
        payload:{request_id:"approval-1",decision:"approve",decided_by:"api",action:"send_email"}};
      fs.appendFileSync(events,JSON.stringify(closing)+"\n");
      const closed=await eventually(send,`(async()=>{await runtime.poll();const ack=approvalAcks.get('approval-1');
        return {ack:ack.state,villager:villagers.find(v=>v.id==='codex:approval')?.state,
          confirmed:document.querySelector('[data-approval-confirmation="approval-1"]')?.textContent
            .includes('Decision approve — confirmed by the exact closing event.')||false};})()`,signal,
        "exact production approval closing event");
      assert.deepEqual(closed,{ack:"acknowledged",villager:"resting",confirmed:true});
      const resetTruth=await eventually(send,`(()=>{
        const original=fleetView.approvalState.requests.get('approval-1');
        const state=record=>({requests:new Map(record?[['approval-1',record]]:[])});
        const resetCursor='v1:fedcba9876543210fedcba9876543210:1:2:3:30';
        approvalAcks.observe({cursor:resetCursor,events:[],reset:true,
          approvalState:state(original)});
        const same=approvalAcks.get('approval-1').state;
        const cases=[
          ['pending',state({...original,resolution:null})],
          ['missing',state(null)],
          ['collided',state({...original,collided:true})],
          ['different',state({...original,resolution:{...original.resolution,
            payload:{...original.resolution.payload,decision:'deny'}}})],
        ];
        const invalid=cases.map(([name,approvalState])=>{
          approvalAcks.observe({cursor:resetCursor,events:[],reset:true,approvalState});
          const item=approvalAcks.get('approval-1');
          const holder=document.createElement('div');holder.innerHTML=approvalFeedback(original);
          return {name,state:item.state,blocked:approvalAcks.blocks('approval-1'),
            role:holder.firstElementChild?.getAttribute('role'),text:holder.textContent};
        });
        approvalAcks.observe({cursor:'v1:fedcba9876543210fedcba9876543210:1:2:3:31',
          events:[original.resolution],approvalState:state(original)});
        return {same,invalid,recovered:approvalAcks.get('approval-1').state};
      })()`,signal,"production approval reset fingerprint UI");
      assert.deepEqual(resetTruth,{same:"acknowledged",invalid:[
        {name:"pending",state:"indeterminate",blocked:true,role:"alert",
          text:"Telemetry reset and the exact lifecycle is pending; retry is disabled until the exact authoritative close is observed again."},
        {name:"missing",state:"indeterminate",blocked:true,role:"alert",
          text:"Telemetry reset and the exact lifecycle is missing or collided; retry is disabled until the exact authoritative close is observed again."},
        {name:"collided",state:"indeterminate",blocked:true,role:"alert",
          text:"Telemetry reset and the exact lifecycle is missing or collided; retry is disabled until the exact authoritative close is observed again."},
        {name:"different",state:"indeterminate",blocked:true,role:"alert",
          text:"Telemetry reset and the replay selected a different closing decision; retry is disabled until the exact authoritative close is observed again."},
      ],recovered:"acknowledged"});
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
    });

    await t.test("parked approval decisions remain globally visible without a villager", async () => {
      const request={v:0,ts:new Date(frozenNow).toISOString(),source:"codex",
        agent_id:"codex:parked-approval",project:"life",type:"needs_human",payload:{
          message:"May I publish the parked note?",request_id:"parked-visible",
          action:"publish_note",detail:{note:"exact parked detail"},options:["approve","deny"]}};
      const ended={...request,ts:new Date(frozenNow+1).toISOString(),type:"session_ended",payload:{}};
      const closing={...request,ts:new Date(frozenNow+2).toISOString(),source:"steward",
        type:"needs_human_resolved",payload:{request_id:"parked-visible",decision:"deny",
          decided_by:"api",action:"publish_note"}};
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention,request,ended]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
      await resetPage();await waitForTelemetry();
      const pending=await eventually(send,`(()=>({
        state:villagers.find(v=>v.id==='codex:parked-approval')?.state,
        global:!!document.querySelector('[data-global-approval-confirmation="parked-visible"]')}))()`,
      signal,"parked approval remains pending at the door");
      assert.deepEqual(pending,{state:"knocking",global:false});

      fs.appendFileSync(events,JSON.stringify(closing)+"\n");
      const visible=await eventually(send,`(async()=>{
        openPanel(FLEET_LEDGER_ID);fleetTab='attention';renderFleet(${frozenNow});
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const tab=document.querySelector('[data-fleet-tab="attention"]');tab.focus();
        await runtime.poll();
        const card=document.querySelector('[data-global-approval-confirmation="parked-visible"]');
        return !villagers.some(v=>v.id==='codex:parked-approval')&&card&&{
          villagerAbsent:true,focus:document.activeElement?.dataset?.fleetTab,
          region:card.closest('section')?.getAttribute('aria-label'),
          action:card.querySelector('h3')?.textContent,detail:card.querySelector('pre')?.textContent,
          decision:card.querySelector('[role="status"]')?.textContent,
          actionable:card.querySelectorAll('button,textarea,input,select').length};
      })()`,signal,"parked approval exact decision in global production UI");
      assert.deepEqual(visible,{villagerAbsent:true,focus:"attention",
        region:"Recent confirmed approval decisions",action:"publish_note",
        detail:'{\n  "note": "exact parked detail"\n}',
        decision:"Decision deny — confirmed by the exact closing event.",actionable:0});

      await resetPage();await waitForTelemetry();
      const replayed=await eventually(send,`(()=>{
        openPanel(FLEET_LEDGER_ID);fleetTab='attention';renderFleet(${frozenNow});
        const card=document.querySelector('[data-global-approval-confirmation="parked-visible"]');
        return !villagers.some(v=>v.id==='codex:parked-approval')&&card&&{
          absent:true,decision:card.querySelector('[role="status"]')?.textContent};
      })()`,signal,"parked approval bootstrap/reset replay parity");
      assert.deepEqual(replayed,{absent:true,
        decision:"Decision deny — confirmed by the exact closing event."});

      const bounded=[];
      for(let index=0;index<approvals.MAX_CONFIRMATIONS+2;index++){
        const id=`parked-bound-${index}`,agent=`codex:parked-bound-${index}`;
        const knock={...request,ts:new Date(frozenNow+index*3).toISOString(),agent_id:agent,
          payload:{...request.payload,request_id:id,detail:{index}}};
        bounded.push(knock,{...knock,ts:new Date(frozenNow+index*3+1).toISOString(),
          type:"session_ended",payload:{}},{...closing,
          ts:new Date(frozenNow+index*3+2).toISOString(),agent_id:agent,
          payload:{...closing.payload,request_id:id,decision:index%2?"deny":"approve"}});
      }
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention,...bounded]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
      await resetPage();await waitForTelemetry();
      const capped=await eventually(send,`(()=>{
        openPanel(FLEET_LEDGER_ID);fleetTab='attention';renderFleet(${frozenNow});
        return [...document.querySelectorAll('[data-global-approval-confirmation]')]
          .map(card=>card.dataset.globalApprovalConfirmation);
      })()`,signal,"bounded global confirmation retention");
      assert.deepEqual(capped,Array.from({length:approvals.MAX_CONFIRMATIONS},(_,offset)=>
        `parked-bound-${approvals.MAX_CONFIRMATIONS+1-offset}`));
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
    });

    await t.test("Fleet approval order matches append-authoritative resident panels under clock skew", async () => {
      const approval=(id,agent,ts)=>({v:0,ts:new Date(ts).toISOString(),source:"codex",
        agent_id:agent,project:"life",type:"needs_human",payload:{message:`Approve ${id}?`,
          request_id:id,action:"send_email",detail:{id},options:["approve","deny"]}});
      const oldSame=approval("order-old","codex:order-a",frozenNow+6*60*60*1000);
      const newSame=approval("order-new","codex:order-a",frozenNow-60*60*1000);
      const newestOther=approval("order-other","codex:order-b",frozenNow-2*60*60*1000);
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention,oldSame,newSame,newestOther]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
      await resetPage();await waitForTelemetry();
      const evidence=await eventually(send,`(()=>{
        openPanel(FLEET_LEDGER_ID);fleetTab='attention';renderFleet(${frozenNow});
        const fleetOrder=[...document.querySelectorAll('.ledger-entry')]
          .filter(row=>row.querySelector('.status-mark')?.textContent.trim()==='approval')
          .map(row=>row.dataset.approvalRequest);
        openPanel('codex:order-a');
        const panelOrder=[...document.querySelectorAll('[data-approval-request]')]
          .map(card=>card.dataset.approvalRequest);
        return {fleetOrder,panelOrder,
          fleetAOrder:fleetOrder.filter(id=>id==='order-old'||id==='order-new'),
          descriptive:[...document.querySelectorAll('[data-approval-request] .ledger-small')]
            .every(row=>row.textContent.includes('knocked'))};
      })()`,signal,"append-authoritative Fleet approval ordering");
      assert.deepEqual(evidence,{fleetOrder:["order-other","order-new","order-old"],
        panelOrder:["order-new","order-old"],fleetAOrder:["order-new","order-old"],
        descriptive:true});
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
    });

    await t.test("mixed plain and malformed knocks keep queued approval credentials actionable", async () => {
      const structured=(id,agent,ts)=>({v:0,ts:new Date(ts).toISOString(),source:"codex",
        agent_id:agent,project:"life",type:"needs_human",payload:{message:`Structured ${id}`,
          request_id:id,action:"send_email",detail:{id},options:["approve","deny"]}});
      const structuredPlain=structured("mixed-plain","codex:mixed-plain",frozenNow+3600000);
      const plain={...structuredPlain,ts:new Date(frozenNow-3600000).toISOString(),
        payload:{message:"Independent plain knock"}};
      const structuredMalformed=structured("mixed-malformed","codex:mixed-malformed",frozenNow+7200000);
      const malformed={...structuredMalformed,ts:new Date(frozenNow-7200000).toISOString(),
        payload:{message:"Independent malformed knock",request_id:"broken-shape",
          action:"SEND",detail:null,options:["approve"]}};
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention,structuredPlain,plain,
        structuredMalformed,malformed].map(value=>JSON.stringify(value)).join("\n")+"\n");
      await resetPage();await waitForTelemetry();
      const evidence=await eventually(send,`(async()=>{
        const originalFetch=window.fetch.bind(window);window.__mixedApprovalPost=null;
        window.fetch=async(url,options={})=>{
          if(String(url)==='http://127.0.0.1:8801/approvals/mixed-malformed'){
            window.__mixedApprovalPost={url:String(url),authorization:options.headers.Authorization,
              credentials:options.credentials,body:JSON.parse(options.body)};
            return {status:202,json:async()=>({request_id:'mixed-malformed',
              status:'recorded',decision:'approve'})};
          }
          return originalFetch(url,options);
        };
        openPanel('codex:mixed-plain');await new Promise(resolve=>requestAnimationFrame(resolve));
        const plainView={message:document.querySelector('.knock-msg')?.textContent,
          displayedStructured:villagers.find(v=>v.id==='codex:mixed-plain')?.knock?.structured!==null,
          queue:[...document.querySelectorAll('[data-approval-request]')]
            .map(card=>card.dataset.approvalRequest),
          connect:document.querySelector('[data-steward-change]')?.textContent.trim()};
        document.querySelector('[data-steward-change]').focus();
        document.querySelector('[data-steward-change]').click();
        while(!stewardDialog.open)await new Promise(resolve=>setTimeout(resolve,5));
        let token=document.querySelector('#steward-token');token.value='plain-connect-secret';
        document.querySelector('#steward-auth button[value="connect"]').click();
        while(!stewardConfig||stewardConfig.token!=='plain-connect-secret')
          await new Promise(resolve=>setTimeout(resolve,5));
        const connected={focus:document.activeElement?.hasAttribute('data-steward-change')||false,
          tokenCleared:token.value==='',secretAbsent:!document.documentElement.innerHTML.includes('plain-connect-secret')};
        document.querySelector('[data-steward-change]').focus();
        document.querySelector('[data-steward-change]').click();
        while(!stewardDialog.open)await new Promise(resolve=>setTimeout(resolve,5));
        token=document.querySelector('#steward-token');token.value='changed-secret';
        document.querySelector('#steward-auth button[value="connect"]').click();
        while(!stewardConfig||stewardConfig.token!=='changed-secret')
          await new Promise(resolve=>setTimeout(resolve,5));
        const changed={focus:document.activeElement?.hasAttribute('data-steward-change')||false,
          tokenCleared:token.value==='',secretAbsent:!document.documentElement.innerHTML.includes('changed-secret')};
        openPanel('codex:mixed-malformed');await new Promise(resolve=>requestAnimationFrame(resolve));
        const malformedView={message:document.querySelector('.knock-msg')?.textContent,
          displayedStructured:villagers.find(v=>v.id==='codex:mixed-malformed')?.knock?.structured!==null,
          queue:[...document.querySelectorAll('[data-approval-request]')]
            .map(card=>card.dataset.approvalRequest),clear:!!document.querySelector('[data-steward-clear]')};
        document.querySelector('[data-steward-clear]').focus();
        document.querySelector('[data-steward-clear]').click();
        const cleared={config:stewardConfig===null,focusInside:panelBody.contains(document.activeElement),
          changedSecretAbsent:!document.documentElement.innerHTML.includes('changed-secret')};
        document.querySelector('[data-approval-id="mixed-malformed"][data-approval-option="approve"]').click();
        while(!stewardDialog.open)await new Promise(resolve=>setTimeout(resolve,5));
        token=document.querySelector('#steward-token');token.value='direct-secret';
        document.querySelector('#steward-auth button[value="connect"]').click();
        while(!window.__mixedApprovalPost)await new Promise(resolve=>setTimeout(resolve,5));
        while(!approvalAcks.get('mixed-malformed')||
          approvalAcks.get('mixed-malformed').state==='requesting')
          await new Promise(resolve=>setTimeout(resolve,5));
        const direct={post:window.__mixedApprovalPost,
          state:approvalAcks.get('mixed-malformed').state,stillPlain:
            document.querySelector('.knock-msg')?.textContent==='Independent malformed knock',
          tokenCleared:token.value==='',secretAbsent:!document.documentElement.innerHTML.includes('direct-secret')};
        const staleClear=document.querySelector('[data-steward-clear]');
        fleetView.approvalState.requests.get('mixed-malformed').resolution={type:'test-stale-control'};
        staleClear.click();
        const staleRejected=stewardConfig?.token==='direct-secret'&&!stewardDialog.open;
        return {plainView,connected,changed,malformedView,cleared,direct,staleRejected};
      })()`,signal,"mixed-knock approval credential controls");
      assert.deepEqual(evidence,{plainView:{message:"Independent plain knock",
        displayedStructured:false,queue:["mixed-plain"],connect:"connect to Steward"},
        connected:{focus:true,tokenCleared:true,secretAbsent:true},
        changed:{focus:true,tokenCleared:true,secretAbsent:true},
        malformedView:{message:"Independent malformed knock",displayedStructured:false,
          queue:["mixed-malformed"],clear:true},
        cleared:{config:true,focusInside:true,changedSecretAbsent:true},
        direct:{post:{url:"http://127.0.0.1:8801/approvals/mixed-malformed",
          authorization:"Bearer direct-secret",credentials:"omit",body:{decision:"approve"}},
          state:"pending",stillPlain:true,tokenCleared:true,secretAbsent:true},staleRejected:true});
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
    });

    await t.test("production 409 handling trusts only Steward's exact expiry envelope", async () => {
      const approval=(id,offset)=>({v:0,ts:new Date(frozenNow+offset).toISOString(),
        source:"codex",agent_id:"codex:approval-409",project:"life",type:"needs_human",
        payload:{message:`Approve ${id}?`,request_id:id,action:"send_email",detail:{id},
          options:["approve","deny"]}});
      const ambiguous=approval("proxy-conflict",0), expired=approval("expired-conflict",1);
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention,ambiguous,expired]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
      await resetPage();await waitForTelemetry();
      const evidence=await eventually(send,`(async()=>{
        stewardConfig={url:'http://127.0.0.1:8801',token:'conflict-secret'};
        const originalFetch=window.fetch.bind(window);
        window.fetch=async(url,options={})=>{
          if(String(url).endsWith('/approvals/proxy-conflict'))return {status:409,
            json:async()=>({error:'approval_expired'})};
          if(String(url).endsWith('/approvals/expired-conflict'))return {status:409,
            json:async()=>({detail:{error:'approval_expired',message:'expired'}})};
          return originalFetch(url,options);
        };
        openPanel('codex:approval-409');await new Promise(resolve=>requestAnimationFrame(resolve));
        document.querySelector('[data-approval-id="proxy-conflict"][data-approval-option="approve"]').click();
        while(!approvalAcks.get('proxy-conflict')||approvalAcks.get('proxy-conflict').state==='requesting')
          await new Promise(resolve=>setTimeout(resolve,5));
        document.querySelector('[data-approval-id="expired-conflict"][data-approval-option="deny"]').click();
        while(!approvalAcks.get('expired-conflict')||approvalAcks.get('expired-conflict').state==='requesting')
          await new Promise(resolve=>setTimeout(resolve,5));
        return {ambiguous:approvalAcks.get('proxy-conflict').state,
          ambiguousBlocked:approvalAcks.blocks('proxy-conflict'),
          expired:approvalAcks.get('expired-conflict').state,
          expiredBlocked:approvalAcks.blocks('expired-conflict'),
          proxyText:document.querySelector('[data-approval-request="proxy-conflict"] .approval-feedback')?.textContent,
          expiryText:document.querySelector('[data-approval-request="expired-conflict"] .approval-feedback')?.textContent};
      })()`,signal,"strict production approval 409 envelope");
      assert.deepEqual(evidence,{ambiguous:"ambiguous",ambiguousBlocked:true,
        expired:"failed",expiredBlocked:false,
        proxyText:"Steward returned 409 after the decision may have been recorded.",
        expiryText:"Steward reports this approval expired and denies by default."});
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
    });

    await t.test("approval rerenders preserve keyed focus, draft, and caret with queue fallback", async () => {
      const approval = (id, offset) => ({v:0,ts:new Date(frozenNow+offset).toISOString(),
        source:"codex",agent_id:"codex:approval-focus",project:"life",type:"needs_human",
        payload:{message:`Approve ${id}?`,request_id:id,action:"send_email",
          detail:{request:id},options:["approve","deny","edit"]}});
      const first=approval("focus-1",0), second=approval("focus-2",1);
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention,first,second]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
      await resetPage(); await waitForTelemetry();
      const ticked=await eventually(send,`(async()=>{
        openPanel('codex:approval-focus');
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const input=document.querySelector('[data-approval-edit="focus-1"]');
        if(!input)return null;
        input.focus();input.value='carefully edited';
        input.dispatchEvent(new Event('input',{bubbles:true}));input.setSelectionRange(3,9);
        renderChrome(${frozenNow+1000});
        let active=document.activeElement;
        const textarea={key:active.dataset.approvalEdit,value:active.value,
          start:active.selectionStart,end:active.selectionEnd};
        document.querySelector('[data-approval-id="focus-1"][data-approval-option="deny"]').focus();
        renderChrome(${frozenNow+2000});active=document.activeElement;
        const button={id:active.dataset.approvalId,option:active.dataset.approvalOption};
        document.querySelector('[data-steward-change]').focus();renderChrome(${frozenNow+3000});
        const link=document.activeElement.hasAttribute('data-steward-change');
        active=document.querySelector('[data-approval-edit="focus-1"]');active.focus();active.setSelectionRange(3,9);
        return {textarea,button,link};})()`,signal,
        "one-second approval rerender focus");
      assert.deepEqual(ticked,{textarea:{key:"focus-1",value:"carefully edited",start:3,end:9},
        button:{id:"focus-1",option:"deny"},link:true});

      const third=approval("focus-3",2);
      fs.appendFileSync(events,JSON.stringify(third)+"\n");
      const queued=await eventually(send,`(async()=>{await runtime.poll();const active=document.activeElement;
        return {key:active?.dataset?.approvalEdit||null,value:active?.value||null,
          start:active?.selectionStart??null,end:active?.selectionEnd??null,
          tag:active?.tagName||null};})()`,signal,
        "approval queue insertion preserves keyed focus");
      assert.deepEqual(queued,{key:"focus-1",value:"carefully edited",start:3,end:9,tag:"TEXTAREA"});

      const submitFallback=await eventually(send,`(()=>{
        const button=document.querySelector('[data-approval-id="focus-1"][data-approval-option="deny"]');
        button.focus();
        const tracking=approvalAcks.request('focus-1','deny',null,runtime.snapshot().cursor,
          {agent_id:'codex:approval-focus',project:'life',action:'send_email',
            lifecycle:fleetView.approvalState.requests.get('focus-1').lifecycle});
        const active=document.activeElement;
        const result=tracking.ok&&active?.dataset?.approvalEdit==='focus-3'&&!active.disabled&&{
          key:active.dataset.approvalEdit,enabled:true,inside:panelBody.contains(active)};
        approvalAcks.failed('focus-1','test cleanup',true);
        return result;
      })()`,signal,"disabled submitted approval focus fallback");
      assert.deepEqual(submitFallback,{key:"focus-3",enabled:true,inside:true});

      const close={...first,ts:new Date(frozenNow+3).toISOString(),source:"steward",
        type:"needs_human_resolved",payload:{request_id:"focus-1",decision:"edit",
          decided_by:"api",action:"send_email"}};
      fs.appendFileSync(events,JSON.stringify(close)+"\n");
      const fallback=await eventually(send,`(async()=>{await runtime.poll();const active=document.activeElement;
        const confirmation=document.querySelector('[data-approval-confirmation="focus-1"]');
        const waiting=[...document.querySelectorAll('[data-approval-request]')]
          .map(item=>item.dataset.approvalRequest);
        return active?.dataset?.approvalEdit==='focus-3'&&panelBody.contains(active)&&confirmation&&{
          key:active.dataset.approvalEdit,inside:true,
          confirmation:confirmation.textContent.includes('Decision edit — confirmed by the exact closing event.'),
          waiting};})()`,signal,
        "resolved approval moves focus to remaining queue");
      assert.deepEqual(fallback,{key:"focus-3",inside:true,confirmation:true,
        waiting:["focus-3","focus-2"]});
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
      const resetFocus=await eventually(send,`(async()=>{await runtime.poll();return !panelEl.classList.contains('open')&&document.activeElement.id==='fleet-open'&&{
          closed:true,focus:document.activeElement.id};})()`,signal,
        "approval reset restores an accessible launcher");
      assert.deepEqual(resetFocus,{closed:true,focus:"fleet-open"});
    });

    await t.test("collided approval identity is diagnostic-only in production UI", async () => {
      const requestA={v:0,ts:new Date(frozenNow).toISOString(),source:"codex",
        agent_id:"codex:collision-a",project:"life",type:"needs_human",payload:{
          message:"Approve A?",request_id:"collision-1",action:"send_email",
          detail:{target:"a"},options:["approve","deny"]}};
      const requestB={...requestA,ts:new Date(frozenNow+1).toISOString(),
        agent_id:"codex:collision-b",payload:{...requestA.payload,message:"Approve B?",
          action:"publish_note",detail:{target:"b"}}};
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention,requestA,requestB]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
      await resetPage(); await waitForTelemetry();
      const evidence=await eventually(send,`(()=>{
        openPanel(FLEET_LEDGER_ID);fleetTab='attention';renderFleet(Date.now());
        const approvalRows=[...panelBody.querySelectorAll('.status-mark')]
          .filter(item=>item.textContent.trim()==='approval').length;
        const diagnostic=panelBody.textContent.includes('request_id collision')&&
          panelBody.textContent.includes('collision-1');
        const count=document.getElementById('panel-status').textContent;
        openPanel('codex:collision-a');
        return approvalRows===0&&diagnostic&&
          panelBody.querySelectorAll('[data-approval-option]').length===0&&{
            actionable:false,diagnostic:true,
            count,
            knocking:villagers.some(item=>item.id.startsWith('codex:collision-')&&item.state==='knocking')};
      })()`,signal,"collided approval quarantine UI");
      assert.deepEqual(evidence,{actionable:false,diagnostic:true,
        count:"Needs you: 1 item.",knocking:false});
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
    });

    await t.test("slow successful job POST renders the request-start deadline before response", async () => {
      await resetPage();
      await waitForTelemetry();
      const evidence = await eventually(send, `(async () => {
        stewardConfig = {url:'http://127.0.0.1:8801',token:'slow-secret'};
        const originalFetch = window.fetch.bind(window);
        let resolvePost; window.__slowPostStarted = false;
        window.fetch = async (url, options = {}) => {
          if (String(url) === 'http://127.0.0.1:8801/jobs') {
            window.__slowPostStarted = true;
            return new Promise(resolve => { resolvePost = resolve; });
          }
          return originalFetch(url, options);
        };
        document.querySelector('[data-panel-target="job-board"]').click();
        const form = document.querySelector('[data-post-job]');
        form.elements.title.value = 'Slow but accepted';
        form.elements.title.dispatchEvent(new Event('input',{bubbles:true}));
        form.requestSubmit();
        while (!window.__slowPostStarted) await new Promise(resolve => setTimeout(resolve,5));
        window.__advanceBurrowClock(BurrowJobs.DEFAULT_ACK_MS);
        runtime.tick();
        const atDeadline = {state:jobAcks.latest().state,
          alert:document.querySelector('.job-ack')?.textContent,
          draft:document.querySelector('[data-post-job]').elements.title.value};
        resolvePost({status:202,json:async()=>({status:'accepted',task_id:'slow-job',request_id:'slow-request'})});
        await new Promise(resolve => setTimeout(resolve,20));
        const late = jobAcks.latest();
        return {atDeadline,afterResponse:late.state,taskId:late.task_id,
          requestId:late.request_id,deadlineRecorded:Number.isFinite(late.deadlineElapsedAt),
          draftAfter:document.querySelector('[data-post-job]').elements.title.value,
          optimistic:!!document.querySelector('[data-task-id="slow-job"]'),
          disabled:[...document.querySelector('[data-post-job]').elements].every(control=>control.disabled)};
      })()`, signal, "slow job response deadline");
      assert.deepEqual(evidence, {atDeadline:{state:"timeout",
        alert:"No matching task_posted event arrived before the acknowledgement timeout.",
        draft:"Slow but accepted"},afterResponse:"timeout",taskId:"slow-job",
        requestId:"slow-request",deadlineRecorded:true,draftAfter:"Slow but accepted",
        optimistic:false,disabled:true});

      const unrelated = {v:0,ts:new Date(frozenNow + 1).toISOString(),source:"steward",
        agent_id:"steward:api",project:"steward",type:"task_posted",
        payload:{task_id:"slow-unrelated",title:"Unrelated",required_skills:[],posted_by:"api"}};
      fs.appendFileSync(events, JSON.stringify(unrelated) + "\n");
      const unrelatedState = await eventually(send, `(async () => {
        await runtime.poll();
        return {state:jobAcks.latest().state,
          disabled:[...document.querySelector('[data-post-job]').elements].every(control=>control.disabled)};
      })()`, signal, "unrelated event cannot reconcile late valid response");
      assert.deepEqual(unrelatedState,{state:"timeout",disabled:true});

      const exact = {...unrelated,ts:new Date(frozenNow + 2).toISOString(),
        payload:{task_id:"slow-job",title:"Slow but accepted",required_skills:[],posted_by:"api"}};
      fs.appendFileSync(events, JSON.stringify(exact) + "\n");
      const reconciled = await eventually(send, `(async () => {
        await runtime.poll();
        const ack=jobAcks.latest();
        return {state:ack.state,deadlineRecorded:Number.isFinite(ack.deadlineElapsedAt),
          status:document.querySelector('.job-ack')?.textContent,
          enabled:[...document.querySelector('[data-post-job]').elements].every(control=>!control.disabled),
          exact:!!document.querySelector('[data-task-id="slow-job"]')};
      })()`, signal, "late exact job event reconciliation");
      assert.deepEqual(reconciled,{state:"acknowledged",deadlineRecorded:true,
        status:"Posted — confirmed by the matching task_posted event after the acknowledgement deadline had elapsed.",
        enabled:true,exact:true});
      fs.writeFileSync(events, [currentEvent, agedEvent, agedAttention]
        .map(value => JSON.stringify(value)).join("\n") + "\n");
    });

    await t.test("malformed 202 uses a safe task identity only to match real production evidence", async () => {
      await resetPage();
      await waitForTelemetry();
      const beforeEvidence = await eventually(send, `(async () => {
        stewardConfig = {url:'http://127.0.0.1:8801',token:'malformed-secret'};
        const originalFetch = window.fetch.bind(window);
        let resolvePost;
        window.fetch = async (url, options = {}) => {
          if (String(url) === 'http://127.0.0.1:8801/jobs')
            return new Promise(resolve => { resolvePost = resolve; });
          return originalFetch(url, options);
        };
        document.querySelector('[data-panel-target="job-board"]').click();
        const form=document.querySelector('[data-post-job]');
        form.elements.title.value='Malformed acceptance';
        form.elements.title.dispatchEvent(new Event('input',{bubbles:true}));
        form.requestSubmit();
        while (!resolvePost) await new Promise(resolve=>setTimeout(resolve,5));
        resolvePost({status:202,json:async()=>({status:'queued',task_id:'malformed-job'})});
        await new Promise(resolve=>setTimeout(resolve,20));
        const ack=jobAcks.latest();
        return {state:ack.state,taskId:ack.task_id,requestId:ack.request_id ?? null,
          optimistic:!!document.querySelector('[data-task-id="malformed-job"]'),
          disabled:[...form.elements].every(control=>control.disabled)};
      })()`, signal, "malformed 202 remains ambiguous without event proof");
      assert.deepEqual(beforeEvidence,{state:"ambiguous",taskId:"malformed-job",
        requestId:null,optimistic:false,disabled:true});

      const unrelated = {v:0,ts:new Date(frozenNow + 3).toISOString(),source:"steward",
        agent_id:"steward:api",project:"steward",type:"task_posted",
        payload:{task_id:"malformed-other",title:"Other",required_skills:[],posted_by:"api"}};
      fs.appendFileSync(events, JSON.stringify(unrelated) + "\n");
      const unrelatedState = await eventually(send, `(async()=>{ await runtime.poll();
        return jobAcks.latest().state; })()`,signal,"malformed acceptance ignores unrelated event");
      assert.equal(unrelatedState,"ambiguous");
      const exact = {...unrelated,ts:new Date(frozenNow + 4).toISOString(),
        payload:{task_id:"malformed-job",title:"Malformed acceptance",required_skills:[],posted_by:"api"}};
      fs.appendFileSync(events, JSON.stringify(exact) + "\n");
      const afterEvidence = await eventually(send, `(async()=>{ await runtime.poll();
        const form=document.querySelector('[data-post-job]'); const ack=jobAcks.latest();
        return {state:ack.state,enabled:[...form.elements].every(control=>!control.disabled),
          exact:!!document.querySelector('[data-task-id="malformed-job"]')}; })()`,
        signal,"malformed acceptance exact event reconciliation");
      assert.deepEqual(afterEvidence,{state:"acknowledged",enabled:true,exact:true});
      fs.writeFileSync(events, [currentEvent, agedEvent, agedAttention]
        .map(value => JSON.stringify(value)).join("\n") + "\n");
    });

    await t.test("job form prevents overlap and keeps definitive refusal visible and retryable", async () => {
      await resetPage();
      await waitForTelemetry();
      const evidence = await eventually(send, `(async () => {
        stewardConfig = {url:'http://127.0.0.1:8801',token:'overlap-secret'};
        const originalFetch = window.fetch.bind(window);
        let resolveFirst; let calls = 0;
        window.fetch = async (url, options = {}) => {
          if (String(url) === 'http://127.0.0.1:8801/jobs') {
            calls++;
            if (calls === 1) return new Promise(resolve => { resolveFirst = resolve; });
            return {status:422,json:async()=>({})};
          }
          return originalFetch(url, options);
        };
        document.querySelector('[data-panel-target="job-board"]').click();
        const form = document.querySelector('[data-post-job]');
        const title = form.elements.title;
        title.value = 'Only once';
        title.dispatchEvent(new Event('input',{bubbles:true}));
        title.focus();
        form.requestSubmit();
        while (calls !== 1) await new Promise(resolve => setTimeout(resolve,5));
        const during = {sameForm:document.querySelector('[data-post-job]') === form,
          disabled:[...form.elements].every(control=>control.disabled),
          busy:form.getAttribute('aria-busy'),draft:title.value,
          status:document.querySelector('.job-ack')?.textContent};
        form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
        await new Promise(resolve => setTimeout(resolve,10));
        const callsAfterOverlap = calls;
        resolveFirst({status:422,json:async()=>({})});
        await new Promise(resolve => setTimeout(resolve,20));
        const afterFailure = {state:jobAcks.latest().state,
          enabled:[...form.elements].every(control=>!control.disabled),
          busy:form.getAttribute('aria-busy'),draft:title.value,
          failure:document.querySelector('.job-ack')?.textContent};
        form.requestSubmit();
        await new Promise(resolve => setTimeout(resolve,20));
        return {during,callsAfterOverlap,afterFailure,callsAfterRetry:calls,
          sameFormAfter:document.querySelector('[data-post-job]') === form};
      })()`, signal, "job submission overlap and definitive retry");
      assert.deepEqual(evidence, {during:{sameForm:true,disabled:true,busy:"true",draft:"Only once",
        status:"Sending directly to Steward…"},callsAfterOverlap:1,
        afterFailure:{state:"failed",enabled:true,busy:"false",draft:"Only once",
          failure:"Steward refused the job (422)."},callsAfterRetry:2,sameFormAfter:true});
    });

    await t.test("ambiguous job outcome disables unsafe duplicate submission", async () => {
      await resetPage();
      await waitForTelemetry();
      const evidence = await eventually(send, `(async () => {
        stewardConfig = {url:'http://127.0.0.1:8801',token:'ambiguous-secret'};
        const originalFetch = window.fetch.bind(window); let calls = 0;
        window.fetch = async (url, options = {}) => {
          if (String(url) === 'http://127.0.0.1:8801/jobs') {
            calls++; return {status:503,json:async()=>({error:'upstream failed'})};
          }
          return originalFetch(url, options);
        };
        document.querySelector('[data-panel-target="job-board"]').click();
        const form = document.querySelector('[data-post-job]');
        form.elements.title.value = 'Maybe posted';
        form.elements.title.dispatchEvent(new Event('input',{bubbles:true}));
        form.requestSubmit();
        await new Promise(resolve => setTimeout(resolve,20));
        const beforeDuplicate = {state:jobAcks.latest().state,
          disabled:[...form.elements].every(control=>control.disabled),
          alert:document.querySelector('.job-ack')?.textContent};
        form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
        await new Promise(resolve => setTimeout(resolve,10));
        return {beforeDuplicate,calls,notice:document.querySelector('[data-job-feedback]').textContent
          .includes('no duplicate request was sent')};
      })()`, signal, "ambiguous post duplicate prevention");
      assert.deepEqual(evidence, {beforeDuplicate:{state:"ambiguous",disabled:true,
        alert:"Steward returned 503 after the job may have been recorded; the outcome is ambiguous."},
        calls:1,notice:true});
    });

    await t.test("job capacity reports newer omitted terminal records without an age claim", async () => {
      const activePosts = Array.from({length:24}, (_, index) => ({
        v:0,ts:new Date(frozenNow - 60_000 + index).toISOString(),source:"steward",
        agent_id:"steward:api",project:"steward",type:"task_posted",
        payload:{task_id:`capacity-open-${index}`,title:`Open ${index}`,
          required_skills:[],posted_by:"api"},
      }));
      const newerDone = {v:0,ts:new Date(frozenNow - 1_000).toISOString(),source:"steward",
        agent_id:"codex:done",project:"life",type:"task_done",
        payload:{task_id:"newer-orphan-done",title:"Newer orphan done",
          claimant:"codex:done",artifacts:[]}};
      const malformed = {...activePosts[0],ts:new Date(frozenNow - 500).toISOString(),
        payload:{...activePosts[0].payload,task_id:"malformed-capacity",title:""}};
      fs.writeFileSync(events, [currentEvent, agedEvent, agedAttention,
        ...activePosts, newerDone, malformed].map(value => JSON.stringify(value)).join("\n") + "\n");
      await resetPage();
      await waitForTelemetry();
      const singular = await eventually(send, `(() => {
        document.querySelector('[data-panel-target="job-board"]').click();
        const list=document.querySelector('[data-job-list]');
        return {cards:list.querySelectorAll('[data-task-id]').length,
          activePreserved:[...list.querySelectorAll('[data-task-id]')]
            .every(card=>card.dataset.taskId.startsWith('capacity-open-')),
          newerDoneVisible:!!list.querySelector('[data-task-id="newer-orphan-done"]'),
          empty:!!list.querySelector('.artifact-empty'),
          statuses:[...list.querySelectorAll('[role="status"]')]
            .map(node=>node.textContent.replace(/\\s+/g,' ').trim())};
      })()`, signal, "singular age-neutral job capacity diagnostic");
      assert.deepEqual(singular, {cards:24,activePreserved:true,newerDoneVisible:false,
        empty:false,statuses:[
          "Skipped 1 malformed task event; missing IDs or titles never become jobs.",
          "1 task record was omitted by the bounded board.",
        ]});

      const newerFailed = {v:0,ts:new Date(frozenNow).toISOString(),source:"steward",
        agent_id:"codex:failed",project:"life",type:"task_failed",
        payload:{task_id:"newer-orphan-failed",title:"Newer orphan failed",
          claimant:"codex:failed",reason:"session_failed"}};
      fs.appendFileSync(events, JSON.stringify(newerFailed) + "\n");
      const plural = await eventually(send, `(async () => {
        await runtime.poll();
        const list=document.querySelector('[data-job-list]');
        return {cards:list.querySelectorAll('[data-task-id]').length,
          newerFailedVisible:!!list.querySelector('[data-task-id="newer-orphan-failed"]'),
          empty:!!list.querySelector('.artifact-empty'),
          statuses:[...list.querySelectorAll('[role="status"]')]
            .map(node=>node.textContent.replace(/\\s+/g,' ').trim())};
      })()`, signal, "plural age-neutral job capacity diagnostic");
      assert.deepEqual(plural, {cards:24,newerFailedVisible:false,empty:false,statuses:[
        "Skipped 1 malformed task event; missing IDs or titles never become jobs.",
        "2 task records were omitted by the bounded board.",
      ]});
      fs.writeFileSync(events, [currentEvent, agedEvent, agedAttention]
        .map(value => JSON.stringify(value)).join("\n") + "\n");
    });

    await t.test("quiet job board expires ordinary failures but keeps lease-expired work open", async () => {
      const open = {v:0,ts:new Date(frozenNow - 61_000).toISOString(),source:"steward",
        agent_id:"steward:api",project:"steward",type:"task_posted",
        payload:{task_id:"aging-job",title:"Aging job",required_skills:[],posted_by:"api"}};
      const failedPost = {...open,ts:new Date(frozenNow - 16 * 60_000).toISOString(),
        payload:{task_id:"expiring-job",title:"Expiring job",required_skills:[],posted_by:"api"}};
      const failed = {...failedPost,ts:new Date(frozenNow - 14 * 60_000).toISOString(),
        agent_id:"layout-resident",project:"burrow",type:"task_failed",
        payload:{task_id:"expiring-job",title:"Expiring job",claimant:"layout-resident",
          reason:"session_failed"}};
      const reopenedPost = {...open,ts:new Date(frozenNow - 18 * 60_000).toISOString(),
        payload:{task_id:"reopened-job",title:"Reopened job",required_skills:[],posted_by:"api"}};
      const reopened = {...reopenedPost,ts:new Date(frozenNow - 17 * 60_000).toISOString(),
        agent_id:"codex:worker",project:"life",type:"task_failed",
        payload:{task_id:"reopened-job",title:"Reopened job",claimant:"codex:worker",
          reason:"lease_expired"}};
      const presentPost = {...open,ts:new Date(frozenNow - 12 * 60_000).toISOString(),
        payload:{task_id:"present-retry",title:"Present retry",required_skills:[],posted_by:"api"}};
      const presentReopened = {...presentPost,ts:new Date(frozenNow - 11 * 60_000).toISOString(),
        agent_id:"layout-resident",project:"burrow",type:"task_failed",
        payload:{task_id:"present-retry",title:"Present retry",claimant:"layout-resident",
          reason:"lease_expired"}};
      const orphan = {...open,ts:new Date(frozenNow - 30_000).toISOString(),
        agent_id:"codex:orphan",project:"life",type:"task_claimed",
        payload:{task_id:"orphan-job",title:"Orphan job",claimant:"codex:orphan"}};
      const blankSkills = {...open,ts:new Date(frozenNow - 40_000).toISOString(),
        payload:{task_id:"blank-skills",title:"Blank skills",
          required_skills:["","  ","research"],posted_by:"api"}};
      fs.writeFileSync(events, [currentEvent, agedEvent, agedAttention, open, failedPost, failed,
        reopenedPost, reopened, presentPost, presentReopened, orphan, blankSkills]
        .map(value => JSON.stringify(value)).join("\n") + "\n");
      await resetPage();
      await waitForTelemetry();
      const evidence = await eventually(send, `(() => {
        document.querySelector('[data-panel-target="job-board"]').click();
        const form = document.querySelector('[data-post-job]');
        const title = form.elements.title;
        title.value = 'draft survives time';
        title.dispatchEvent(new Event('input',{bubbles:true}));
        title.focus();
        const initialAge = document.querySelector('[data-task-id="aging-job"] .job-skills').textContent;
        const terminalInitiallyVisible = !!document.querySelector('[data-task-id="expiring-job"]');
        const presentRetry = document.querySelector('[data-task-id="present-retry"]');
        const presentRetryText = presentRetry.textContent.replace(/\\s+/g,' ').trim();
        const presentRetryLink = !!presentRetry.querySelector('[data-agent="layout-resident"]');
        const reopenedBefore = document.querySelector('[data-task-id="reopened-job"]')
          .textContent.replace(/\\s+/g,' ').trim();
        const orphanSkills = document.querySelector('[data-task-id="orphan-job"] .job-skills')
          .textContent.replace(/\\s+/g,' ').trim();
        const blankSkillsElement = document.querySelector('[data-task-id="blank-skills"] .job-skills');
        const blankSkillsText = blankSkillsElement.textContent.replace(/\\s+/g,' ').trim();
        const blankSkillMarkers = [...blankSkillsElement.querySelectorAll('.job-skill-empty')]
          .map(element => element.getAttribute('aria-label'));
        const retryLinkBefore = presentRetry.querySelector('[data-agent="layout-resident"]');
        retryLinkBefore.focus();
        runtime.tick();
        const retryLinkAfter = document.querySelector('[data-task-id="present-retry"]')
          .querySelector('[data-agent="layout-resident"]');
        const claimantFocusSurvivesTick = document.activeElement === retryLinkAfter &&
          retryLinkAfter !== retryLinkBefore;
        const expiringLink = document.querySelector('[data-task-id="expiring-job"]')
          .querySelector('[data-agent="layout-resident"]');
        expiringLink.focus();
        window.__advanceBurrowClock(2 * 60 * 1000);
        runtime.tick();
        const currentForm = document.querySelector('[data-post-job]');
        return {initialAge, laterAge:document.querySelector('[data-task-id="aging-job"] .job-skills').textContent,
          terminalInitiallyVisible, terminalGone:!document.querySelector('[data-task-id="expiring-job"]'),
          reopenedStillVisible:!!document.querySelector('[data-task-id="reopened-job"]'),reopenedBefore,
          presentRetryText,presentRetryLink,
          sameForm:currentForm === form, sameTitle:currentForm.elements.title === title,
          claimantFocusSurvivesTick,
          expiryFocusInsideDialog:document.activeElement === document.querySelector('#panel-title'),
          draft:title.value,orphanSkills,
          blankSkillsText,blankSkillMarkers};
      })()`, signal, "wall-clock job age and expiry");
      assert.deepEqual(evidence, {initialAge:"no required skills · posted 1m ago",
        laterAge:"no required skills · posted 3m ago",terminalInitiallyVisible:true,
        terminalGone:true,reopenedStillVisible:true,
        reopenedBefore:"Reopened job open no required skills · posted 18m ago open reason: lease_expired retry after expired lease · former claimant codex:worker absent",
        presentRetryText:"Present retry open no required skills · posted 12m ago open reason: lease_expired retry after expired lease · former claimant Sorrel",
        presentRetryLink:true,
        sameForm:true,sameTitle:true,claimantFocusSurvivesTick:true,
        expiryFocusInsideDialog:true,draft:"draft survives time",
        orphanSkills:"required skills unavailable · posted age unavailable · updated 30s ago",
        blankSkillsText:"skills: (unnamed skill), (unnamed skill), research · posted 40s ago",
        blankSkillMarkers:["blank required skill","blank required skill"]});
      fs.writeFileSync(events, [currentEvent, agedEvent, agedAttention]
        .map(value => JSON.stringify(value)).join("\n") + "\n");
    });

    await t.test("rejected Steward credentials can be changed and retried without reload", async () => {
      await resetPage();
      await waitForTelemetry();
      const evidence = await eventually(send, `(async () => {
        stewardConfig = {url:'http://127.0.0.1:8801',token:'rejected-secret'};
        const originalFetch = window.fetch.bind(window);
        const authorizations = [];
        window.fetch = async (url, options = {}) => {
          if (String(url) === 'http://127.0.0.1:8801/jobs') {
            authorizations.push(options.headers.Authorization);
            if (authorizations.length === 1) return {status:401,json:async()=>({})};
            return {status:202,json:async()=>({status:'accepted',task_id:'retry-job',request_id:'retry-request'})};
          }
          return originalFetch(url, options);
        };
        document.querySelector('[data-panel-target="job-board"]').click();
        let form = document.querySelector('[data-post-job]');
        form.elements.title.value = 'Retry safely';
        form.elements.title.dispatchEvent(new Event('input',{bubbles:true}));
        form.requestSubmit();
        await new Promise(resolve => setTimeout(resolve,20));
        const rejected = {configCleared:stewardConfig === null,
          retryMessage:document.querySelector('#panel-body').textContent.includes('Connect again to retry without reloading'),
          connectVisible:!!document.querySelector('[data-steward-change]'),
          oldSecretAbsent:!document.documentElement.innerHTML.includes('rejected-secret')};
        document.querySelector('[data-steward-change]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        const token = document.querySelector('#steward-token');
        token.value = 'correct-secret';
        document.querySelector('#steward-auth button[value="connect"]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        form = document.querySelector('[data-post-job]');
        form.requestSubmit();
        await new Promise(resolve => setTimeout(resolve,20));
        const state = jobAcks.latest().state;
        const clear = document.querySelector('[data-steward-clear]');
        const clearAccessible = clear && clear.type === 'button' && clear.textContent.includes('clear credentials');
        clear.click();
        return {rejected,authorizations,state,clearAccessible,configClearedAfter:stewardConfig === null,
          tokenFieldCleared:token.value === '',newSecretAbsent:!document.documentElement.innerHTML.includes('correct-secret')};
      })()`, signal, "credential rejection correction");
      assert.deepEqual(evidence, {rejected:{configCleared:true,retryMessage:true,
        connectVisible:true,oldSecretAbsent:true},authorizations:["Bearer rejected-secret","Bearer correct-secret"],
        state:"pending",clearAccessible:true,configClearedAfter:true,tokenFieldCleared:true,newSecretAbsent:true});
    });

    await t.test("Run Now refusal stays retryable without corrupting next-run truth", async () => {
      await resetPage();
      await waitForTelemetry();
      const evidence = await eventually(send, `(async () => {
        await runtime.refreshResidents();
        stewardConfig = {url:'http://127.0.0.1:8801',token:'ephemeral'};
        stewardDeclarations = {state:'loaded',error:null,byRoutine:new Map([[
          JSON.stringify(['life-agent','daily-summary']), {next_fire:'2026-08-26T07:00:00Z',enabled:true,retired:false}
        ]])};
        document.querySelector('#fleet-open').click();
        document.querySelector('[data-fleet-tab="routines"]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        [...document.querySelectorAll('.ledger-entry')].find(item => item.textContent.includes('daily-summary'))
          .querySelector('[data-agent]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (url, options) => String(url).startsWith('http://127.0.0.1:8801/') ?
          ({status:503,json:async()=>({error:'busy'})}) : originalFetch(url, options);
        document.querySelector('[data-run-routine]').click();
        await new Promise(resolve => setTimeout(resolve, 20));
        const text = document.querySelector('#panel-body').textContent;
        return {failure:text.includes('request failed — Steward refused the request (503) — retry available'),
          nextRun:text.includes('declared by Steward, not observed'),
          retryEnabled:!document.querySelector('[data-run-routine]').disabled,
          declarationState:stewardDeclarations.state};
      })()`, signal, "retryable Run Now refusal");
      assert.deepEqual(evidence, {failure:true,nextRun:true,retryEnabled:true,declarationState:'loaded'});
    });

    await t.test("Run Now capacity refusal is accessible and has no connection side effect", async () => {
      await resetPage();
      await waitForTelemetry();
      const evidence = await eventually(send, `(async () => {
        await runtime.refreshResidents();
        document.querySelector('#fleet-open').click();
        document.querySelector('[data-fleet-tab="routines"]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        [...document.querySelectorAll('.ledger-entry')].find(item => item.textContent.includes('daily-summary'))
          .querySelector('[data-agent]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        for (let i = 0; i < BurrowRoutines.MAX_ACKNOWLEDGEMENTS; i++)
          routineAcks.requested({agent_id:'capacity-' + i}, 'routine-' + i, Date.now());
        window.__capacityRunPosts = 0;
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (url, options = {}) => {
          const target = new URL(String(url), location.href);
          if (options.method === 'POST' && target.pathname.startsWith('/residents/') &&
              target.pathname.includes('/routines/') && target.pathname.endsWith('/run'))
            window.__capacityRunPosts++;
          return originalFetch(url, options);
        };
        document.querySelector('[data-run-routine]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        const button = document.querySelector('[data-run-routine]');
        const alert = button.parentElement.querySelector('[role="alert"]');
        return {enabled:!button.disabled,alert:alert && alert.textContent,
          runPosts:window.__capacityRunPosts,authOpen:document.querySelector('#steward-auth').open,
          tracked:routineAcks.size()};
      })()`, signal, "explicit Run Now capacity refusal");
      assert.deepEqual(evidence, {enabled:true,
        alert:"run-request tracking is full of unresolved requests; no request was sent",
        runPosts:0,authOpen:false,tracked:200});
    });

    await t.test("project Run Now keeps active navigation but accepts a distinct Steward runner", async () => {
      const activeOwner = { ...currentEvent, source:"codex", agent_id:"codex:project-owner",
        project:"quiet-project", payload:{tool:"Read",detail:"project owner"} };
      fs.appendFileSync(events, JSON.stringify(activeOwner) + "\n");
      await resetPage();
      await waitForTelemetry();
      const projectedOwner = await eventually(send, `(async () => {
        await runtime.poll();
        const owner = villagers.find(villager => villager.id === 'codex:project-owner');
        return owner && {id:owner.id,project:owner.project};
      })()`, signal, "production projection observes the active project owner");
      const pending = await eventually(send, `(async () => {
        await runtime.refreshResidents();
        stewardConfig = {url:'http://127.0.0.1:8801',token:'ephemeral'};
        stewardDeclarations = {state:'loaded',error:null,byRoutine:new Map([[
          JSON.stringify(['life-agent','daily-summary']), {next_fire:'2026-08-26T07:00:00Z',enabled:true,retired:false}
        ]])};
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (url, options) => String(url).startsWith('http://127.0.0.1:8801/') ?
          ({status:202,json:async()=>({status:'accepted',request_id:'mixed-request'})}) :
          originalFetch(url, options);
        document.querySelector('#fleet-open').click();
        document.querySelector('[data-fleet-tab="routines"]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        const row = [...document.querySelectorAll('.ledger-entry')]
          .find(item => item.textContent.includes('daily-summary'));
        const link = row && row.querySelector('[data-agent]');
        if (!link) return false;
        const navigationOwner = link.dataset.agent;
        link.click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        const button = document.querySelector('[data-run-routine]');
        const correlation = {agent:button.dataset.runAgent,project:button.dataset.runProject};
        const pendingStatus = new Promise((resolve, reject) => {
          const current = () => document.querySelector('#panel-body [role="status"]')
            ?.textContent.includes('pending acknowledgement');
          if (current()) { resolve(); return; }
          const observer = new MutationObserver(() => {
            if (!current()) return;
            observer.disconnect(); clearTimeout(timer); resolve();
          });
          observer.observe(document.querySelector('#panel-body'), {childList:true,subtree:true});
          const timer = setTimeout(() => { observer.disconnect();
            reject(new Error('pending acknowledgement was not rendered')); }, 1000);
        });
        button.click();
        await pendingStatus;
        const ack = routineAcks.get({project:'quiet-project'},'daily-summary');
        return {navigationOwner,correlation,state:ack && ack.state,
          openOwner:document.querySelector('#panel-body').textContent.includes('codex:project-owner')};
      })()`, signal, "project Run Now pending state");

      const runnerStart = { v:0, ts:new Date(frozenNow + 1).toISOString(), source:"steward",
        agent_id:"steward:ephemeral-runner", project:"quiet-project", type:"routine_started",
        payload:{routine:"daily-summary",run_id:"mixed-run",trigger:"manual"} };
      fs.appendFileSync(events, JSON.stringify(runnerStart) + "\n");
      const running = await eventually(send, `(async () => {
        await runtime.poll(); renderPanel(Date.now());
        const ack = routineAcks.get({project:'quiet-project'},'daily-summary');
        return ack && {state:ack.state,agent_id:ack.agent_id,run_id:ack.run_id,
          visible:document.querySelector('#panel-body [role="status"]')?.textContent};
      })()`, signal, "project Run Now running state");

      const runnerFinish = { ...runnerStart, ts:new Date(frozenNow + 2).toISOString(),
        type:"routine_finished", payload:{routine:"daily-summary",run_id:"mixed-run",
          outcome:"ok",artifacts:[],duration_s:1} };
      fs.appendFileSync(events, JSON.stringify(runnerFinish) + "\n");
      const completed = await eventually(send, `(async () => {
        await runtime.poll(); renderPanel(Date.now());
        const ack = routineAcks.get({project:'quiet-project'},'daily-summary');
        return ack && {state:ack.state,
          visible:document.querySelector('#panel-body [role="status"]')?.textContent};
      })()`, signal, "project Run Now terminal state");
      fs.writeFileSync(events, [currentEvent, agedEvent, agedAttention]
        .map(value => JSON.stringify(value)).join("\n") + "\n");

      assert.deepEqual(projectedOwner,
        {id:"codex:project-owner",project:"quiet-project"});
      assert.deepEqual(pending, {navigationOwner:"codex:project-owner",
        correlation:{agent:"",project:"quiet-project"},state:"pending",openOwner:true});
      assert.deepEqual(running, {state:"running",agent_id:"steward:ephemeral-runner",
        run_id:"mixed-run",visible:"running — confirmed by matching real event"});
      assert.deepEqual(completed,
        {state:"completed",visible:"completed — confirmed by matching real event"});
    });

    await t.test("journal observation draws an accessible home writing spot and keeps content truth separate", async () => {
      await resetPage();
      await waitForTelemetry();
      await eventually(send, "runtime.refreshResidents().then(() => fleetView.residents.length > 0)",
        signal, "journal resident declaration");
      const before = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
      const observed = {v:0,ts:new Date(frozenNow).toISOString(),source:"steward",
        agent_id:"codex:journal-layout",project:"quiet-project",type:"journal_written",
        payload:{routine:"close-of-day",day:"2025-01-15",
          path:"/data/residents/life/memory/journal/2025-01-15.md"}};
      fs.appendFileSync(events, JSON.stringify(observed) + "\n");
      const villageEvidence = await eventually(send, `(async () => {
        await runtime.poll(); await runtime.refreshResidents();
        const villager=runtime.snapshot().villagers.find(item=>item.id==='codex:journal-layout');
        const prop=scene && scene.journalProps && scene.journalProps[5];
        if(!villager||!prop||!prop.visible)return false;
        const chip=scene.viz.get(villager.id).chip.el;
        return {state:villager.state,doing:villager.doing,home:villager.home,place:villager.place,
          propVisible:prop.visible,parts:prop.list.length,partKinds:prop.list.map(item=>item.type),
          glow:scene.glows[5].visible,aria:chip.getAttribute('aria-label')};
      })()`, signal, "journal home writing spot");
      const after = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
      const panels = await eventually(send, `(async () => {
        openPanel('codex:journal-layout'); await new Promise(r=>requestAnimationFrame(r));
        const detail=document.querySelector('#panel-body');
        const path=detail.querySelector('[data-journal-observed] code');
        const detailText=detail.textContent;
        const mismatch=Boolean(detail.querySelector('[data-journal-mismatch]'));
        closePanel(); await new Promise(r=>setTimeout(r,250));
        document.querySelector('#fleet-open').click();
        await new Promise(r=>requestAnimationFrame(r));
        const activity=document.querySelector('#panel-body').textContent;
        document.querySelector('[data-fleet-tab="residents"]').click();
        await new Promise(r=>requestAnimationFrame(r));
        const directory=document.querySelector('#panel-body').textContent;
        return {detailText,activity,directory,pathText:path&&path.textContent,
          pathLinked:Boolean(path&&path.closest('a[href]')),mismatch};
      })()`, signal, "journal resident and Fleet copy");
      const expired = await eventually(send, `(() => {
        window.__advanceBurrowClock(60000); runtime.tick();
        return {villager:runtime.snapshot().villagers.some(item=>item.id==='codex:journal-layout'),
          prop:scene.journalProps[5].visible,
          retained:runtime.snapshot().journalState.records.size};
      })()`, signal, "journal overlay expiry");
      fs.writeFileSync(events, [currentEvent, agedEvent, agedAttention]
        .map(value => JSON.stringify(value)).join("\n") + "\n");

      assert.deepEqual(villageEvidence, {state:"working",doing:"writing the journal",home:5,
        place:null,propVisible:true,parts:6,
        partKinds:["Rectangle","Rectangle","Rectangle","Rectangle","Rectangle","Arc"],
        glow:true,
        aria:"Routine Keeper, writing the journal at home beside an illuminated desk, paper, and lamp. Open details"});
      assert.notEqual(after.data, before.data,
        "the production screenshot must contain a visible journal writing-state change");
      assert.match(panels.detailText,/wrote the journal for 2025-01-15 after close-of-day/);
      assert.match(panels.detailText,/contents are not yet refreshed/);
      assert.match(panels.activity,/wrote the journal for 2025-01-15 after close-of-day/);
      assert.match(panels.directory,/observed written 2025-01-15/);
      assert.equal(panels.pathText,observed.payload.path);
      assert.equal(panels.pathLinked,false,"journal evidence path is escaped text, never navigation");
      assert.equal(panels.mismatch,true);
      assert.deepEqual(expired,{villager:false,prop:false,retained:1});
    });

    await t.test("Fleet reports every retained journal collision under malformed pressure", async () => {
      const journalPressure=[];
      for(let index=0;index<40;index++) {
        const day=new Date(Date.UTC(2024,10,1+index)).toISOString().slice(0,10);
        const canonical={v:0,ts:new Date(frozenNow-index).toISOString(),source:"steward",
          agent_id:"codex:journal-layout",project:"quiet-project",type:"journal_written",
          payload:{routine:"close-of-day",day,path:`/journal/${day}.md`}};
        const conflict={...canonical,payload:{...canonical.payload,routine:"nightly"}};
        journalPressure.push(canonical,conflict);
      }
      for(let index=0;index<45;index++) {
        const malformed={...journalPressure[0],source:"codex",
          ts:new Date(frozenNow-100-index).toISOString()};
        journalPressure.push(malformed);
      }
      fs.writeFileSync(events,[currentEvent,...journalPressure]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
      await resetPage(); await waitForTelemetry();
      const evidence=await eventually(send,`(async()=>{
        await runtime.refreshResidents(); await runtime.poll();
        const state=runtime.snapshot().journalState;
        if(state.records.size!==40||state.malformed!==45)return false;
        if(!panelEl.classList.contains('open'))document.querySelector('#fleet-open').click();
        document.querySelector('[data-fleet-tab="activity"]').click();
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const block=[...document.querySelectorAll('#panel-body .ledger-state.bad')]
          .find(item=>item.textContent.includes('Journal diagnostics'));
        if(!block)return false;
        const rows=[...block.querySelectorAll('li')];
        return {records:state.records.size,malformed:state.malformed,
          collisions:state.collisionDiagnostics.length,
          malformedDetails:state.malformedDiagnostics.length,
          combined:state.diagnostics.length,
          collisionRows:rows.filter(item=>item.textContent.includes('journal day collision')).length,
          rows:rows.length,summary:block.firstChild.textContent,
          malformedCopy:[...document.querySelectorAll('#panel-body .ledger-state.bad')]
            .some(item=>item.textContent.includes('45 malformed journal observations were ignored'))};
      })()`,signal,"journal collision diagnostic pressure");
      assert.deepEqual(evidence,{records:40,malformed:45,collisions:40,
        malformedDetails:40,combined:80,collisionRows:40,rows:80,
        summary:"Journal diagnostics (40 retained collisions; 40 most recent malformed details of 45):",
        malformedCopy:true});
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
    });

    await t.test("Claude and Codex child journal observations remain explicit Fleet-only visitor facts", async () => {
      const lineage=(agent,parent,offset)=>({v:0,ts:new Date(frozenNow+offset).toISOString(),
        source:agent.startsWith("claude-code:")?"claude-code":"codex",agent_id:agent,
        project:"quiet-project",type:"tool_called",
        payload:{tool:"Read",parent_agent_id:parent}});
      const written=(agent,day,offset)=>({v:0,ts:new Date(frozenNow+offset).toISOString(),
        source:"steward",agent_id:agent,project:"quiet-project",type:"journal_written",
        payload:{routine:"close-of-day",day,path:`/journal/${day}.md`}});
      const claudeLineage=lineage("claude-code:journal-child","claude-code:root",-3);
      const claudeJournal=written("claude-code:journal-child","2025-01-14",-2);
      const codexJournal=written("codex:journal-child","2025-01-15",-1);
      const codexLineage=lineage("codex:journal-child","codex:root",0);
      fs.writeFileSync(events,[currentEvent,claudeLineage,claudeJournal,codexJournal,codexLineage]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
      await resetPage(); await waitForTelemetry();
      const evidence=await eventually(send,`(async()=>{
        await runtime.refreshResidents(); await runtime.poll();
        const snapshot=runtime.snapshot();
        const children=['claude-code:journal-child','codex:journal-child'].map(id=>{
          const item=snapshot.villagers.find(value=>value.id===id);
          return item&&{id:item.id,residency:item.residency,home:item.home,journal:item.journal};
        });
        if(children.some(item=>!item))return false;
        document.querySelector('#fleet-open').click();
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const activity=document.querySelector('#panel-body').textContent;
        const activityVisitorCount=[...document.querySelectorAll('.ledger-entry .identity-warning')]
          .filter(item=>item.textContent.includes('visitor child of')).length;
        document.querySelector('[data-fleet-tab="residents"]').click();
        await new Promise(resolve=>requestAnimationFrame(resolve));
        const directory=document.querySelector('#panel-body').textContent;
        return {children,diagnostics:snapshot.journalState.ownershipDiagnostics.map(item=>item.reason),
          homeWritingProp:scene.journalProps[5].visible,
          activityVisitorCount,
          directoryClaimsObserved:directory.includes('observed written 2025-01-14')||
            directory.includes('observed written 2025-01-15')};
      })()`,signal,"child journal Fleet-only ownership");
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
      assert.deepEqual(evidence.children,[
        {id:"claude-code:journal-child",residency:"visitor",home:null,journal:null},
        {id:"codex:journal-child",residency:"visitor",home:null,journal:null},
      ]);
      assert.equal(evidence.diagnostics.length,2);
      assert.ok(evidence.diagnostics.every(reason=>reason.includes("visitor child of")));
      assert.equal(evidence.homeWritingProp,false);
      assert.equal(evidence.activityVisitorCount,2);
      assert.equal(evidence.directoryClaimsObserved,false);
    });

    await t.test("Fleet Activity applies one append-ordered newest-200 cap after journal merge", async () => {
      await resetPage();
      await waitForTelemetry();
      const ordinary=Array.from({length:200},(_,index)=>({v:0,
        ts:new Date(frozenNow+(index%2)).toISOString(),source:"codex",
        agent_id:`codex:activity-${index}`,project:index%2?"odd":"even",
        type:"tool_called",payload:{tool:"Read",index}}));
      const journals=Array.from({length:40},(_,index)=>{
        const date=new Date(Date.UTC(2025,0,1+index)).toISOString().slice(0,10);
        return {v:0,ts:new Date(frozenNow-100000-index).toISOString(),source:"steward",
          agent_id:`codex:journal-history-${index}`,project:index%2?"odd":"even",
          type:"journal_written",payload:{routine:"close-of-day",day:date,
            path:`/journal/${date}.md`}};
      });
      fs.writeFileSync(events,[...ordinary,...journals].map(value=>JSON.stringify(value)).join("\n")+"\n");
      const unfiltered=await eventually(send,`(async()=>{
        await runtime.poll();
        if(!panelEl.classList.contains('open'))document.querySelector('#fleet-open').click();
        await new Promise(r=>requestAnimationFrame(r));
        const rows=[...document.querySelectorAll('#panel-body .ledger-list .ledger-entry')];
        return rows.length===200&&{
          rows:rows.length,journals:rows.filter(row=>row.textContent.includes('journal_written')).length,
          ordinary:rows.filter(row=>row.textContent.includes('tool_called')).length,
          first:rows[0]?.textContent.includes('journal_written'),
          last:rows.at(-1)?.textContent.includes('tool_called'),
          status:document.querySelector('#panel-status').textContent,
          uniqueEvidence:new Set(fleetActivityEntries().map(entry=>entry.event)).size===200};
      })()`,signal,"globally capped Fleet activity");
      const filtered=await eventually(send,`(()=>{
        const select=document.querySelector('#panel-body select[name="source"]');
        select.value='steward';select.dispatchEvent(new Event('input',{bubbles:true}));
        const rows=[...document.querySelectorAll('#panel-body .ledger-list .ledger-entry')];
        return rows.length===40&&{rows:rows.length,
          journals:rows.every(row=>row.textContent.includes('journal_written')),
          status:document.querySelector('#panel-status').textContent};
      })()`,signal,"filtered Fleet count labels");
      assert.deepEqual(unfiltered,{rows:200,journals:40,ordinary:160,first:true,last:true,
        status:"Activity: 200 items.",uniqueEvidence:true});
      assert.deepEqual(filtered,{rows:40,journals:true,
        status:"Activity: 40 items."});
      fs.writeFileSync(events,[currentEvent,agedEvent,agedAttention]
        .map(value=>JSON.stringify(value)).join("\n")+"\n");
    });

    await t.test("Run Now refuses unavailable telemetry before credentials or network", async () => {
      await resetPage();
      await waitForTelemetry();
      const result = await eventually(send, `(async () => {
        await runtime.refreshResidents();
        document.querySelector('#fleet-open').click();
        document.querySelector('[data-fleet-tab="routines"]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        [...document.querySelectorAll('.ledger-entry')].find(item => item.textContent.includes('daily-summary'))
          .querySelector('[data-agent]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        let fetchCalls = 0;
        window.fetch = async () => { fetchCalls++; throw new Error('must not fetch'); };
        const originalSnapshot = runtime.snapshot;
        runtime.snapshot = () => ({...originalSnapshot(),transport:'disconnected'});
        fleetView = {...fleetView,transport:'disconnected'};
        renderPanel(Date.now());
        document.querySelector('[data-run-routine]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        const button = document.querySelector('[data-run-routine]');
        return {fetchCalls,dialogOpen:document.querySelector('#steward-auth').open,
          disabled:button.disabled,
          status:button.parentElement.querySelector('[role="status"]').textContent};
      })()`, signal, "unavailable telemetry Run Now gate");
      assert.deepEqual(result, {fetchCalls:0,dialogOpen:false,disabled:true,
        status:"Run Now unavailable: telemetry is disconnected; no request was sent"});
    });

    await t.test("Run Now waits for a cached-cursor recovery to publish successfully", async () => {
      await resetPage();
      await waitForTelemetry();
      const result = await eventually(send, `(async () => {
        await runtime.refreshResidents();
        document.querySelector('#fleet-open').click();
        document.querySelector('[data-fleet-tab="routines"]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        [...document.querySelectorAll('.ledger-entry')]
          .find(item => item.textContent.includes('daily-summary'))
          .querySelector('[data-agent]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));

        const cachedCursor = runtime.snapshot().cursor;
        let eventCall = 0, finishPending, failPending;
        const response = () => ({ok:true,
          headers:{get:name => name === 'X-Burrow-Cursor' ? cachedCursor : null},
          text:async()=>''});
        const gateRuntime = BurrowBrowser.createBrowserRuntime({
          now:()=>Date.now(), EventSource:null, setTimeout(){}, clearTimeout(){},
          fetch(url) {
            if (url === '/villagers') return Promise.resolve({ok:false});
            eventCall++;
            if (eventCall === 1) return Promise.resolve(response());
            if (eventCall === 2) return new Promise(resolve => { finishPending = resolve; });
            return new Promise((resolve, reject) => { failPending = reject; });
          },
        });
        await gateRuntime.poll();
        const originalSnapshot = runtime.snapshot;
        runtime.snapshot = () => gateRuntime.snapshot();
        let stewardCalls = 0;
        window.fetch = async () => { stewardCalls++; throw new Error('must not fetch'); };

        const setGateView = () => {
          const snapshot = gateRuntime.snapshot();
          fleetView = {...fleetView,transport:snapshot.transport,cursor:snapshot.cursor};
          renderPanel(Date.now());
        };
        const pendingPoll = gateRuntime.poll();
        setGateView();
        let button = document.querySelector('[data-run-routine]');
        const pending = {transport:gateRuntime.snapshot().transport,disabled:button.disabled,
          reason:button.parentElement.querySelector('[role="status"]')?.textContent};
        button.click();

        finishPending(response());
        await pendingPoll;
        setGateView();
        button = document.querySelector('[data-run-routine]');
        const successful = {transport:gateRuntime.snapshot().transport,disabled:button.disabled};

        const failedPoll = gateRuntime.poll();
        setGateView();
        button = document.querySelector('[data-run-routine]');
        const failing = {transport:gateRuntime.snapshot().transport,disabled:button.disabled,
          reason:button.parentElement.querySelector('[role="status"]')?.textContent};
        button.click();
        failPending(new Error('offline'));
        await failedPoll;
        setGateView();
        button = document.querySelector('[data-run-routine]');
        const failed = {transport:gateRuntime.snapshot().transport,disabled:button.disabled,
          reason:button.parentElement.querySelector('[role="status"]')?.textContent};
        runtime.snapshot = originalSnapshot;
        return {pending,successful,failing,failed,stewardCalls,
          dialogOpen:document.querySelector('#steward-auth').open};
      })()`, signal, "pending polling recovery Run Now gate");
      assert.deepEqual(result, {
        pending:{transport:"recovering",disabled:true,
          reason:"Run Now unavailable: telemetry is recovering; no request was sent"},
        successful:{transport:"polling",disabled:false},
        failing:{transport:"recovering",disabled:true,
          reason:"Run Now unavailable: telemetry is recovering; no request was sent"},
        failed:{transport:"disconnected",disabled:true,
          reason:"Run Now unavailable: telemetry is disconnected; no request was sent"},
        stewardCalls:0,dialogOpen:false,
      });
    });

    await t.test("Run Now aborts if telemetry resets while credentials are open", async () => {
      await resetPage();
      await waitForTelemetry();
      const result = await eventually(send, `(async () => {
        await runtime.refreshResidents();
        document.querySelector('#fleet-open').click();
        document.querySelector('[data-fleet-tab="routines"]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        [...document.querySelectorAll('.ledger-entry')]
          .find(item => item.textContent.includes('daily-summary'))
          .querySelector('[data-agent]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        let gets = 0, posts = 0;
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (url, options = {}) => {
          if (!String(url).startsWith('http://127.0.0.1:8801/')) return originalFetch(url, options);
          if (options.method === 'POST') { posts++; throw new Error('must not POST'); }
          gets++;
          return {status:200,json:async()=>({routines:[{resident:'life-agent',
            routine:'daily-summary',next_fire:null,enabled:true,retired:false}]})};
        };
        document.querySelector('[data-run-routine]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        const originalSnapshot = runtime.snapshot;
        const resetCursor = originalSnapshot().cursor.split(':');
        resetCursor[4] = String(BigInt(resetCursor[4]) + 1n);
        runtime.snapshot = () => ({...originalSnapshot(),cursor:resetCursor.join(':')});
        document.querySelector('#steward-token').value = 'reset-secret';
        document.querySelector('#steward-auth button[value="connect"]').click();
        let alert;
        for (let frame = 0; frame < 60 && !alert; frame++) {
          await new Promise(resolve => requestAnimationFrame(resolve));
          alert = document.querySelector('[data-run-routine]')?.parentElement
            .querySelector('[role="alert"]')?.textContent;
        }
        return {gets,posts,authOpen:document.querySelector('#steward-auth').open,alert};
      })()`, signal, "dialog telemetry reset gate");
      assert.deepEqual(result, {gets:1,posts:0,authOpen:false,
        alert:"Run Now unavailable: telemetry changed while authorizing; no request was sent"});
    });

    await t.test("Run Now aborts if telemetry resets while declarations are pending", async () => {
      await resetPage();
      await waitForTelemetry();
      const result = await eventually(send, `(async () => {
        await runtime.refreshResidents();
        document.querySelector('#fleet-open').click();
        document.querySelector('[data-fleet-tab="routines"]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        [...document.querySelectorAll('.ledger-entry')]
          .find(item => item.textContent.includes('daily-summary'))
          .querySelector('[data-agent]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        let gets = 0, posts = 0, resolveGet, signalGet;
        const getStarted = new Promise(resolve => { signalGet = resolve; });
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (url, options = {}) => {
          if (!String(url).startsWith('http://127.0.0.1:8801/')) return originalFetch(url, options);
          if (options.method === 'POST') { posts++; throw new Error('must not POST'); }
          gets++; signalGet();
          return new Promise(resolve => { resolveGet = () => resolve({status:200,
            json:async()=>({routines:[{resident:'life-agent',routine:'daily-summary',
              next_fire:null,enabled:true,retired:false}]})}); });
        };
        document.querySelector('[data-run-routine]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        document.querySelector('#steward-token').value = 'pending-secret';
        document.querySelector('#steward-auth button[value="connect"]').click();
        await getStarted;
        const originalSnapshot = runtime.snapshot;
        const resetCursor = originalSnapshot().cursor.split(':');
        resetCursor[4] = String(BigInt(resetCursor[4]) + 1n);
        runtime.snapshot = () => ({...originalSnapshot(),cursor:resetCursor.join(':')});
        resolveGet();
        let alert;
        for (let frame = 0; frame < 60 && !alert; frame++) {
          await new Promise(resolve => requestAnimationFrame(resolve));
          alert = document.querySelector('[data-run-routine]')?.parentElement
            .querySelector('[role="alert"]')?.textContent;
        }
        return {gets,posts,alert};
      })()`, signal, "pending declaration telemetry reset gate");
      assert.deepEqual(result, {gets:1,posts:0,
        alert:"Run Now unavailable: telemetry changed while authorizing; no request was sent"});
    });

    await t.test("first Run Now click rechecks authoritative declarations and a second click cannot overlap", async () => {
      await resetPage();
      await waitForTelemetry();
      const evidence = await eventually(send, `(async () => {
        await runtime.refreshResidents();
        const openRoutine = async () => {
          document.querySelector('#fleet-open').click();
          document.querySelector('[data-fleet-tab="routines"]').click();
          await new Promise(resolve => requestAnimationFrame(resolve));
          const row = [...document.querySelectorAll('.ledger-entry')]
            .find(item => item.textContent.includes('daily-summary'));
          row.querySelector('[data-agent]').click();
          await new Promise(resolve => requestAnimationFrame(resolve));
        };
        await openRoutine();
        window.__stewardCalls = [];
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (url, options) => {
          if (!String(url).startsWith('http://127.0.0.1:8801/')) return originalFetch(url, options);
          window.__stewardCalls.push({ url:String(url), method:options.method });
          if (String(url).endsWith('/routines')) return {status:200,json:async()=>({routines:[{
            resident:'life-agent',routine:'daily-summary',next_fire:null,enabled:false,retired:false
          }]})};
          return {status:202,json:async()=>({status:'accepted',request_id:'must-not-run'})};
        };
        document.querySelector('[data-run-routine]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        document.querySelector('#steward-token').value = 'disabled-secret';
        document.querySelector('#steward-auth button[value="connect"]').click();
        await new Promise(resolve => setTimeout(resolve, 20));
        const disabledFirstClick = {
          calls: window.__stewardCalls.map(call => call.method),
          disabled: document.querySelector('[data-run-routine]').disabled
        };

        return disabledFirstClick;
      })()`, signal, "disabled first-click authoritative gate");
      assert.deepEqual(evidence, {calls:["GET"],disabled:true});
      await resetPage();
      await waitForTelemetry();
      const result = await eventually(send, `(async () => {
        await runtime.refreshResidents();
        document.querySelector('#fleet-open').click();
        document.querySelector('[data-fleet-tab="routines"]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        [...document.querySelectorAll('.ledger-entry')].find(item => item.textContent.includes('daily-summary'))
          .querySelector('[data-agent]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        window.__postCalls = 0; let resolvePost;
        const originalFetch = window.fetch.bind(window);
        window.fetch = async (url, options) => {
          if (!String(url).startsWith('http://127.0.0.1:8801/')) return originalFetch(url, options);
          if (String(url).endsWith('/routines')) return {status:200,json:async()=>({routines:[{
            resident:'life-agent',routine:'daily-summary',next_fire:'2026-08-26T07:00:00Z',
            enabled:true,retired:false
          }]})};
          window.__postCalls++;
          return new Promise(resolve => { resolvePost = () => resolve({status:202,
            json:async()=>({status:'accepted',request_id:'q-one'})}); });
        };
        document.querySelector('[data-run-routine]').click();
        await new Promise(resolve => requestAnimationFrame(resolve));
        document.querySelector('#steward-token').value = 'enabled-secret';
        document.querySelector('#steward-auth button[value="connect"]').click();
        while (window.__postCalls !== 1) await new Promise(resolve => setTimeout(resolve, 5));
        const duringPost = document.querySelector('[data-run-routine]');
        const disabledWhileRequesting = duringPost.disabled;
        duringPost.click();
        await new Promise(resolve => setTimeout(resolve, 10));
        const callsAfterSecondClick = window.__postCalls;
        resolvePost();
        await new Promise(resolve => setTimeout(resolve, 20));
        const pendingButton = document.querySelector('[data-run-routine]');
        const disabledWhilePending = pendingButton.disabled;
        pendingButton.click();
        await new Promise(resolve => setTimeout(resolve, 10));
        const timeoutAt = Date.now() + BurrowRoutines.DEFAULT_ACK_MS;
        routineAcks.observe({events:[],cursor:runtime.snapshot().cursor}, timeoutAt, validateEvent);
        renderPanel(timeoutAt);
        const unacknowledgedButton = document.querySelector('[data-run-routine]');
        const disabledWhileUnacknowledged = unacknowledgedButton.disabled;
        const unacknowledgedMessage = unacknowledgedButton.parentElement
          .querySelector('[role="status"]').textContent;
        unacknowledgedButton.click();
        await new Promise(resolve => setTimeout(resolve, 10));
        const distantFuture = timeoutAt + 365 * 24 * 60 * 60 * 1000;
        routineAcks.observe({events:[],cursor:runtime.snapshot().cursor}, distantFuture, validateEvent);
        renderPanel(distantFuture);
        const uncertainButton = document.querySelector('[data-run-routine]');
        return { disabledWhileRequesting, callsAfterSecondClick,
          disabledWhilePending, disabledWhileUnacknowledged, unacknowledgedMessage,
          callsAfterUnacknowledgedClick:window.__postCalls,
          disabledInDistantFuture:uncertainButton.disabled,
          uncertainMessage:uncertainButton.parentElement.querySelector('[role="status"]').textContent,
          finalPostCalls:window.__postCalls };
      })()`, signal, "enabled first-click and second-click overlap gate");
      assert.deepEqual(result, { disabledWhileRequesting:true,callsAfterSecondClick:1,
        disabledWhilePending:true,disabledWhileUnacknowledged:true,
        unacknowledgedMessage:"steward acknowledged nothing — outcome remains uncertain; retry unavailable until exact lifecycle evidence arrives",
        callsAfterUnacknowledgedClick:1,disabledInDistantFuture:true,
        uncertainMessage:"steward acknowledged nothing — outcome remains uncertain; retry unavailable until exact lifecycle evidence arrives",
        finalPostCalls:1 });
    });

    await t.test("dialog transition races and native Tab trapping remain bounded", async () => {
      await resetPage();
      await eventually(send, "document.querySelector('#fleet-open').click(); " +
        "document.querySelector('#panel.open.ledger') && true");
    const transitionStart = await eventually(send, `(() => {
      const dialog = document.querySelector('#panel');
      dialog.querySelector('.close').click();
      return !dialog.classList.contains('open') && !dialog.hidden;
    })()`);
    assert.equal(transitionStart, true, "close removes open before transition hides the dialog");
    await eventually(send, "document.querySelector('#panel').hidden");
    const reopenRace = await eventually(send, `(() => {
      const launcher = document.querySelector('#fleet-open'), dialog = document.querySelector('#panel');
      launcher.click();
      requestAnimationFrame(() => dialog.querySelector('.close').click());
      requestAnimationFrame(() => requestAnimationFrame(() => launcher.click()));
      return true;
    })()`);
    assert.equal(reopenRace, true);
    await eventually(send, "!document.querySelector('#panel').hidden && document.querySelector('#panel').classList.contains('open')");

    await send("Runtime.evaluate", { expression: `(() => {
      const dialog = document.querySelector('#panel');
      dialog.insertAdjacentHTML('beforeend', '<div id="focus-fixtures"><button hidden>hidden</button><button disabled>disabled</button><span inert tabindex="0">inert</span><span tabindex="-1">negative</span><span id="trap-last" tabindex="0">last</span></div>');
    })()` });
    await send("Runtime.evaluate", { expression: "document.querySelector('#trap-last').focus()" });
    const beforeTab = await send("Runtime.evaluate", { expression: "({active: document.activeElement.id, last: dialogFocusable().at(-1).id, first: dialogFocusable()[0].className})", returnByValue: true });
    assert.deepEqual(beforeTab.result.value, { active: "trap-last", last: "trap-last", first: "close" });
    await pressTab(send);
    let trapped = await send("Runtime.evaluate", { expression: "({wrapped: document.activeElement === document.querySelector('#panel .close'), active: document.activeElement.outerHTML})", returnByValue: true });
    assert.equal(trapped.result.value.wrapped, true, `native forward Tab wraps from the final focusable control; active=${trapped.result.value.active}`);
    await pressTab(send, true);
    trapped = await send("Runtime.evaluate", { expression: "({wrapped: document.activeElement.id === 'trap-last', active: document.activeElement.tagName + '#' + document.activeElement.id})", returnByValue: true });
    assert.equal(trapped.result.value.wrapped, true, `native reverse Tab wraps from the first focusable control; active=${trapped.result.value.active}`);
    await send("Runtime.evaluate", { expression: "document.querySelector('#focus-fixtures').remove()" });
    });

    await t.test("dialog controls expose accessible semantics, contrast, and focus restoration", async () => {
      await resetPage();
      await eventually(send,
        "!!document.querySelector('button.chip[data-panel-target]:not([data-panel-target=\"notice-board\"])')",
        signal, "accessibility production villager button readiness");
      await eventually(send, "document.querySelector('#fleet-open').click(); " +
        "document.querySelector('#panel.open.ledger') && true");
    const launcherAccessibility = await eventually(send, `(async () => {
      const close = document.querySelector('#panel .close');
      close.click(); await new Promise(resolve => setTimeout(resolve, 250));
      const launcher = document.querySelector('#fleet-open');
      const launcherRestored = document.activeElement === launcher;
      launcher.click(); await new Promise(resolve => requestAnimationFrame(resolve));
      const initialFocus = document.activeElement === close;
      const dialog = document.querySelector('#panel');
      const tabs = [...dialog.querySelectorAll('[role="tab"]')];
      const controls = [...dialog.querySelectorAll('button, input, select')].filter(el => !el.disabled);
      const named = controls.every(el => (el.getAttribute('aria-label') || el.textContent || '').trim());
      const focusRing = (() => { tabs[0].focus(); const css = getComputedStyle(tabs[0]);
        return parseFloat(css.outlineWidth) >= 3 && css.outlineStyle !== 'none'; })();
      const rgb = value => (value.match(/[\\d.]+/g) || []).slice(0, 3).map(Number);
      const luminance = value => { const c = rgb(value).map(v => v / 255).map(v =>
        v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
        return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]; };
      const contrast = (a, b) => { const x = luminance(a), y = luminance(b);
        return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); };
      const contrastRatio = contrast(getComputedStyle(dialog).color, getComputedStyle(dialog).backgroundColor);
      return { launcherRestored, initialFocus, named, focusRing, contrastRatio,
        villagerButtons: document.querySelectorAll('.chip[data-panel-target]:not([data-panel-target="notice-board"])').length,
        boardRole: document.querySelector('[data-panel-target="notice-board"]').tagName,
        tabRoles: tabs.map(tab => tab.getAttribute('role')) };
    })()`, signal, "accessibility launcher semantics and contrast");

    const escapeAccessibility = await eventually(send, `(async () => {
      const dialog = document.querySelector('#panel');
      const close = dialog.querySelector('.close');
      const launcher = document.querySelector('#fleet-open');
      close.focus(); close.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await new Promise(resolve => setTimeout(resolve, 250));
      const escapeRestored = document.activeElement === launcher && dialog.hidden;
      return { escapeRestored };
    })()`, signal, "accessibility Escape focus restoration");

    const boardAccessibility = await eventually(send, `(async () => {
      const dialog = document.querySelector('#panel');
      const close = dialog.querySelector('.close');
      const board = document.querySelector('[data-panel-target="notice-board"]');
      board.focus(); board.click(); await new Promise(resolve => requestAnimationFrame(resolve));
      const boardDialog = dialog.getAttribute('aria-labelledby') === 'panel-title' &&
        document.activeElement === close && dialog.getAttribute('aria-modal') === 'true';
      close.click(); await new Promise(resolve => setTimeout(resolve, 250));
      const boardRestored = document.activeElement === board;
      return { boardDialog, boardRestored };
    })()`, signal, "accessibility notice-board focus restoration");

    const fleetAccessibility = await eventually(send, `(async () => {
      const dialog = document.querySelector('#panel');
      const close = dialog.querySelector('.close');
      const launcher = document.querySelector('#fleet-open');
      launcher.click(); await new Promise(resolve => requestAnimationFrame(resolve));
      const row = document.querySelector('.ledger-entry [data-agent]');
      const rowKey = row.dataset.fleetFocus; row.focus(); row.click();
      await new Promise(resolve => requestAnimationFrame(resolve));
      close.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await new Promise(resolve => requestAnimationFrame(resolve));
      const detailReturnedToLedger = !dialog.hidden && dialog.classList.contains('ledger') &&
        document.activeElement.dataset.fleetFocus === rowKey;
      close.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await new Promise(resolve => setTimeout(resolve, 250));
      const ledgerRestored = dialog.hidden && document.activeElement === launcher;
      return { detailReturnedToLedger, ledgerRestored,
        backgroundExposed: document.querySelector('header').inert };
    })()`, signal, "accessibility fleet detail and ledger focus restoration");
    const accessibility = { ...launcherAccessibility, ...escapeAccessibility,
      ...boardAccessibility, ...fleetAccessibility };
    assert.equal(accessibility.launcherRestored, true);
    assert.equal(accessibility.initialFocus, true, "dialog close receives initial focus");
    assert.equal(accessibility.named, true, "every rendered fleet control has an accessible name");
    assert.equal(accessibility.focusRing, true, "keyboard focus has a production visible ring");
    assert.ok(accessibility.contrastRatio >= 4.5,
      `dialog text contrast was only ${accessibility.contrastRatio.toFixed(2)}:1`);
    assert.equal(accessibility.escapeRestored, true);
    assert.equal(accessibility.boardDialog, true);
    assert.equal(accessibility.boardRestored, true);
    assert.equal(accessibility.detailReturnedToLedger, true,
      "detail Escape restores the logical fleet row across the rerender");
    assert.equal(accessibility.ledgerRestored, true);
    assert.ok(accessibility.villagerButtons >= 1);
    assert.equal(accessibility.boardRole, "BUTTON");
    assert.deepEqual(accessibility.tabRoles, ["tab", "tab", "tab", "tab"]);
    assert.equal(accessibility.backgroundExposed, false, "background is restored after close");
    });

    await t.test("phone production layout matches the strict platform baseline", async () => {
      fs.writeFileSync(events, [agedAttention, agedEvent, currentEvent]
        .map(value => JSON.stringify(value)).join("\n") + "\n");
      await resetPage();
    const phoneReady = await eventually(send, `(async () => {
      if (!scene || !runtime || !window.__burrow) return false;
      await runtime.poll(); await runtime.refreshResidents(); await document.fonts.ready;
      if (!document.querySelector('#snapshot-motion-freeze')) {
        const style = document.createElement('style'); style.id = 'snapshot-motion-freeze';
        style.textContent = '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}';
        document.head.appendChild(style);
      }
      const dialog = document.querySelector('#panel');
      if (!dialog.hidden) { dialog.querySelector('.close').click(); await new Promise(r => requestAnimationFrame(r)); }
      scene.tweens.killAll();
      for (const viz of scene.viz.values()) {
        viz.cont.setPosition(viz.target.x, viz.target.y); viz.walk = null;
        viz.spr.anims.stop(); viz.spr.setTexture(viz.char + '-idle', D_DOWN);
      }
      scene.events.emit('update'); document.activeElement?.blur();
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const canvas = document.querySelector('#game canvas').getBoundingClientRect();
      return dialog.hidden && innerWidth === 320 && innerHeight === 800 &&
        canvas.width > 300 && document.fonts.check('16px BurrowSnapshot');
    })()`);
    assert.equal(phoneReady, true, "phone snapshot reached its frozen final layout");
    const phoneShot = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
    const phonePng = Buffer.from(phoneShot.data, "base64");
    const phonePixels = pixelEvidence(phonePng, [[0, 0, 320, 250], [0, 250, 320, 800]]);
    assert.equal(phonePixels.width, 320, "phone production snapshot width");
    assert.equal(phonePixels.height, 800, "phone production snapshot height");
    assert.ok(phonePixels.evidence[0].colors > 25 && phonePixels.evidence[0].light > 20,
      `phone chrome lost substantive rendered detail: ${JSON.stringify(phonePixels.evidence)}`);
    assert.ok(phonePixels.evidence[1].colors > 40,
      `phone village became blank/corrupt: ${JSON.stringify(phonePixels.evidence)}`);
    assert.ok(phonePixels.evidence.reduce((sum, region) => sum + region.known["142317"], 0) > 100,
      `phone snapshot lost the canonical village background: ${JSON.stringify(phonePixels.evidence)}`);
    compareSnapshot("phone-320x800", phonePng);
    });

    await t.test("desktop production layout matches the strict platform baseline", async () => {
      await resetPage(1440, 900);
      await eventually(send, `(async () => {
        if (!scene || !runtime || !window.__burrow) return false;
        await runtime.poll(); await runtime.refreshResidents(); await document.fonts.ready;
        const style = document.createElement('style'); style.id = 'snapshot-motion-freeze';
        style.textContent = '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}';
        document.head.appendChild(style);
        scene.tweens.killAll();
        for (const viz of scene.viz.values()) {
          viz.cont.setPosition(viz.target.x, viz.target.y); viz.walk = null;
          viz.spr.anims.stop(); viz.spr.setTexture(viz.char + '-idle', D_DOWN);
        }
        scene.events.emit('update'); document.activeElement?.blur();
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        return innerWidth === 1440 && innerHeight === 900 &&
          document.fonts.check('16px BurrowSnapshot');
      })()`);
    await eventually(send, "window.dispatchEvent(new Event('resize')); " +
      "document.querySelector('#fleet-open').click(); document.querySelector('[data-fleet-tab=activity]').click(); " +
      "document.querySelector('#panel.ledger.open') && true");
    const desktop = await eventually(send, `(() => {
      const canvas = document.querySelector('#game canvas').getBoundingClientRect();
      if (innerWidth !== 1440 || innerHeight !== 900 || canvas.width < 1000 || canvas.height < 800) return false;
      const panel = document.querySelector('#panel').getBoundingClientRect();
      if (panel.left <= 600 || panel.right > 1440 || panel.width > 760) return false;
      return { viewport: [innerWidth, innerHeight], canvas: [canvas.width, canvas.height],
        panel: [panel.left, panel.right, panel.width], overflow: document.documentElement.scrollWidth - innerWidth };
    })()`);
    assert.deepEqual(desktop.viewport, [1440, 900]);
    assert.ok(desktop.canvas[0] >= 1000 && desktop.canvas[1] >= 800,
      `village lost its desktop composition: ${JSON.stringify(desktop.canvas)}`);
    assert.ok(desktop.panel[0] > 600 && desktop.panel[1] <= 1440 && desktop.panel[2] <= 760);
    assert.ok(desktop.overflow <= 0);
    const desktopShot = await send("Page.captureScreenshot", { format: "png", fromSurface: true });
    const png = Buffer.from(desktopShot.data, "base64");
    const desktopPixels = pixelEvidence(png, [[0, 0, 680, 450], [680, 0, 1440, 900]]);
    assert.equal(desktopPixels.width, 1440, "desktop production snapshot width");
    assert.equal(desktopPixels.height, 900, "desktop production snapshot height");
    assert.ok(desktopPixels.evidence[0].colors > 80 && desktopPixels.evidence[0].light > 40,
      `desktop village lost substantive pixels: ${JSON.stringify(desktopPixels.evidence)}`);
    assert.ok(desktopPixels.evidence[1].colors > 25 && desktopPixels.evidence[1].light > 40,
      `desktop ledger lost substantive pixels: ${JSON.stringify(desktopPixels.evidence)}`);
    assert.ok(desktopPixels.evidence[1].known["17130f"] > 1000,
      `desktop snapshot lost the canonical ledger surface: ${JSON.stringify(desktopPixels.evidence)}`);
    compareSnapshot("desktop-1440x900", png);
    });
  } catch (error) {
    testFailure = error;
  } finally {
    const failures = testFailure ? [testFailure] : [];
    const stopped = await Promise.allSettled([
      stop(chrome, { processGroup: true }),
      stop(server),
    ]);
    failures.push(...stopped.filter(result => result.status === "rejected")
      .map(result => result.reason));
    try {
      fs.rmSync(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    } catch (error) {
      failures.push(error);
    }
    if (failures.length === 1) throw failures[0];
    if (failures.length > 1) throw new AggregateError(failures, "layout test and cleanup failed");
  }
});
