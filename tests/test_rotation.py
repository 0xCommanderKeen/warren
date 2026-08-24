"""Rotation: the live log stays bounded, archives keep the history, and the
village the viewer reduces to is identical before and after the roll.

    python3 tests/test_rotation.py        (from the repo root)
"""
import datetime
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve


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
    def test_keeps_live_agents_and_drops_departed_ones(self):
        lines = [json.dumps(e) for e in [
            event("a", "task_started", 5, prompt="work"),
            event("gone", "task_started", 5),
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
        self.server = serve.http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        serve.EVENTS, serve.MAX_LOG_BYTES, serve.ARCHIVE_DIR = self.previous
        serve._rotate_floor = 0
        self.tmp.cleanup()

    def post(self, ev):
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.request("POST", "/events", json.dumps(ev),
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status

    def get_events(self):
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.request("GET", "/events")
        body = conn.getresponse().read().decode("utf-8")
        conn.close()
        return [line for line in body.splitlines() if line]

    def seed_departed_chatter(self, n=60):
        """History that rotation can actually reclaim: an agent that went home."""
        with open(self.events, "a", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps(event("departed", "tool_called", 300,
                                         tool="Read", detail="o" * 200, n=i)) + "\n")
            f.write(json.dumps(event("departed", "session_ended", 299)) + "\n")

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
