"""Claude parent/child lifecycle callbacks remain separate v0 villagers."""

import importlib.util
import pathlib
import json
import os
import subprocess
import sys
import tempfile
import io
from unittest import mock
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("burrow_emit_claude_children", ROOT / "hooks" / "emit.py")
emit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(emit)
FIXTURE = ROOT / "tests" / "fixtures" / "claude-subagents.jsonl"


class ClaudeSubagentAdapterTest(unittest.TestCase):
    def deliver(self, hooks):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, BURROW_MIRROR="")
            env.pop("BURROW_URL", None)
            for hook in hooks:
                subprocess.run([sys.executable, str(ROOT / "hooks" / "emit.py")],
                               input=json.dumps(hook), text=True, check=True, env=env)
            log = pathlib.Path(home) / ".burrow" / "events.jsonl"
            return [json.loads(line) for line in log.read_text().splitlines()]

    def project(self, events):
        script = """
const { reduce } = require('./viewer/projection.js');
const events = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(reduce(events, Date.now(), [])));
"""
        result = subprocess.run(["node", "-e", script, json.dumps(events)], cwd=ROOT,
                                check=True, text=True, capture_output=True)
        return json.loads(result.stdout)

    def test_fixture_projects_parent_and_two_children_then_stops_only_one(self):
        hooks = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
        before = {v["id"]: v for v in self.project(self.deliver(hooks[:3]))}
        self.assertEqual(set(before), {
            "claude-code:parent-redacted", "claude-code:child-redacted-a",
            "claude-code:child-redacted-b",
        })
        self.assertEqual(before["claude-code:child-redacted-a"]["events"][0]["payload"], {
            "agent_type": "Explore", "parent_agent_id": "claude-code:parent-redacted",
        })
        after = {v["id"]: v for v in self.project(self.deliver(hooks))}
        self.assertNotIn("claude-code:child-redacted-a", after)
        self.assertIn("claude-code:child-redacted-b", after)
        self.assertIn("claude-code:parent-redacted", after)

    def test_parent_and_two_children_have_independent_identities_and_lineage(self):
        parent = {"hook_event_name": "UserPromptSubmit", "session_id": "parent", "prompt": "delegate"}
        first = {"hook_event_name": "SubagentStart", "session_id": "parent",
                 "agent_id": "child-a", "agent_type": "Explore"}
        second = {"hook_event_name": "SubagentStart", "session_id": "parent",
                  "agent_id": "child-b", "agent_type": "general-purpose"}

        self.assertEqual(emit.hook_agent_id("claude", parent), "claude-code:parent")
        self.assertEqual(emit.hook_agent_id("claude", first), "claude-code:child-a")
        self.assertEqual(emit.hook_agent_id("claude", second), "claude-code:child-b")
        self.assertEqual(emit.adapt_hook("claude", first), [("task_started", {
            "parent_agent_id": "claude-code:parent", "agent_type": "Explore",
        })])

    def test_subagent_stop_ends_only_the_matching_child(self):
        stopped = {"hook_event_name": "SubagentStop", "session_id": "parent",
                   "agent_id": "child-b", "agent_type": "general-purpose"}
        self.assertEqual(emit.hook_agent_id("claude", stopped), "claude-code:child-b")
        self.assertEqual(emit.adapt_hook("claude", stopped), [("session_ended", {
            "parent_agent_id": "claude-code:parent", "agent_type": "general-purpose",
        })])

    def test_resident_parent_does_not_collapse_or_keep_a_stopped_child(self):
        hook = {"hook_event_name": "SubagentStop", "session_id": "backing-session",
                "agent_id": "child", "agent_type": "Explore", "cwd": "/workspace/life"}
        delivered = []
        with mock.patch.dict(os.environ, {"BURROW_AGENT_ID": "life-agent"}), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(hook))), \
                mock.patch.object(emit, "deliver", side_effect=delivered.append):
            emit.main("claude")
        [event] = delivered
        self.assertEqual(event["agent_id"], "claude-code:child")
        self.assertEqual(event["type"], "session_ended")
        self.assertEqual(event["payload"]["parent_agent_id"], "claude-code:life-agent")


if __name__ == "__main__":
    unittest.main()
