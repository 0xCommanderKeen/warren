const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync("viewer/index.html", "utf8");
const start = html.indexOf("let souls = [];");
const end = html.indexOf("/* ————— boot", start);

const streams = [];
class FakeEventSource {
  constructor(url) { this.url = url; this.listeners = {}; streams.push(this); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  close() { this.closed = true; }
}

let eventRequests = 0;
let retry;
let finishCatchup;
const response = cursor => ({
  ok: true,
  headers: { get: name => name === "X-Burrow-Cursor" ? cursor : null },
  text: async () => "",
});
const context = {
  Map, Date, Promise, EventSource: FakeEventSource,
  encodeURIComponent,
  setTimeout(fn) { retry = fn; return 1; },
  clearTimeout() {},
  fetch(url) {
    if (url === "/villagers") return Promise.resolve({ ok: false });
    eventRequests += 1;
    if (eventRequests === 2)
      return new Promise(resolve => { finishCatchup = () => resolve(response("1:2:60")); });
    return Promise.resolve(response("1:2:10"));
  },
  // project() parses each batch once and folds it into both the village and the
  // notice board; give it the real parser, or a missing global throws into
  // poll()'s catch and the cursor assertions below pass for the wrong reason.
  parseEvents: require("../viewer/projection.js").parseEvents,
  foldEvents(agents, events) {
    for (const event of events) {
      const seen = agents.get(event.agent_id) || [];
      seen.push(event); agents.set(event.agent_id, seen);
    }
  },
  foldArtifacts: require("../viewer/projection.js").foldArtifacts,
  reduce: agents => [...agents.values()],
  beatEl: {}, renderChrome() {}, scene: null,
};
vm.createContext(context);
vm.runInContext(html.slice(start, end) + `
  this.transport = { poll, connectStream, cursor: () => eventCursor,
    agents: () => agents };
`, context);

(async () => {
  await context.transport.poll();
  context.transport.connectStream();
  assert.equal(streams[0].url, "/events/stream?since=1%3A2%3A10");

  context.transport.poll();
  streams[0].onerror();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(streams[0].closed, true);
  assert.equal(eventRequests, 2, "polling resumes when SSE fails");
  assert.equal(retry, undefined, "reconnect waits for the in-flight poll");

  finishCatchup();
  await new Promise(resolve => setImmediate(resolve));

  retry();
  assert.equal(streams[1].url, "/events/stream?since=1%3A2%3A60");
  streams[1].listeners.reset();
  assert.equal(context.transport.cursor(), 0);
  assert.equal(context.transport.agents().size, 0);
  console.log("SSE resumes by cursor and falls back to polling");
})().catch(error => { console.error(error); process.exitCode = 1; });
