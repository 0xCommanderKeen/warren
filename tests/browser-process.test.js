"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { PassThrough } = require("node:stream");
const { cdp, delay, processGroupExists, stop } = require("./browser-process");

function childProcess(pid = 42) {
  const child = new EventEmitter();
  child.pid = pid;
  child.exitCode = null;
  child.signalCode = null;
  child.kill = signal => child.signals.push(signal);
  child.signals = [];
  return child;
}

test("process group probing distinguishes a missing group", () => {
  assert.equal(processGroupExists(42, () => { const error = new Error(); error.code = "ESRCH";
    throw error; }), false);
  assert.equal(processGroupExists(42, () => {}), true);
});

test("POSIX cleanup signals the whole group and waits for descendants", async () => {
  const child = childProcess();
  let groupAlive = true;
  const signals = [];
  const killProcess = (pid, signal) => {
    assert.equal(pid, -42);
    if (signal === 0) {
      if (!groupAlive) { const error = new Error(); error.code = "ESRCH"; throw error; }
      return;
    }
    signals.push(signal);
    if (signal === "SIGKILL") groupAlive = false;
  };
  setImmediate(() => { child.signalCode = "SIGTERM"; child.emit("exit"); });

  await stop(child, { processGroup: true, timeout: 5, killProcess });

  assert.deepEqual(signals, ["SIGTERM", "SIGKILL"]);
  assert.deepEqual(child.signals, []);
});

test("non-group cleanup retains the child kill fallback", async () => {
  const child = childProcess();
  child.kill = signal => {
    child.signals.push(signal);
    child.signalCode = signal;
    child.emit("exit");
  };

  await stop(child, { processGroup: false, timeout: 5 });

  assert.deepEqual(child.signals, ["SIGTERM"]);
});

test("cleanup closes parent-side custom stdio after an already-exited child", async () => {
  const child = childProcess();
  child.exitCode = 0;
  child.stdio = [null, null, null, new PassThrough(), new PassThrough()];

  await stop(child, { processGroup: false, timeout: 5 });

  assert.equal(child.stdio[3].destroyed, true);
  assert.equal(child.stdio[4].destroyed, true);
  assert.deepEqual(child.signals, []);
});

test("cleanup reports a process tree that survives KILL", async () => {
  const child = childProcess();
  await assert.rejects(stop(child, { processGroup: true, timeout: 1,
    killProcess: (_pid, signal) => { if (signal !== 0) child.signals.push(signal); } }),
  /process group 42 did not exit after SIGTERM and SIGKILL/);
  assert.deepEqual(child.signals, ["SIGTERM", "SIGKILL"]);
});

function chromeProcess() {
  const chrome = new EventEmitter();
  chrome.stdio = [];
  chrome.stdio[3] = new PassThrough();
  chrome.stdio[4] = new PassThrough();
  return chrome;
}

function nextRequest(chrome) {
  return new Promise(resolve => chrome.stdio[3].once("data", chunk =>
    resolve(JSON.parse(chunk.toString().replace(/\0$/, "")))));
}

test("CDP named options select browser routing and override the timeout", async () => {
  const chrome = chromeProcess();
  const send = cdp(chrome, { operationTimeout: 10000 });
  send.setSession("session-42");

  const browserRequest = nextRequest(chrome);
  const browserPending = send("Target.getTargets", {}, { browser: true });
  const browserMessage = await browserRequest;
  assert.equal(browserMessage.sessionId, undefined);
  chrome.stdio[4].write(JSON.stringify({ id: browserMessage.id, result: { targetInfos: [] } }) + "\0");
  await browserPending;

  const sessionRequest = nextRequest(chrome);
  const sessionPending = send("Page.enable");
  const sessionMessage = await sessionRequest;
  assert.equal(sessionMessage.sessionId, "session-42");
  chrome.stdio[4].write(JSON.stringify({ id: sessionMessage.id, result: {} }) + "\0");
  await sessionPending;

  await assert.rejects(send("Runtime.evaluate", {}, { timeout: 1 }),
    /Runtime\.evaluate timed out after 1ms/);
});

test("abort promptly cancels delays and pending CDP operations", async () => {
  const controller = new AbortController();
  const chrome = chromeProcess();
  const send = cdp(chrome, { signal: controller.signal, operationTimeout: 10000 });
  const started = Date.now();
  const pending = Promise.allSettled([delay(10000, controller.signal), send("Page.enable")]);
  controller.abort(new Error("outer test deadline"));
  const results = await pending;
  assert.ok(Date.now() - started < 500);
  assert.deepEqual(results.map(result => result.status), ["rejected", "rejected"]);
  assert.match(results[0].reason.message, /outer test deadline/);
  assert.match(results[1].reason.message, /outer test deadline/);
});

test("CDP request stream failure rejects all pending once and tolerates races", async () => {
  const chrome = chromeProcess();
  const send = cdp(chrome, { operationTimeout: 10000 });
  const first = send("Page.enable"), second = send("Runtime.enable");
  const failure = new Error("write EPIPE"); failure.code = "EPIPE";
  chrome.stdio[3].emit("error", failure);
  chrome.emit("exit", 1, null);
  chrome.stdio[3].emit("error", new Error("late stream error"));
  chrome.stdio[4].emit("error", new Error("late response error"));
  const results = await Promise.allSettled([first, second]);
  assert.deepEqual(results.map(result => result.status), ["rejected", "rejected"]);
  assert.ok(results.every(result => /write EPIPE/.test(result.reason.message)));
  await assert.rejects(send("Page.navigate"), /write EPIPE/);
});
