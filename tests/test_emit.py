"""Hook -> event mapping for the emitter (docs/protocol.md, issue #4).

    python3 -m unittest discover tests     (run from the repo root)
"""
import importlib.util
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
        env = dict(os.environ, HOME=home)
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


if __name__ == "__main__":
    unittest.main()
