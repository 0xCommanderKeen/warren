"""Rotation: the live log stays bounded, archives keep the history, and the
village the viewer reduces to is identical before and after the roll.

    python3 tests/test_rotation.py        (from the repo root)
"""
import datetime
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from tests.http_test_support import RunningServer


def ts(minutes_ago=0):
    now = datetime.datetime.now(datetime.timezone.utc)
    when = now - datetime.timedelta(minutes=minutes_ago)
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def event(agent, etype="tool_called", minutes_ago=0, **payload):
    return {"v": 0, "ts": ts(minutes_ago), "source": "test", "agent_id": agent,
            "project": "burrow", "cwd": "/tmp", "type": etype, "payload": payload}


def village(lines, now_ms=None):
    """The viewer's projection, boiled down to what each villager shows: state
    is decided by the latest event, and only live villagers are drawn."""
    now_ms = now_ms or int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    per_agent = {}
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not ev.get("agent_id") or ev.get("type") not in serve.EVENT_TYPES:
            continue
        kept = per_agent.setdefault(ev["agent_id"], [])
        kept.append(ev)
        if len(kept) > serve.KEEP_PER_AGENT:
            kept.pop(0)
    out = {}
    for agent, kept in per_agent.items():
        last = kept[-1]
        if last["type"] == "session_ended":
            continue
        if now_ms - serve.event_ms(last) > serve.DROP_MS:
            continue
        out[agent] = {"last": last, "history": [json.dumps(e, sort_keys=True) for e in kept]}
    return out


