"use strict";
/* Tests for the burrow projection (viewer/projection.js).
 *
 *     node --test tests/          # or: node tests/projection.test.js
 *
 * No dependencies, no build step: node:test + node:assert only. Every case is
 * driven by a fixture event log in tests/fixtures/*.jsonl, read the same way the
 * viewer reads GET /events (split on newlines, drop empties).
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  reduce, NAMES, CHARS, ACCENTS, STALE_MS, DROP_MS, MAX_EVENTS,
  MAX_ARTIFACTS, foldArtifacts, nameArtifacts,
  describe: describeEvent, doingLabel, ago, esc, hashCode,
} = require("../viewer/projection.js");

/** All fixture timestamps are written relative to this instant. */
const NOW = Date.parse("2026-08-24T12:00:00.000Z");
const MIN = 60 * 1000, HOUR = 60 * MIN;

function log(name) {
  return fs.readFileSync(path.join(__dirname, "fixtures", name), "utf8")
    .split("\n").filter(Boolean);
}
function byId(villagers) {
  return new Map(villagers.map(v => [v.id, v]));
}
function ids(villagers) {
  return villagers.map(v => v.id);
}
/** A soul file as GET /villagers serves it. */
function soul(meta, body) {
  return { file: (meta.name || "anon") + ".md", meta, body: body || "" };
}

describe("windows are the protocol's windows", () => {
  it("stale after 30 minutes, dropped after 12 hours", () => {
    assert.equal(STALE_MS, 30 * MIN);
    assert.equal(DROP_MS, 12 * HOUR);
  });
});

describe("event filtering", () => {
  const village = reduce(log("noise.jsonl"), NOW, []);

  it("ignores unknown event types", () => {
    // the fixture's only known-type events belong to a-worker
    assert.deepEqual(ids(village), ["claude-code:a-worker"]);
    const v = village[0];
    assert.deepEqual(v.events.map(e => e.type), ["tool_called", "idle"]);
    // The later idle signal decides state; heartbeat stays out of visible history.
    assert.equal(v.state, "resting");
  });

  it("keeps a heartbeat-only villager alive but ignores unknown types", () => {
    const projected = reduce([
      '{"ts":"2026-08-24T11:59:00.000Z","agent_id":"x:1","type":"heartbeat","payload":{}}',
      '{"ts":"2026-08-24T11:59:01.000Z","agent_id":"x:1","type":"agent_thought","payload":{}}',
    ], NOW, []);
    assert.equal(projected.length, 1);
    assert.equal(projected[0].state, "working");
    assert.deepEqual(projected[0].events, []);
  });

  it("skips unparseable lines, nulls and events with no agent_id", () => {
    // noise.jsonl holds a bare string, a `null`, an empty type and an
    // agent_id-less event; none of them may crash or create a villager.
    assert.equal(village.length, 1);
    assert.equal(reduce(["", "{", "null", "[]", "7"], NOW, []).length, 0);
  });

  it("survives an empty log", () => {
    assert.deepEqual(reduce([], NOW, []), []);
    assert.deepEqual(reduce([], NOW, undefined), []);
  });
});

describe("notice board artifacts", () => {
  it("lists artifacts most recent first and ignores malformed entries", () => {
    const artifacts = foldArtifacts([], [
      JSON.stringify({ ts: "2026-08-24T11:58:00Z", agent_id: "a", project: "one", type: "artifact_produced", payload: { artifact: "old.md" } }),
      "{",
      JSON.stringify({ ts: "2026-08-24T11:59:00Z", agent_id: "b", project: "two", type: "artifact_produced", payload: { artifact: "new.md" } }),
      JSON.stringify({ ts: "2026-08-24T12:00:00Z", agent_id: "c", type: "artifact_produced", payload: {} }),
    ]);
    assert.deepEqual(artifacts.map(a => a.artifact), ["new.md", "old.md"]);
    assert.deepEqual(artifacts.map(a => a.project), ["two", "one"]);
  });

  it("stays bounded during incremental ingestion", () => {
    const artifacts = [];
    for (let i = 0; i < MAX_ARTIFACTS + 25; i++) {
      foldArtifacts(artifacts, [JSON.stringify({
        ts: new Date(NOW + i).toISOString(), agent_id: "maker", project: "burrow",
        type: "artifact_produced", payload: { artifact: `file-${i}` },
      })]);
      assert.ok(artifacts.length <= MAX_ARTIFACTS);
    }
    assert.equal(artifacts.length, MAX_ARTIFACTS);
    assert.equal(artifacts[0].artifact, `file-${MAX_ARTIFACTS + 24}`);
  });

  it("uses the visible villager name with a stable fallback", () => {
    const input = [{ artifact: "x", agent_id: "known", project: "p", ts: NOW },
                   { artifact: "y", agent_id: "gone", project: "p", ts: NOW - 1 }];
    const named = nameArtifacts(input, [{ id: "known", name: "Maren" }]);
    assert.equal(named[0].name, "Maren");
    assert.equal(named[1].name, NAMES[hashCode("gone") % NAMES.length]);
    const historical = nameArtifacts([input[1]], [], [soul({ project: "p", name: "Vesper" })]);
    assert.equal(historical[0].name, "Vesper");
  });
});

