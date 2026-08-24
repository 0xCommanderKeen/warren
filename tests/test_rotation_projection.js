/* Rotation must be invisible to the village. This runs the viewer's own
 * reduce() over a log and over the tail serve.py would carry forward, and
 * asserts both project to exactly the same villagers.
 *
 *     node tests/test_rotation_projection.js        (from the repo root)
 */
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { reduce } = require("../viewer/projection.js");

const NOW = Date.parse("2026-08-24T12:00:00.000Z");
const at = (minutesAgo) => new Date(NOW - minutesAgo * 60000).toISOString();
const ev = (agent, type, minutesAgo, payload = {}) => JSON.stringify({
  v: 0, ts: at(minutesAgo), source: "test", agent_id: agent,
  project: "burrow", cwd: "/tmp", type, payload,
});

const lines = [];
for (let i = 0; i < 30; i++) {
  lines.push(ev("departed", "tool_called", 300, { tool: "Read", detail: "x".repeat(150), n: i }));
  lines.push(ev("ancient", "tool_called", 60 * 30, { tool: "Grep", detail: "y".repeat(150), n: i }));
  lines.push(ev("live-1", "tool_called", 240 - i, { tool: "Bash", detail: "make " + i }));
}
lines.push(ev("departed", "session_ended", 200));
lines.push(ev("ancient", "idle", 60 * 29));
lines.push(ev("live-1", "task_started", 5, { prompt: "the current task" }));
lines.push(ev("live-2", "needs_human", 2, { message: "a question" }));
lines.push(ev("live-3", "idle", 45));
lines.push(ev("live-4", "tool_called", 40, { tool: "Write", detail: "notes.md" }));

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "burrow-rot-"));
const logPath = path.join(dir, "events.jsonl");
fs.writeFileSync(logPath, lines.join("\n") + "\n");

// the tail serve.py would keep when it rolls this log
const python = process.env.PYTHON || "python3";
const tail = execFileSync(python, ["-c", `
import json, sys
sys.path.insert(0, ".")
import serve
with open(sys.argv[1], encoding="utf-8") as f:
    lines = f.read().splitlines()
sys.stdout.write("\\n".join(serve.carry_forward(lines, int(sys.argv[2]))))
`, logPath, String(NOW)], { encoding: "utf8" }).split("\n").filter(Boolean);

assert.ok(tail.length > 0, "carry_forward returned nothing");
assert.ok(tail.length < lines.length, "carry_forward reclaimed nothing");

const shape = (villagers) => villagers.map((v) => ({
  id: v.id, state: v.state, lastTs: v.lastTs, name: v.name, char: v.char,
  accent: v.accent, project: v.project, doing: v.doing, lastLine: v.lastLine,
  events: v.events.map((e) => JSON.stringify(e)),
}));

const before = shape(reduce(lines, NOW, []));
const after = shape(reduce(tail, NOW, []));

assert.deepEqual(after, before);
// spread out of the vm realm so the array prototypes match
assert.deepEqual([...before.map((v) => v.id)].sort(),
                 ["live-1", "live-2", "live-3", "live-4"]);
fs.rmSync(dir, { recursive: true, force: true });
console.log(`ok — ${before.length} villagers identical across rotation ` +
            `(${lines.length} lines -> ${tail.length})`);
