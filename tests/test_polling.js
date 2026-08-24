"use strict";

const assert = require("node:assert/strict");
const { createBrowserRuntime } = require("../viewer/browser-runtime.js");

let resolveFirst;
const firstResponse = new Promise(resolve => { resolveFirst = resolve; });
let eventRequests = 0;
const eventResponse = cursor => ({
  ok: true,
  headers: { get: name => name === "X-Burrow-Cursor" ? cursor : null },
  text: async () => '{"v":0,"ts":"2026-08-24T12:00:00.000Z","source":"test","agent_id":"one","project":"burrow","type":"idle","payload":{}}\n',
});
const runtime = createBrowserRuntime({
  now: () => Date.parse("2026-08-24T12:01:00.000Z"), EventSource: null,
  setTimeout() {}, clearTimeout() {},
  fetch(url) {
    if (url === "/villagers") return Promise.resolve({ ok: false });
    eventRequests += 1;
    return eventRequests === 1 ? firstResponse : Promise.resolve(eventResponse("20"));
  },
});

(async () => {
  const first = runtime.poll();
  const overlapping = runtime.poll();
  resolveFirst(eventResponse("10"));
  await Promise.all([first, overlapping]);

  assert.equal(eventRequests, 1, "a pending poll must suppress an overlapping request");
  assert.equal(runtime.snapshot().villagers.length, 1);
  console.log("overlapping polls are serialized");
})().catch(error => { console.error(error); process.exitCode = 1; });
