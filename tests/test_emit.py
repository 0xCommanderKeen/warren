"""Hook -> event mapping for the emitter (docs/protocol.md, issue #4).

    python3 -m unittest discover tests     (run from the repo root)
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMIT = os.path.join(ROOT, "hooks", "emit.py")

_spec = importlib.util.spec_from_file_location("burrow_emit", EMIT)
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)


class ToEventTest(unittest.TestCase):
    def test_pre_tool_use_still_starts_a_tool(self):
        etype, payload = emit.to_event({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "make build"},
        })
        self.assertEqual(etype, "tool_called")
        self.assertEqual(payload["tool"], "Bash")

    def test_write_still_produces_an_artifact(self):
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            etype, payload = emit.to_event({
                "hook_event_name": "PostToolUse",
                "tool_name": tool,
                "tool_input": {"file_path": "/w/burrow/serve.py"},
            })
            self.assertEqual(etype, "artifact_produced", tool)
            self.assertEqual(payload["artifact"], "/w/burrow/serve.py")

    def test_every_other_tool_finishing_is_a_heartbeat(self):
        for hook in ({"tool_name": "Bash", "tool_input": {"command": "make build"}},
                     {"tool_name": "Grep", "tool_input": {"pattern": "stale"}},
                     {"tool_name": "Read", "tool_input": {"file_path": "/w/README.md"}},
                     {"tool_name": "WebSearch", "tool_input": {}}):
            hook["hook_event_name"] = "PostToolUse"
            etype, payload = emit.to_event(hook)
            self.assertEqual(etype, "heartbeat", hook["tool_name"])
            self.assertEqual(payload, {"tool": hook["tool_name"]})

    def test_no_post_tool_use_is_dropped(self):
        """Every completion is a signal; dropping one is what made runs look stale."""
        etype, _ = emit.to_event({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                                  "tool_input": {}})
        self.assertIsNotNone(etype)


class EndToEndTest(unittest.TestCase):
    """Run the real hook script and read back the event it appended."""

    def run_hook(self, home, hook):
        env = dict(os.environ, HOME=home, BURROW_MIRROR="")
        env.pop("BURROW_URL", None)
        env.pop("BURROW_AGENT_ID", None)
        env.pop("BURROW_PROJECT", None)
        proc = subprocess.run([sys.executable, EMIT], input=json.dumps(hook),
                              text=True, capture_output=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_long_bash_run_writes_a_heartbeat(self):
        with tempfile.TemporaryDirectory() as home:
            self.run_hook(home, {
                "hook_event_name": "PostToolUse", "session_id": "s1",
                "cwd": "/w/burrow", "tool_name": "Bash",
                "tool_input": {"command": "make build"},
            })
            with open(os.path.join(home, ".burrow", "events.jsonl")) as f:
                events = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["type"], "heartbeat")
        self.assertEqual(event["payload"], {"tool": "Bash"})
        self.assertEqual(event["agent_id"], "claude-code:s1")
        self.assertEqual(event["project"], "burrow")
        self.assertTrue(event["ts"].endswith("Z"))


class TargetsTest(unittest.TestCase):
    """Delivery targets: the village plus any mirror (a local dev server)."""

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in
                      ("BURROW_URL", "BURROW_TOKEN", "BURROW_MIRROR", "BURROW_MIRROR_TOKEN")}
        for k in self.saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_local_dev_server_is_mirrored_by_default(self):
        """The whole point: run serve.py locally and see the live fleet without
        touching settings or deploying to the village."""
        os.environ["BURROW_URL"] = "http://village:8737"
        os.environ["BURROW_TOKEN"] = "s3cret"
        self.assertEqual(emit.targets(),
                         [("http://village:8737", "s3cret"),
                          (emit.DEFAULT_MIRROR, "")])

    def test_mirror_never_gets_the_village_secret(self):
        os.environ["BURROW_URL"] = "http://village:8737"
        os.environ["BURROW_TOKEN"] = "s3cret"
        os.environ["BURROW_MIRROR"] = "http://127.0.0.1:9000"
        self.assertEqual(dict(emit.targets())["http://127.0.0.1:9000"], "")
        os.environ["BURROW_MIRROR_TOKEN"] = "dev"
        self.assertEqual(dict(emit.targets())["http://127.0.0.1:9000"], "dev")

    def test_empty_mirror_turns_it_off(self):
        os.environ["BURROW_URL"] = "http://village:8737"
        os.environ["BURROW_MIRROR"] = ""
        self.assertEqual(emit.targets(), [("http://village:8737", "")])

    def test_a_url_named_twice_does_not_double_the_event(self):
        os.environ["BURROW_URL"] = "http://127.0.0.1:8737/"
        self.assertEqual(emit.targets(), [("http://127.0.0.1:8737", "")])

    def test_one_breaker_per_target(self):
        """A village that is down must not silence the dev server beside it."""
        self.assertNotEqual(emit.breaker_path("http://village:8737"),
                            emit.breaker_path(emit.DEFAULT_MIRROR))
        self.assertTrue(emit.is_loopback(emit.DEFAULT_MIRROR))
        self.assertFalse(emit.is_loopback("http://village:8737"))


class MirrorDeliveryTest(unittest.TestCase):
    """main() with a live village and a live mirror: both see the event, and
    nothing is written locally. With both down, the local log still catches it."""

    def emit_one(self, home, urls_up):
        posted = []

        class Ctx:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            if req.full_url not in urls_up:
                raise OSError("refused")
            posted.append(req.full_url)
            return Ctx()

        env = {"BURROW_URL": "http://village:8737", "BURROW_TOKEN": "s3cret"}
        saved_env = {k: os.environ.get(k) for k in
                     list(env) + ["BURROW_MIRROR", "BURROW_AGENT_ID", "BURROW_PROJECT"]}
        saved = (emit.LOG_DIR, emit.LOG, emit.BREAKER, emit.urllib.request.urlopen, sys.stdin)
        os.environ.pop("BURROW_MIRROR", None)
        os.environ.pop("BURROW_AGENT_ID", None)
        os.environ.pop("BURROW_PROJECT", None)
        os.environ.update(env)
        emit.LOG_DIR = home
        emit.LOG = os.path.join(home, "events.jsonl")
        emit.BREAKER = os.path.join(home, ".post-failed")
        emit.urllib.request.urlopen = fake_urlopen
        sys.stdin = io.StringIO(json.dumps(
            {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/w/burrow"}))
        try:
            emit.main()
        finally:
            (emit.LOG_DIR, emit.LOG, emit.BREAKER,
             emit.urllib.request.urlopen, sys.stdin) = saved
            for k, v in saved_env.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        local = os.path.join(home, "events.jsonl")
        lines = 0
        if os.path.exists(local):
            with open(local) as f:
                lines = sum(1 for line in f if line.strip())
        return posted, lines

    def test_village_and_mirror_both_receive_it(self):
        with tempfile.TemporaryDirectory() as home:
            posted, lines = self.emit_one(
                home, {"http://village:8737/events", emit.DEFAULT_MIRROR + "/events"})
        self.assertEqual(len(posted), 2, posted)
        self.assertEqual(lines, 0, "delivered remotely, so nothing to log locally")

    def test_mirror_alone_is_enough_to_skip_the_local_log(self):
        """Off the tailnet with a dev server up: the event is not lost, and it is
        not written twice (the dev server appends it to its own log)."""
        with tempfile.TemporaryDirectory() as home:
            posted, lines = self.emit_one(home, {emit.DEFAULT_MIRROR + "/events"})
        self.assertEqual(posted, [emit.DEFAULT_MIRROR + "/events"])
        self.assertEqual(lines, 0)

    def test_nothing_up_still_falls_back_to_the_local_log(self):
        with tempfile.TemporaryDirectory() as home:
            posted, lines = self.emit_one(home, set())
        self.assertEqual(posted, [])
        self.assertEqual(lines, 1)


if __name__ == "__main__":
    unittest.main()
