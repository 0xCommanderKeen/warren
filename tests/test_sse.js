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
    return Promise.resolve(response(eventRequests === 1 ? "1:2:10" : "1:2:60"));
  },
  foldEvents(agents, lines) {
    for (const line of lines) {
      const event = JSON.parse(line);
      const events = agents.get(event.agent_id) || [];
      events.push(event); agents.set(event.agent_id, events);
    }
  },
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

  streams[0].onmessage({
    data: '{"type":"idle","agent_id":"one"}', lastEventId: "1:2:42",
  });
  assert.equal(context.transport.cursor(), "1:2:42");
  assert.equal(context.transport.agents().get("one").length, 1);

  streams[0].onerror();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(streams[0].closed, true);
  assert.equal(eventRequests, 2, "polling resumes when SSE fails");

  retry();
  assert.equal(streams[1].url, "/events/stream?since=1%3A2%3A60");
  streams[1].listeners.reset();
  assert.equal(context.transport.cursor(), 0);
  assert.equal(context.transport.agents().size, 0);
  console.log("SSE resumes by cursor and falls back to polling");
})().catch(error => { console.error(error); process.exitCode = 1; });