describe("state mapping (docs/protocol.md, projection rules v0)", () => {
  const village = reduce(log("basic.jsonl"), NOW, []);
  const v = byId(village);

  it("maps the latest event to a state", () => {
    assert.equal(v.get("claude-code:a-worker").state, "working");   // tool_called
    assert.equal(v.get("claude-code:b-crafter").state, "working");  // artifact_produced
    assert.equal(v.get("claude-code:c-rester").state, "resting");   // idle
    assert.equal(v.get("claude-code:d-knocker").state, "knocking"); // needs_human
  });

  it("maps task_started to working too", () => {
    const only = reduce(log("basic.jsonl").slice(0, 1), NOW, []);
    assert.equal(only[0].state, "working");
    assert.equal(only[0].doing, "starting a task");
  });

  it("labels what a working villager is doing, and nothing otherwise", () => {
    assert.equal(v.get("claude-code:a-worker").doing, "reading");        // Read
    assert.equal(v.get("claude-code:b-crafter").doing, "crafting");      // artifact
    assert.equal(v.get("cron:e-nameless").doing, "using Telescope");     // unknown tool
    assert.equal(v.get("claude-code:c-rester").doing, "");
    assert.equal(v.get("claude-code:d-knocker").doing, "");
  });

  it("describes the last event in words", () => {
    assert.equal(v.get("claude-code:a-worker").lastLine, "reading — README.md");
    assert.equal(v.get("claude-code:b-crafter").lastLine, "crafted viewer/index.html");
    assert.equal(v.get("claude-code:c-rester").lastLine, "finished, resting");
    assert.equal(v.get("claude-code:d-knocker").lastLine, "needs you: approve the draft?");
  });

  it("keeps the whole history per agent, in log order", () => {
    const worker = v.get("claude-code:a-worker");
    assert.deepEqual(worker.events.map(e => e.type), ["task_started", "tool_called"]);
    assert.equal(worker.lastTs, Date.parse("2026-08-24T11:59:40.000Z"));
  });

  it("carries project and cwd from the latest event, with fallbacks", () => {
    assert.equal(v.get("claude-code:a-worker").project, "burrow");
    assert.equal(v.get("claude-code:a-worker").cwd, "/Users/miha/Work/hobbies/burrow");
    assert.equal(v.get("cron:e-nameless").project, "unknown");
    assert.equal(v.get("cron:e-nameless").cwd, "");
  });

  it("orders villagers by agent_id", () => {
    assert.deepEqual(ids(village), [...ids(village)].sort());
  });

  it("caps an agent's kept history at MAX_EVENTS", () => {
    const lines = [];
    for (let i = 0; i < MAX_EVENTS + 20; i++) {
      lines.push(JSON.stringify({
        ts: new Date(NOW - (MAX_EVENTS + 20 - i) * 1000).toISOString(),
        agent_id: "x:long", project: "burrow", type: "tool_called",
        payload: { tool: "Read", detail: "file" + i },
      }));
    }
    const [long] = reduce(lines, NOW, []);
    assert.equal(long.events.length, MAX_EVENTS);
    // the cap drops the oldest, never the newest
    assert.equal(long.events.at(-1).payload.detail, "file" + (MAX_EVENTS + 19));
    assert.equal(long.events[0].payload.detail, "file20");
  });
});

