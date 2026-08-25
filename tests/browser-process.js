"use strict";

function delay(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

function exited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

function processGroupExists(pid, killProcess) {
  try {
    killProcess(-pid, 0);
    return true;
  } catch (error) {
    if (error.code === "ESRCH") return false;
    if (error.code === "EPERM") return true;
    throw error;
  }
}

function waitForExit(child, timeout) {
  if (exited(child)) return Promise.resolve(true);
  return new Promise(resolve => {
    const timer = setTimeout(() => {
      child.removeListener("exit", onExit);
      resolve(false);
    }, timeout);
    function onExit() {
      clearTimeout(timer);
      resolve(true);
    }
    child.once("exit", onExit);
  });
}

async function waitForGroupExit(pid, timeout, killProcess) {
  const deadline = Date.now() + timeout;
  while (processGroupExists(pid, killProcess)) {
    if (Date.now() >= deadline) return false;
    await delay(Math.min(25, deadline - Date.now()));
  }
  return true;
}

async function stop(child, options = {}) {
  if (!child) return;
  const timeout = options.timeout ?? 3000;
  const killProcess = options.killProcess ?? process.kill;
  const processGroup = options.processGroup === true && process.platform !== "win32";
  if (!processGroup && exited(child)) return;

  const signal = name => {
    if (processGroup) {
      try {
        killProcess(-child.pid, name);
      } catch (error) {
        if (error.code !== "ESRCH") throw error;
      }
    } else {
      child.kill(name);
    }
  };
  const wait = async () => {
    const results = await Promise.all([
      waitForExit(child, timeout),
      processGroup ? waitForGroupExit(child.pid, timeout, killProcess) : true,
    ]);
    return results.every(Boolean);
  };

  signal("SIGTERM");
  if (await wait()) return;
  signal("SIGKILL");
  if (!await wait()) {
    const target = processGroup ? `process group ${child.pid}` : `child ${child.pid}`;
    throw new Error(`${target} did not exit after SIGTERM and SIGKILL`);
  }
}

module.exports = { processGroupExists, stop };
