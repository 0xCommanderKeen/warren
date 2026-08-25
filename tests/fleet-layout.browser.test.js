"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const { stop } = require("./browser-process");

const ROOT = path.resolve(__dirname, "..");

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

async function waitForServer(port, process) {
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    if (process.exitCode !== null) throw new Error(`Burrow server exited with ${process.exitCode}`);
    const ready = await new Promise(resolve => {
      const request = http.get(`http://127.0.0.1:${port}/`, response => {
        response.resume(); resolve(response.statusCode === 200);
      });
      request.once("error", () => resolve(false));
    });
    if (ready) return;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error("Burrow server did not become ready");
}

function cdp(chrome) {
  let nextId = 1, buffered = "", sessionId = null;
  const pending = new Map();
  chrome.stdio[4].setEncoding("utf8");
  chrome.stdio[4].on("data", chunk => {
    buffered += chunk;
    for (;;) {
      const end = buffered.indexOf("\0");
      if (end < 0) break;
      const raw = buffered.slice(0, end); buffered = buffered.slice(end + 1);
      if (!raw) continue;
      const message = JSON.parse(raw);
      if (!message.id || !pending.has(message.id)) continue;
      const { resolve, reject } = pending.get(message.id); pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message)); else resolve(message.result);
    }
  });
  const send = (method, params = {}, browser = false) => new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    const message = { id, method, params };
    if (sessionId && !browser) message.sessionId = sessionId;
    chrome.stdio[3].write(JSON.stringify(message) + "\0");
  });
  send.setSession = value => { sessionId = value; };
  return send;
}

async function eventually(evaluate, expression) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const result = await evaluate("Runtime.evaluate", { expression, returnByValue: true,
      awaitPromise: true });
    if (result.result.value) return result.result.value;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error(`browser condition timed out: ${expression}`);
}

test("production fleet ledger has no horizontal overflow at 320px", { timeout: 30000 }, async () => {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "burrow-layout-"));
  const events = path.join(temporary, "events.jsonl");
  const currentEvent = { v: 0, ts: new Date().toISOString(),
    source: "claude-code", agent_id: "layout-resident", project: "burrow",
    type: "tool_called", payload: { tool: "Read",
      detail: "a/very/long/production/path/that/must/wrap/without/forcing/the/ledger/wider/than/the/viewport.md" } };
  const agedEvent = { ...currentEvent, ts: new Date(Date.now() - 13 * 60 * 60 * 1000).toISOString(),
    agent_id: "aged-worker", type: "idle", payload: {} };
  const agedAttention = { ...agedEvent, agent_id: "aged-knocker", type: "needs_human",
    payload: { message: "A retained request still needs a truthful destination" } };
  fs.writeFileSync(events, [currentEvent, agedEvent, agedAttention]
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
  const port = await freePort();
  const server = spawn(process.env.PYTHON || "python3", ["serve.py", String(port)], {
    cwd: ROOT, env: { ...process.env, BURROW_EVENTS: events, BURROW_VILLAGERS: villagers },
    stdio: ["ignore", "ignore", "pipe"],
  });
  let chrome, testFailure;
  try {
    await waitForServer(port, server);
    chrome = spawn(chromeExecutable(), ["--headless=new", "--disable-gpu", "--no-sandbox",
      "--remote-debugging-pipe", `--user-data-dir=${path.join(temporary, "chrome")}`, "about:blank"],
    { detached: process.platform !== "win32",
      stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"] });
    let chromeErrors = "";
    chrome.stderr.on("data", chunk => { chromeErrors += chunk; });
    const send = cdp(chrome);
    const target = await send("Target.createTarget", { url: "about:blank" }, true);
    const attached = await send("Target.attachToTarget",
      { targetId: target.targetId, flatten: true }, true);
    send.setSession(attached.sessionId);
    await send("Emulation.setDeviceMetricsOverride", { width: 320, height: 800,
      deviceScaleFactor: 1, mobile: false });
    await send("Page.enable");
    await send("Runtime.enable");
    await send("Page.navigate", { url: `http://127.0.0.1:${port}/` });
    await eventually(send, "document.readyState === 'complete' && !!window.BurrowFleetController");
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
