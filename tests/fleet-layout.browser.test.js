"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

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

async function stop(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  const waitForExit = timeout => new Promise(resolve => {
    if (child.exitCode !== null || child.signalCode !== null) { resolve(true); return; }
    const timer = setTimeout(() => { child.removeListener("exit", onExit); resolve(false); }, timeout);
    function onExit() { clearTimeout(timer); resolve(true); }
    child.once("exit", onExit);
  });
  child.kill("SIGTERM");
  if (await waitForExit(3000)) return;
  child.kill("SIGKILL");
  if (!await waitForExit(3000)) {
    throw new Error(`child ${child.pid} did not exit after SIGTERM and SIGKILL`);
  }
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
  let chrome;
  try {
    await waitForServer(port, server);
    chrome = spawn(chromeExecutable(), ["--headless=new", "--disable-gpu", "--no-sandbox",
      "--remote-debugging-pipe", `--user-data-dir=${path.join(temporary, "chrome")}`, "about:blank"],
    { stdio: ["ignore", "ignore", "pipe", "pipe", "pipe"] });
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
  } finally {
    const stopped = await Promise.allSettled([stop(chrome), stop(server)]);
    const failure = stopped.find(result => result.status === "rejected");
    if (failure) throw failure.reason;
    fs.rmSync(temporary, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
});