describe("stale and drop windows", () => {
  const v = byId(reduce(log("windows.jsonl"), NOW, []));

  it("keeps a fresh worker working", () => {
    assert.equal(v.get("t:fresh").state, "working");
  });

  it("goes stale only once past 30 minutes", () => {
    assert.equal(v.get("t:stale-edge-inside").state, "working");   // exactly 30m
    assert.equal(v.get("t:stale-edge-outside").state, "stale");    // 30m + 1ms
  });

  it("never staleness-fades a villager that is resting or knocking", () => {
    // resting and knocking are honest states with no swing to freeze mid-air
    assert.equal(v.get("t:old-idle").state, "resting");
    assert.equal(v.get("t:old-knocker").state, "knocking");
  });

  it("drops a villager only once past 12 hours", () => {
    assert.equal(v.get("t:drop-edge-inside").state, "stale");      // exactly 12h
    assert.equal(v.has("t:drop-edge-outside"), false);             // 12h + 1ms
  });

  it("drops old villagers whatever their state", () => {
    assert.equal(v.has("t:ancient-idle"), false);
    assert.equal(v.has("t:ancient-knocker"), false);
  });

  it("drops an event whose timestamp will not parse", () => {
    assert.equal(v.has("t:unparseable-ts"), false);
  });

  it("moves the windows with `now`", () => {
    const later = byId(reduce(log("windows.jsonl"), NOW + 40 * MIN, []));
    assert.equal(later.get("t:fresh").state, "stale");
    const earlier = byId(reduce(log("windows.jsonl"), NOW - 25 * MIN, []));
    assert.equal(earlier.get("t:stale-edge-outside").state, "working");
  });
});

describe("session_ended", () => {
  const village = reduce(log("session_ended.jsonl"), NOW, []);
  const v = byId(village);

  it("removes the villager whose last event is session_ended", () => {
    assert.equal(v.has("s:gone"), false);
  });

  it("leaves other villagers alone", () => {
    assert.equal(v.get("s:staying").state, "resting");
  });

  it("brings a villager back when a newer event follows the session_ended", () => {
    const back = v.get("s:returned");
    assert.equal(back.state, "working");
    assert.deepEqual(back.events.map(e => e.type),
      ["idle", "session_ended", "task_started"]);
  });
});

describe("soul matching", () => {
  const lines = log("souls.jsonl");
  const maren = soul({ project: "burrow", name: "Maren", char: "Hunter",
                       accent: "#4f7d5b", role: "village builder" },
                     "Works on burrow itself.");
  const vesper = soul({ agent_id: "soul:a-alpha", name: "Vesper", char: "Noble",
                        accent: "#7d5ba6" });

  it("prefers an agent_id soul over a project soul for the same villager", () => {
    const v = byId(reduce(lines, NOW, [maren, vesper]));
    const alpha = v.get("soul:a-alpha");            // matches both souls
    assert.equal(alpha.name, "Vesper");
    assert.equal(alpha.char, "Noble");
    assert.equal(alpha.accent, "#7d5ba6");
    assert.equal(alpha.soul.file, "Vesper.md");
    // …and the project soul is then free for the other burrow villager
    const bravo = v.get("soul:b-bravo");
    assert.equal(bravo.name, "Maren");
    assert.equal(bravo.soul.file, "Maren.md");
  });

  it("hands a soul to one villager only — the rest fall back", () => {
    const v = byId(reduce(lines, NOW, [maren]));
    const alpha = v.get("soul:a-alpha");            // first by agent_id order
    const bravo = v.get("soul:b-bravo");            // same project, no soul left
    assert.equal(alpha.name, "Maren");
    assert.equal(alpha.soul.file, "Maren.md");
    assert.equal(bravo.soul, null);
    assert.ok(NAMES.includes(bravo.name), "fell back to a village name");
    assert.notEqual(bravo.name, "Maren");
  });

  it("leaves villagers with no matching soul on hash identities", () => {
    const v = byId(reduce(lines, NOW, [maren, vesper]));
    for (const id of ["soul:c-charlie", "soul:d-delta"]) {
      const x = v.get(id);
      assert.equal(x.soul, null);
      assert.ok(NAMES.includes(x.name));
      assert.ok(CHARS.includes(x.char));
      assert.ok(ACCENTS.includes(x.accent));
    }
  });

  it("exposes the soul body and role for the panel", () => {
    const v = byId(reduce(lines, NOW, [maren]));
    assert.equal(v.get("soul:a-alpha").soul.body, "Works on burrow itself.");
    assert.equal(v.get("soul:a-alpha").soul.meta.role, "village builder");
  });

  it("keeps the hash identity for fields the soul does not pin", () => {
    const partial = soul({ project: "life", char: "Monk" });   // no name, no accent
    const v = byId(reduce(lines, NOW, [partial]));
    const charlie = v.get("soul:c-charlie");
    assert.equal(charlie.char, "Monk");
    assert.ok(NAMES.includes(charlie.name), "name still comes from the hash");
    assert.ok(ACCENTS.includes(charlie.accent), "accent still comes from the hash");
    assert.notEqual(charlie.soul, null);
  });

  it("ignores a sprite name that is not in the character set", () => {
    const bogus = soul({ project: "life", name: "Ilo", char: "Dragon" });
    const charlie = byId(reduce(lines, NOW, [bogus])).get("soul:c-charlie");
    assert.equal(charlie.name, "Ilo");
    assert.ok(CHARS.includes(charlie.char), "unknown char ignored, hash char kept");
  });

  it("ignores malformed soul entries", () => {
    const v = byId(reduce(lines, NOW, [null, {}, { meta: null }, maren]));
    assert.equal(v.get("soul:a-alpha").name, "Maren");
  });

  it("does not project a soul that matches nobody", () => {
    const orphan = soul({ project: "nowhere", name: "Nobody" });
    const village = reduce(lines, NOW, [orphan]);
    assert.equal(village.filter(x => x.name === "Nobody").length, 0);
    assert.equal(village.length, 4);
  });
});

