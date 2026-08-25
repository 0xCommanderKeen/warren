"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { processGroupExists, stop } = require("./browser-process");

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

test("cleanup reports a process tree that survives KILL", async () => {
  const child = childProcess();
  await assert.rejects(stop(child, { processGroup: true, timeout: 1,
    killProcess: (_pid, signal) => { if (signal !== 0) child.signals.push(signal); } }),
  /process group 42 did not exit after SIGTERM and SIGKILL/);
  assert.deepEqual(child.signals, ["SIGTERM", "SIGKILL"]);
});