class CarryForwardTest(unittest.TestCase):
    def test_task_tie_projection_is_constant_space_under_ten_thousand_events(self):
        timestamp = "2026-08-25T10:01:00.000Z"
        post = {"v": 0, "ts": "2026-08-25T10:00:00.000Z", "source": "steward",
                "agent_id": "steward:api", "project": "life", "type": "task_posted",
                "payload": {"task_id": "stress", "title": "Stress",
                            "required_skills": ["research"], "posted_by": "api"}}
        transitions = []
        for index in range(10_000):
            claimant = "codex:holder-%05d" % index
            transitions.append({"v": 0, "ts": timestamp, "source": "steward",
                                "agent_id": claimant, "project": "life",
                                "type": "task_claimed", "payload": {
                                    "task_id": "stress", "title": "Stress",
                                    "claimant": claimant}})

        ordered = [post, *transitions, transitions[0], transitions[-1]]
        keep = serve._task_keep_indexes(list(enumerate(ordered)))
        retained = [ordered[index] for index in sorted(keep)]
        self.assertLessEqual(len(retained), 2,
                             "rotation retains only canonical post and transition")
        self.assertEqual(retained[-1]["payload"]["claimant"], "codex:holder-09999")

        reversed_input = [post, *reversed(transitions), transitions[-1], transitions[0]]
        reverse_keep = serve._task_keep_indexes(list(enumerate(reversed_input)))
        reverse_retained = [reversed_input[index] for index in sorted(reverse_keep)]
        self.assertEqual(
            [serve._task_event_identity(item) for item in retained],
            [serve._task_event_identity(item) for item in reverse_retained],
            "equal-time selection is independent of grouping, order, and exact replay")

    def test_matches_viewers_global_4000_line_window(self):
        sparse = json.dumps(event("sparse", "idle", 1))
        lines = [sparse] + [json.dumps(event("gone", "session_ended", 1, n=i))
                            for i in range(4000)]
        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertNotIn(sparse, tail)

    def test_latest_heartbeat_keeps_agent_live_without_consuming_history(self):
        actions = [json.dumps(event("a", "tool_called", 60 * 13,
                                    tool="Read", n=i))
                   for i in range(serve.KEEP_PER_AGENT + 10)]
        heartbeat = json.dumps(event("a", "heartbeat", 1, tool="Read"))
        tail = serve.carry_forward(actions + [heartbeat], int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(len(tail), serve.KEEP_PER_AGENT + 1)
        self.assertEqual(tail[-1], heartbeat)
        self.assertEqual(json.loads(tail[0])["payload"]["n"], 10)

    def test_heartbeat_after_session_end_revives_agent(self):
        ended = json.dumps(event("a", "session_ended", 2))
        heartbeat = json.dumps(event("a", "heartbeat", 1, tool="Read"))
        tail = serve.carry_forward([ended, heartbeat], int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(tail, [ended, heartbeat])

    def test_keeps_live_agents_and_drops_departed_ones(self):
        lines = [json.dumps(e) for e in [
            event("a", "task_started", 5, prompt="work"),
            event("gone", "task_started", 5, prompt="work"),
            event("stale", "idle", 60 * 20),          # 20 h ago: past the window
            event("gone", "session_ended", 4),
            event("a", "tool_called", 1, tool="Read"),
        ]]
        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        agents = {json.loads(line)["agent_id"] for line in tail}
        self.assertEqual(agents, {"a"})
        self.assertEqual(len(tail), 2)

    def test_preserves_original_order(self):
        lines = [json.dumps(event("a", "task_started", 3, prompt="one")),
                 json.dumps(event("b", "tool_called", 2, tool="Read")),
                 json.dumps(event("a", "idle", 1))]
        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(tail, lines)

    def test_caps_history_per_agent(self):
        lines = [json.dumps(event("a", "tool_called", 1, tool="Read", n=i))
                 for i in range(serve.KEEP_PER_AGENT + 40)]
        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(len(tail), serve.KEEP_PER_AGENT)
        self.assertEqual(tail, lines[-serve.KEEP_PER_AGENT:])

    def test_compaction_reload_keeps_child_lineage_outside_display_history(self):
        lineage = event("a-child", "task_started", 3,
                        prompt="delegated work", parent_agent_id="z-parent",
                        agent_type="reviewer")
        lines = [json.dumps(lineage), json.dumps(event("z-parent", "idle", 2))]
        lines.extend(json.dumps(event("a-child", "tool_called", 1,
                                             tool="Read", n=index))
                     for index in range(serve.KEEP_PER_AGENT + 1))

        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        script = """
const fs = require('node:fs');
const { reduce } = require('./viewer/projection.js');
const lines = JSON.parse(fs.readFileSync(0, 'utf8'));
const resident = {
  file: 'project.resident.json', valid: true, manifest_version: 1, home: 0,
  match: { project: 'burrow' },
  meta: { project: 'burrow', name: 'Maren', char: 'Monk', accent: '#a68a4f' },
  body: 'Resident', capabilities: {
    soul: {}, skills: [], memory: {}, routes: [], app_grants: []
  }
};
const village = reduce(lines, Date.now(), [resident]);
process.stdout.write(JSON.stringify(Object.fromEntries(
  village.map(v => [v.id, v.residency]))));
"""
        projected = subprocess.run(
            ["node", "-e", script], input=json.dumps(tail), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)

        self.assertIn(json.dumps(lineage), tail)
        self.assertEqual(json.loads(projected.stdout), {
            "a-child": "visitor", "z-parent": "resident",
        })

    def test_ignores_junk_lines(self):
        lines = ["not json", json.dumps({"type": "tool_called"}),
                 json.dumps(event("a", "idle", 1))]
        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(tail, [lines[-1]])


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = os.path.join(self.tmp.name, "data", "events.jsonl")
        os.makedirs(os.path.dirname(self.events))
        self.previous = (serve.EVENTS, serve.MAX_LOG_BYTES, serve.ARCHIVE_DIR)
        serve.EVENTS = self.events
        serve.MAX_LOG_BYTES = 4096
        serve.ARCHIVE_DIR = ""
        serve._rotate_floor = 0

    def tearDown(self):
        serve.EVENTS, serve.MAX_LOG_BYTES, serve.ARCHIVE_DIR = self.previous
        serve._rotate_floor = 0
        self.tmp.cleanup()

    def write(self, events):
        with open(self.events, "a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def live_lines(self):
        with open(self.events, encoding="utf-8") as f:
            return f.read().splitlines()

    def archives(self):
        into = serve.archive_dir()
        if not os.path.isdir(into):
            return []
        return sorted(os.path.join(into, f) for f in os.listdir(into))

    def noisy_history(self):
        """A log well past the threshold: piles of chatter from a villager that
        went home and one that fell out of the 12 h window, then two live ones."""
        old = []
        for i in range(30):
            old.append(event("departed", "tool_called", 300, tool="Read",
                             detail="x" * 150, n=i))
            old.append(event("ancient", "tool_called", 60 * 30, tool="Grep",
                             detail="y" * 150, n=i))
        return old + [
            event("departed", "session_ended", 200),
            event("ancient", "idle", 60 * 29),
            event("live-1", "task_started", 5, prompt="the current task"),
            event("live-1", "tool_called", 4, tool="Read", detail="README.md"),
            event("live-2", "needs_human", 2, message="a question"),
        ]

    def test_rotates_and_preserves_the_village(self):
        self.write(self.noisy_history())
        before = village(self.live_lines())
        size = os.path.getsize(self.events)
        self.assertGreater(size, serve.MAX_LOG_BYTES)

        with serve.LOG_LOCK:
            serve.maybe_rotate()

        self.assertLess(os.path.getsize(self.events), size)
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(village(self.live_lines()), before)
        self.assertEqual(set(before), {"live-1", "live-2"})

    def test_archive_holds_everything_that_was_live(self):
        original = self.noisy_history()
        self.write(original)
        with serve.LOG_LOCK:
            serve.maybe_rotate()
        with open(self.archives()[0], encoding="utf-8") as f:
            archived = f.read().splitlines()
        self.assertEqual(archived,
                         [json.dumps(e, ensure_ascii=False) for e in original])

    def test_archive_directory_is_durable_before_live_log_is_modified(self):
        original = self.noisy_history()
        self.write(original)
        checkpoints = []

        def inspect_live(path):
            with open(self.events, "rb") as stream:
                checkpoints.append((path, stream.read()))

        with mock.patch.object(serve, "_fsync_parent", side_effect=inspect_live):
            with serve.LOG_LOCK:
                serve.maybe_rotate()

        self.assertEqual(len(checkpoints), 1)
        self.assertTrue(checkpoints[0][0].startswith(serve.archive_dir()))
        self.assertTrue(os.path.exists(checkpoints[0][0]))
        self.assertEqual(checkpoints[0][1],
                         "".join(json.dumps(item, ensure_ascii=False) + "\n"
                                 for item in original).encode())

    def test_already_open_append_descriptor_stays_on_live_log(self):
        self.write(self.noisy_history())
        fd = open(self.events, "a", encoding="utf-8")
        try:
            with serve.LOG_LOCK:
                serve.maybe_rotate()
            late = event("late", "task_started", 0, prompt="after rotation")
            fd.write(json.dumps(late) + "\n")
            fd.flush()
        finally:
            fd.close()
        self.assertIn("late", {json.loads(line)["agent_id"]
                               for line in self.live_lines()})

    def test_archive_names_carry_a_timestamp(self):
        self.write(self.noisy_history())
        with serve.LOG_LOCK:
            serve.maybe_rotate()
        name = os.path.basename(self.archives()[0])
        stamp = name[len("events-"):-len(".jsonl")]
        self.assertTrue(name.startswith("events-") and name.endswith(".jsonl"), name)
        datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")

    def test_repeated_rotations_keep_every_segment(self):
        for _ in range(3):
            self.write(self.noisy_history())
            with serve.LOG_LOCK:
                serve.maybe_rotate()
        self.assertEqual(len(self.archives()), 3)

    def test_honours_a_separate_archive_directory(self):
        serve.ARCHIVE_DIR = os.path.join(self.tmp.name, "elsewhere")
        self.write(self.noisy_history())
        with serve.LOG_LOCK:
            serve.maybe_rotate()
        self.assertEqual(len(self.archives()), 1)
        self.assertTrue(self.archives()[0].startswith(serve.ARCHIVE_DIR))

    def test_disabled_by_zero_threshold(self):
        serve.MAX_LOG_BYTES = 0
        self.write(self.noisy_history())
        size = os.path.getsize(self.events)
        with serve.LOG_LOCK:
            serve.maybe_rotate()
        self.assertEqual(os.path.getsize(self.events), size)
        self.assertEqual(self.archives(), [])

    def test_no_thrash_when_every_event_is_still_live(self):
        """One busy agent whose whole (capped) history is current: there is
        nothing to reclaim, so we must not archive a copy on every append."""
        serve.MAX_LOG_BYTES = 512
        self.write([event("busy", "tool_called", 1, tool="Read", detail="z" * 100)
                    for _ in range(serve.KEEP_PER_AGENT)])
        for _ in range(5):
            with serve.LOG_LOCK:
                serve.maybe_rotate()
        self.assertLessEqual(len(self.archives()), 1)


class ServerRotationTest(unittest.TestCase):
    """The same thing through the HTTP surface the fleet actually uses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = os.path.join(self.tmp.name, "data", "events.jsonl")
        os.makedirs(os.path.dirname(self.events))
        self.previous = (serve.EVENTS, serve.MAX_LOG_BYTES, serve.ARCHIVE_DIR)
        serve.EVENTS = self.events
        serve.MAX_LOG_BYTES = 8192
        serve.ARCHIVE_DIR = ""
        serve._rotate_floor = 0
        self.running_server = RunningServer(serve)
        self.server = self.running_server.server

    def tearDown(self):
        self.running_server.stop()
        serve.EVENTS, serve.MAX_LOG_BYTES, serve.ARCHIVE_DIR = self.previous
        serve._rotate_floor = 0
        self.tmp.cleanup()

    def write(self, events):
        with open(self.events, "a", encoding="utf-8") as stream:
            for item in events:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")

    def post(self, ev):
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.request("POST", "/events", json.dumps(ev),
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status

    def get_events(self):
        _, _, body = self.get_events_response()
        return [line for line in body.splitlines() if line]

    def get_events_response(self, since=None):
        conn = http.client.HTTPConnection(*self.server.server_address)
        path = "/events" + ("?since=" + since if since else "")
        conn.request("GET", path)
        response = conn.getresponse()
        status = response.status
        headers = dict(response.getheaders())
        body = response.read().decode("utf-8")
        conn.close()
        return status, headers, body

    def project_jobs(self, lines):
        script = """
const fs = require('node:fs');
const jobs = require('./viewer/job-board.js');
const { validateEvent } = require('./viewer/projection.js');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const state = jobs.createState();
jobs.fold(state, input, { validateEvent });
process.stdout.write(JSON.stringify(jobs.rows(state, Date.now())));
"""
        projected = subprocess.run(
            ["node", "-e", script], input=json.dumps(lines), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        return json.loads(projected.stdout)

    def task_event(self, task_id, etype, minutes_ago, claimant="codex:worker",
                   reason="session_failed"):
        payload = {"task_id": task_id, "title": "Task " + task_id}
        agent_id = "steward:api"
        if etype == "task_posted":
            payload.update(required_skills=["research"], posted_by="api")
        else:
            agent_id = claimant
            payload["claimant"] = claimant
            if etype == "task_done":
                payload["artifacts"] = ["notes/" + task_id + ".md"]
            if etype == "task_failed":
                payload["reason"] = reason
        return {"v": 0, "ts": ts(minutes_ago), "source": "steward",
                "agent_id": agent_id, "project": "life", "type": etype,
                "payload": payload}

    def seed_departed_chatter(self, n=60):
        """History that rotation can actually reclaim: an agent that went home."""
        with open(self.events, "a", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps(event("departed", "tool_called", 300,
                                         tool="Read", detail="o" * 200, n=i)) + "\n")
            f.write(json.dumps(event("departed", "session_ended", 299)) + "\n")

    def test_rotation_reset_preserves_cross_agent_task_lifecycles(self):
        """The real reset response must reconstruct every Steward task state.

        Posts belong to steward:api while claims and terminal evidence belong
        to claimants whose sessions may then end. Orphans remain explicit
        degraded evidence rather than acquiring invented post metadata.
        """
        serve.MAX_LOG_BYTES = 0
        lifecycle = [
            self.task_event("done", "task_posted", 9),
            self.task_event("done", "task_claimed", 8, "codex:done"),
            self.task_event("done", "task_done", 7, "codex:done"),
            event("codex:done", "session_ended", 6),
            self.task_event("failed", "task_posted", 9),
            self.task_event("failed", "task_claimed", 8, "codex:failed"),
            self.task_event("failed", "task_failed", 7, "codex:failed"),
            event("codex:failed", "session_ended", 6),
            self.task_event("reopened", "task_posted", 9),
            self.task_event("reopened", "task_claimed", 8, "codex:reopened"),
            self.task_event("reopened", "task_failed", 7, "codex:reopened",
                            "lease_expired"),
            event("codex:reopened", "session_ended", 6),
            self.task_event("orphan-claim", "task_claimed", 5, "codex:orphan"),
            self.task_event("orphan-done", "task_done", 4, "codex:orphan"),
            event("codex:orphan", "session_ended", 3),
            # Later facts arrive first; duplicates and older replay follow.
            self.task_event("unordered", "task_posted", 9),
            self.task_event("unordered", "task_done", 2, "codex:unordered"),
            self.task_event("unordered", "task_claimed", 8, "codex:unordered"),
            self.task_event("unordered", "task_posted", 10),
            self.task_event("unordered", "task_done", 2, "codex:unordered"),
            event("codex:unordered", "session_ended", 1),
        ]
        same_ts = ts(8)
        same_post = self.task_event("same-ms-reclaim", "task_posted", 9)
        same_expiry = self.task_event("same-ms-reclaim", "task_failed", 8,
                                      "codex:old-holder", "lease_expired")
        same_claim = self.task_event("same-ms-reclaim", "task_claimed", 8,
                                     "codex:new-holder")
        same_expiry["ts"] = same_ts
        same_claim["ts"] = same_ts
        lifecycle.extend([same_post, same_expiry, same_claim, same_expiry.copy()])
        # More terminal identities than the board retains prove that central
        # posts cannot leak back through per-agent history and resurrect work
        # deliberately omitted by task-ID capacity.
        for index in range(26):
            task_id = "capacity-%02d" % index
            claimant = "codex:capacity-%02d" % index
            lifecycle.extend([
                self.task_event(task_id, "task_posted", 14),
                self.task_event(task_id, "task_done", 13, claimant),
                event(claimant, "session_ended", 12),
            ])
        self.write(lifecycle)
        status, headers, initial_body = self.get_events_response()
        self.assertEqual(status, 200)
        cursor = headers["X-Burrow-Cursor"]
        before = self.project_jobs(initial_body.splitlines())

        self.seed_departed_chatter(80)
        serve.MAX_LOG_BYTES = 512
        status, headers, reset_body = self.get_events_response(cursor)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Burrow-Reset"), "1")
        after = self.project_jobs(reset_body.splitlines())
        self.assertEqual(after, before)

        rows = {row["id"]: row for row in after}
        self.assertEqual(rows["done"]["state"], "done")
        self.assertEqual(rows["failed"]["state"], "failed")
        self.assertEqual(rows["reopened"]["state"], "open")
        self.assertEqual(rows["unordered"]["state"], "done")
        self.assertEqual(rows["same-ms-reclaim"]["state"], "claimed")
        self.assertEqual(rows["same-ms-reclaim"]["claimant"], "codex:new-holder")
        self.assertIsNone(rows["orphan-claim"]["required_skills"])
        self.assertIsNone(rows["orphan-done"]["required_skills"])

    def test_rotation_preserves_event_granular_capacity_without_restoring_evicted_post(self):
        """A claim may reintroduce an evicted ID, but not its missing post fields."""
        serve.MAX_LOG_BYTES = 0
        start = datetime.datetime(2026, 8, 25, 10, 0, tzinfo=datetime.timezone.utc)
        posts = []
        for index in range(serve.KEEP_TASKS + 1):
            item = self.task_event("task-%02d" % index, "task_posted", 1)
            item["ts"] = (start + datetime.timedelta(milliseconds=index)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
            item["payload"]["required_skills"] = ["skill-%02d" % index]
            posts.append(item)
        claim = self.task_event("task-00", "task_claimed", 0, "codex:reintroduced")
        claim["ts"] = (start + datetime.timedelta(seconds=1)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        claim["payload"]["title"] = "Claimed after eviction"
        self.write([*posts, claim])

        status, headers, initial_body = self.get_events_response()
        self.assertEqual(status, 200)
        before = self.project_jobs(initial_body.splitlines())
        before_rows = {row["id"]: row for row in before}
        self.assertIsNone(before_rows["task-00"]["required_skills"])
        self.assertEqual(before_rows["task-00"]["title"], "Claimed after eviction")
        self.assertNotIn("task-01", before_rows)

        self.seed_departed_chatter(80)
        serve.MAX_LOG_BYTES = 512
        status, reset_headers, reset_body = self.get_events_response(
            headers["X-Burrow-Cursor"])
        self.assertEqual(status, 200)
        self.assertEqual(reset_headers.get("X-Burrow-Reset"), "1")
        after = self.project_jobs(reset_body.splitlines())
        self.assertEqual(after, before,
                         "rotation/reset preserves canonical rows and missing-post truth")

    def test_concurrent_posts_survive_rotation(self):
        self.seed_departed_chatter()
        agents = ["claude-code:%d" % i for i in range(4)]
        sent = []
        lock = threading.Lock()

        def hammer(agent):
            for i in range(60):
                ev = event(agent, "tool_called", 0, tool="Read", detail="q" * 200, n=i)
                self.assertEqual(self.post(ev), 204)
                with lock:
                    sent.append(ev)

        threads = [threading.Thread(target=hammer, args=(a,)) for a in agents]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        archives = sorted(os.listdir(serve.archive_dir())) if os.path.isdir(serve.archive_dir()) else []
        self.assertTrue(archives, "expected at least one rotation under load")

        # every accepted POST is somewhere on disk: live tail or an archive
        seen = set(self.get_events())
        for name in archives:
            with open(os.path.join(serve.archive_dir(), name), encoding="utf-8") as f:
                seen.update(f.read().splitlines())
        for ev in sent:
            self.assertIn(json.dumps(ev, ensure_ascii=False), seen)

        # and the live log alone still shows all four villagers, working
        drawn = village(self.get_events())
        self.assertEqual(set(drawn), set(agents))

    def test_local_mode_rotation_happens_on_read(self):
        """Nothing POSTs in local mode — emitters append to the file directly —
        so the read path has to keep it bounded too."""
        self.seed_departed_chatter()
        with open(self.events, "a", encoding="utf-8") as f:
            f.write(json.dumps(event("live", "task_started", 4, prompt="now")) + "\n")
            f.write(json.dumps(event("live", "idle", 1)) + "\n")
        size = os.path.getsize(self.events)
        self.assertGreater(size, serve.MAX_LOG_BYTES)
        lines = self.get_events()
        self.assertLess(os.path.getsize(self.events), serve.MAX_LOG_BYTES)
        self.assertTrue(os.listdir(serve.archive_dir()))
        self.assertEqual(set(village(lines)), {"live"})


if __name__ == "__main__":
    unittest.main()