describe("fallback identities", () => {
  const crowd = reduce(log("crowd.jsonl"), NOW, []);

  it("gives every villager in a full village a distinct name", () => {
    assert.equal(crowd.length, 16);
    assert.equal(new Set(crowd.map(v => v.name)).size, 16);
    assert.equal(NAMES.length, 16, "the probe can only stay unique up to NAMES");
  });

  it("probes sprites too, up to the size of the character set", () => {
    const chars = new Set(crowd.map(v => v.char));
    assert.equal(chars.size, CHARS.length);   // 12 sprites, all used
    assert.ok(crowd.length > CHARS.length, "so some sprites are necessarily reused");
    for (const v of crowd) assert.ok(CHARS.includes(v.char));
  });

  it("is deterministic for the same log", () => {
    const again = reduce(log("crowd.jsonl"), NOW, []);
    assert.deepEqual(again.map(v => [v.id, v.name, v.char, v.accent]),
                     crowd.map(v => [v.id, v.name, v.char, v.accent]));
  });

  it("gives one lonely villager a stable hash identity", () => {
    const [one] = reduce(log("crowd.jsonl").slice(3, 4), NOW, []);
    const h = hashCode(one.id);
    assert.equal(one.name, NAMES[h % NAMES.length]);
    assert.equal(one.char, CHARS[h % CHARS.length]);
    assert.equal(one.accent, ACCENTS[h % ACCENTS.length]);
  });

  it("reuses a name once a soul frees the slot it would have taken", () => {
    // soul names live outside NAMES, so they never starve the probe
    const named = soul({ project: "p3", name: "Maren" });
    const v = reduce(log("crowd.jsonl"), NOW, [named]);
    assert.equal(new Set(v.map(x => x.name)).size, 16);
  });
});

describe("presentation helpers shared with the viewer", () => {
  it("describes every protocol event type", () => {
    const say = (type, payload) => describeEvent({ type, payload });
    assert.match(say("task_started", { prompt: "fix the roof" }), /fix the roof/);
    assert.equal(say("tool_called", { tool: "Grep", detail: "reduce(" }),
                 "searching — reduce(");
    assert.equal(say("artifact_produced", {}), "crafted something");
    assert.equal(say("needs_human", {}), "needs you: (no message)");
    assert.equal(say("idle", {}), "finished, resting");
    assert.equal(say("session_ended", {}), "went home");
    assert.equal(say("heartbeat", {}), "finished a tool");
    assert.equal(doingLabel({ type: "idle", payload: {} }), "");
  });

  it("renders relative times", () => {
    assert.equal(ago(NOW - 5000, NOW), "5s ago");
    assert.equal(ago(NOW - 5 * MIN, NOW), "5m ago");
    assert.equal(ago(NOW - 90 * MIN, NOW), "1.5h ago");
    assert.equal(ago(NOW + 5000, NOW), "0s ago");   // clock skew is not negative time
  });

  it("escapes agent-supplied text before it reaches the DOM", () => {
    assert.equal(esc('<img src=x onerror="alert(1)">'),
                 "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
  });
});
