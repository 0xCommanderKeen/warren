"use strict";

function abortError(signal) {
  return signal?.reason instanceof Error ? signal.reason : new Error("operation aborted", {
    cause: signal?.reason,
  });
}

function delay(milliseconds, signal) {
  if (signal?.aborted) return Promise.reject(abortError(signal));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    function onAbort() {
      clearTimeout(timer);
      reject(abortError(signal));
    }
    signal?.addEventListener("abort", onAbort, { once: true });
  });
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

function cdp(chrome, options = {}) {
  const operationTimeout = options.operationTimeout ?? 10000;
  const defaultSignal = options.signal;
  let nextId = 1, buffered = "", sessionId = null, terminalError = null;
  const pending = new Map();
  const request = chrome.stdio[3], response = chrome.stdio[4];
  const description = (method, params) => {
    if (method !== "Runtime.evaluate" || typeof params.expression !== "string") return method;
    const expression = params.expression.replace(/\s+/g, " ").trim();
    return `${method} (${expression.slice(0, 160)}${expression.length > 160 ? "…" : ""})`;
  };
  const settle = (id, kind, value) => {
    const entry = pending.get(id);
    if (!entry) return;
    pending.delete(id);
    clearTimeout(entry.timer);
    entry.signal?.removeEventListener("abort", entry.onAbort);
    entry[kind](value);
  };
  const fail = error => {
    if (terminalError) return;
    terminalError = error instanceof Error ? error : new Error(String(error));
    for (const [id, entry] of pending) settle(id, "reject", new Error(
      `${entry.operation} failed because Chrome's CDP pipe closed: ${terminalError.message}`,
      { cause: terminalError }));
  };
  // Keep error listeners installed for the lifetime of both streams. A late EPIPE
  // after an exit/close race must be observed even after pending work is rejected.
  request.on("error", fail);
  request.once("close", () => fail(new Error("request stream closed")));
  response.setEncoding("utf8");
  response.on("error", fail);
  response.once("close", () => fail(new Error("response stream closed")));
  response.on("data", chunk => {
    if (terminalError) return;
    buffered += chunk;
    for (;;) {
      const end = buffered.indexOf("\0");
      if (end < 0) break;
      const raw = buffered.slice(0, end); buffered = buffered.slice(end + 1);
      if (!raw) continue;
      let message;
      try { message = JSON.parse(raw); } catch (error) { fail(error); return; }
      if (!message.id || !pending.has(message.id)) continue;
      if (message.error) settle(message.id, "reject", new Error(message.error.message));
      else settle(message.id, "resolve", message.result);
    }
  });
  chrome.once("exit", (code, signal) => fail(
    new Error(`Chrome exited (code=${code}, signal=${signal})`)));
  const send = (method, params = {}, options = {}) => new Promise((resolve, reject) => {
    const browser = options.browser === true;
    const timeout = options.timeout ?? operationTimeout;
    const signal = options.signal ?? defaultSignal;
    const operation = description(method, params);
    if (terminalError) {
      reject(new Error(`${operation} failed because Chrome's CDP pipe closed: ${terminalError.message}`,
        { cause: terminalError }));
      return;
    }
    if (signal?.aborted) { reject(abortError(signal)); return; }
    const id = nextId++;
    const onAbort = () => settle(id, "reject", abortError(signal));
    const timer = setTimeout(() => settle(id, "reject",
      new Error(`${operation} timed out after ${timeout}ms`)), timeout);
    pending.set(id, { resolve, reject, timer, operation, signal, onAbort });
    signal?.addEventListener("abort", onAbort, { once: true });
    const message = { id, method, params };
    if (sessionId && !browser) message.sessionId = sessionId;
    request.write(JSON.stringify(message) + "\0", error => {
      if (error) settle(id, "reject",
        new Error(`${operation} could not be written to Chrome: ${error.message}`, { cause: error }));
    });
  });
  send.setSession = value => { sessionId = value; };
  send.signal = defaultSignal;
  return send;
}

async function stop(child, options = {}) {
  if (!child) return;
  const timeout = options.timeout ?? 3000;
  const killProcess = options.killProcess ?? process.kill;
  const processGroup = options.processGroup === true && process.platform !== "win32";
  const closeStdio = () => {
    // Custom CDP pipes remain referenced by Node even after Chrome exits unless
    // the parent closes its stream endpoints. A green suite otherwise prints
    // all results and idles until the outer CI timeout kills it.
    for (const stream of child.stdio || []) stream?.destroy?.();
  };
  if (!processGroup && exited(child)) { closeStdio(); return; }

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
  if (await wait()) { closeStdio(); return; }
  signal("SIGKILL");
  if (!await wait()) {
    closeStdio();
    const target = processGroup ? `process group ${child.pid}` : `child ${child.pid}`;
    throw new Error(`${target} did not exit after SIGTERM and SIGKILL`);
  }
  closeStdio();
}

module.exports = { abortError, cdp, delay, processGroupExists, stop };
