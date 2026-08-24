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
from unittest import mock

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

    def test_notebook_path_and_failed_tools_are_truthful(self):
        self.assertEqual(emit.to_event({
            "hook_event_name": "PostToolUse", "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": "/w/analysis.ipynb"},
        }), ("artifact_produced", {"artifact": "/w/analysis.ipynb"}))
        self.assertEqual(emit.to_event({
            "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
            "error": "exit code 2",
        }), ("tool_failed", {"tool": "Bash", "error": "exit code 2"}))

    def test_only_real_approval_and_elicitation_notifications_knock(self):
        for notification_type in ("permission_prompt", "elicitation_dialog"):
            self.assertEqual(emit.to_event({
                "hook_event_name": "Notification", "notification_type": notification_type,
                "message": "Please decide",
            }), ("needs_human", {"message": "Please decide"}))
        for notification_type in ("idle_prompt", "auth_success", None):
            self.assertEqual(emit.to_event({
                "hook_event_name": "Notification", "notification_type": notification_type,
                "message": "informational",
            }), (None, None))


class DetailPolicyTest(unittest.TestCase):
    EVENT = {
        "v": 0, "ts": "2026-08-24T12:00:00.000Z", "source": "test",
        "agent_id": "test:one", "project": "burrow", "cwd": "/secret/project",
        "type": "artifact_produced", "payload": {"artifact": "/secret/project/notes.md"},
    }

    def delivered_payload(self, runner, hook, policy="safe"):
        event_type, payload = emit.adapt_hook(runner, hook)[0]
        event = dict(self.EVENT, type=event_type, payload=payload)
        posted = []
        with mock.patch.dict(os.environ, {
            "BURROW_DETAIL": policy, "BURROW_URL": "http://village",
            "BURROW_MIRROR": "",
        }), mock.patch.object(emit, "post_event",
                             side_effect=lambda url, outgoing, token:
                             posted.append(outgoing) or True):
            emit.deliver(event)
        self.assertEqual(len(posted), 1)
        return posted[0]["payload"]

    def test_safe_redacts_a_command_containing_a_url_and_query_token(self):
        payload = self.delivered_payload("codex", {
            "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {
                "command": "curl https://api.example.test/items?token=secret",
            },
        })
        self.assertEqual(payload["detail"], "[redacted]")

    def test_safe_redacts_url_query_description_and_approval_reason(self):
        cases = (
            ("WebFetch", {"url": "https://private.test/team/report"}, "detail"),
            ("WebSearch", {"query": "site:private.test/team report"}, "detail"),
            ("Bash", {"description": "Deploy team/private service"}, "detail"),
        )
        for tool, tool_input, field in cases:
            with self.subTest(tool_input=tool_input):
                payload = self.delivered_payload("codex", {
                    "hook_event_name": "PreToolUse", "tool_name": tool,
                    "tool_input": tool_input,
                })
                self.assertEqual(payload[field], "[redacted]")

        approval = self.delivered_payload("codex", {
            "hook_event_name": "PermissionRequest", "tool_name": "Bash",
            "tool_input": {"description": "Approve team/private deploy"},
        })
        self.assertEqual(approval["message"], "[redacted]")

    def test_safe_basenames_only_explicit_file_and_notebook_paths(self):
        for field, value, expected in (
            ("file_path", "/secret/project/report.txt", "report.txt"),
            ("notebook_path", "/secret/project/analysis.ipynb", "analysis.ipynb"),
        ):
            with self.subTest(field=field):
                hook = {
                    "hook_event_name": "PreToolUse", "tool_name": "Read",
                    "tool_input": {field: value},
                }
                original = json.loads(json.dumps(hook))
                payload = self.delivered_payload("codex", hook)
                self.assertEqual(payload["detail"], expected)
                self.assertEqual(hook, original)

    def test_full_keeps_and_off_redacts_tool_detail(self):
        hook = {
            "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": "curl https://private.test/team"},
        }
        original = json.loads(json.dumps(hook))
        full_detail = self.delivered_payload("codex", hook, "full")["detail"]
        self.assertEqual(full_detail, hook["tool_input"]["command"])
        self.assertIs(type(full_detail), str)
        self.assertEqual(self.delivered_payload("codex", hook, "off")["detail"],
                         "[redacted]")
        self.assertEqual(hook, original)

    def test_full_safe_and_off_are_applied_without_mutating_input(self):
        for policy, cwd, artifact in (
            ("full", "/secret/project", "/secret/project/notes.md"),
            ("safe", "", "notes.md"),
            ("off", "", "[redacted]"),
        ):
            with self.subTest(policy), mock.patch.dict(os.environ, {"BURROW_DETAIL": policy}):
                redacted = emit.redact_event(self.EVENT)
                self.assertEqual(redacted["cwd"], cwd)
                self.assertEqual(redacted["payload"]["artifact"], artifact)
        self.assertEqual(self.EVENT["cwd"], "/secret/project")
        self.assertEqual(self.EVENT["payload"]["artifact"], "/secret/project/notes.md")

    def test_unknown_policy_fails_private(self):
        with mock.patch.dict(os.environ, {"BURROW_DETAIL": "typo"}):
            self.assertEqual(emit.detail_policy(), "safe")

    def test_delivery_redacts_before_each_remote_transport(self):
        posted = []
        with mock.patch.dict(os.environ, {
            "BURROW_DETAIL": "safe", "BURROW_URL": "http://village",
            "BURROW_MIRROR": "http://mirror",
        }), mock.patch.object(emit, "post_event",
                             side_effect=lambda url, event, token: posted.append(event) or True):
            emit.deliver(self.EVENT)
        self.assertEqual(len(posted), 2)
        self.assertTrue(all(event["cwd"] == "" for event in posted))
        self.assertTrue(all(event["payload"]["artifact"] == "notes.md"
                            for event in posted))

    def test_local_fallback_is_also_redacted(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {
                    "BURROW_DETAIL": "off", "BURROW_URL": "", "BURROW_MIRROR": "",
                }), mock.patch.object(emit, "LOG_DIR", directory), \
                mock.patch.object(emit, "LOG", os.path.join(directory, "events.jsonl")):
            emit.deliver(self.EVENT)
            with open(emit.LOG, encoding="utf-8") as stream:
                written = json.load(stream)
        self.assertEqual(written["cwd"], "")
        self.assertEqual(written["payload"]["artifact"], "[redacted]")


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
