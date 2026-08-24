const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const html = fs.readFileSync("viewer/index.html", "utf8");
const start = html.indexOf("let souls = [];");
const end = html.indexOf("/* ————— boot", start);
assert.notEqual(start, -1);
assert.notEqual(end, -1);

let resolveFirst;
const firstResponse = new Promise(resolve => { resolveFirst = resolve; });
let eventRequests = 0;
const eventResponse = cursor => ({
  ok: true,
  headers: { get: name => name === "X-Burrow-Cursor" ? cursor : null },
  text: async () => '{"type":"idle","agent_id":"one"}\n',
});

const context = {
  Map,
  Date,
  Promise,
  fetch(url) {
    if (url === "/villagers") return Promise.resolve({ ok: false });
    eventRequests += 1;
    return eventRequests === 1 ? firstResponse : Promise.resolve(eventResponse("20"));
  },
  foldEvents(agents, lines) {
    const events = agents.get("one") || [];
    events.push(...lines);
    agents.set("one", events);
  },
  reduce: agents => [...agents.values()],
  beatEl: {},
  renderChrome() {},
  scene: null,
};
vm.createContext(context);
vm.runInContext(
  html.slice(start, end) + "\nthis.polling = { poll, getAgents: () => agents };",
  context,
);

(async () => {
  const first = context.polling.poll();
  const overlapping = context.polling.poll();
  resolveFirst(eventResponse("10"));
  await Promise.all([first, overlapping]);

  assert.equal(eventRequests, 1, "a pending poll must suppress an overlapping request");
  assert.equal(context.polling.getAgents().get("one").length, 1);
  console.log("overlapping polls are serialized");
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
