"""Redacted captured-shape Codex hooks -> v0 delivery -> projection (issue #34).

The fixture is a fictionalized reconstruction of captured callbacks, not a raw
transcript: identifiers, paths, prompts, and responses are safe test values while
the field shape follows the official Codex hooks documentation.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import importlib.util


ROOT = pathlib.Path(__file__).resolve().parents[1]
EMIT = ROOT / "hooks" / "emit.py"
FIXTURE = ROOT / "tests" / "fixtures" / "codex-hooks.jsonl"

_spec = importlib.util.spec_from_file_location("burrow_emit_codex", EMIT)
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)


class CodexEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.hooks = [json.loads(line) for line in FIXTURE.read_text().splitlines()]

    def deliver(self, hooks):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, BURROW_MIRROR="")
            env.pop("BURROW_URL", None)
            for hook in hooks:
                proc = subprocess.run(
                    [sys.executable, str(EMIT), "--runner", "codex"],
                    input=json.dumps(hook), text=True, capture_output=True, env=env,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), "{}")
            path = pathlib.Path(home) / ".burrow" / "events.jsonl"
            return [json.loads(line) for line in path.read_text().splitlines()]

    def test_redacted_fixture_matches_the_documented_hook_shape(self):
        common = {"session_id", "transcript_path", "cwd",
                  "hook_event_name", "model"}
        permission_events = {
            "UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUse",
            "SubagentStart", "SubagentStop", "Stop",
        }
        turn_events = permission_events
        for hook in self.hooks:
            self.assertTrue(common <= hook.keys(), hook["hook_event_name"])
            self.assertTrue(hook["transcript_path"] is None
                            or isinstance(hook["transcript_path"], str))
            if hook["hook_event_name"] in permission_events:
                self.assertIn("permission_mode", hook)
            if hook["hook_event_name"] in turn_events:
                self.assertIn("turn_id", hook)

        by_name = {hook["hook_event_name"]: hook for hook in self.hooks}
        self.assertEqual(by_name["SessionEnd"]["reason"], "other")
        self.assertTrue({"agent_id", "agent_type"}
                        <= by_name["SubagentStart"].keys())
        self.assertTrue({"agent_id", "agent_type", "agent_transcript_path",
                         "stop_hook_active", "last_assistant_message"}
                        <= by_name["SubagentStop"].keys())

    def project(self, events):
        script = """
const { reduce } = require('./viewer/projection.js');
const events = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(reduce(events, Date.now(), [])));
"""
        result = subprocess.run(
            ["node", "-e", script, json.dumps(events)], cwd=ROOT,
            check=True, text=True, capture_output=True,
        )
        return json.loads(result.stdout)

    def test_captured_turn_reaches_the_existing_projection(self):
        events = self.deliver(self.hooks[:-1])
        root = [event for event in events
                if event["agent_id"] == "codex:thr_redacted_root"]
        child = [event for event in events
                 if event["agent_id"] == "codex:agent_redacted_review"]

        self.assertEqual({event["source"] for event in events}, {"codex"})
        self.assertEqual(
            [event["type"] for event in root],
            ["task_started", "tool_called", "heartbeat", "heartbeat",
             "tool_called", "artifact_produced", "artifact_produced",
             "heartbeat", "heartbeat"],
        )
        self.assertEqual(root[2]["payload"], {
            "phase": "approval_requested",
            "tool": "Bash",
            "detail": "Run the repository test suite outside the sandbox",
        })
        self.assertEqual([event["payload"]["artifact"] for event in root
                          if event["type"] == "artifact_produced"],
                         ["tests/new.py", "hooks/emit.py"])
        self.assertNotIn("hooks/old_emit.py", {
            event.get("payload", {}).get("artifact") for event in root
        })
        self.assertEqual(root[7]["payload"], {"tool": "apply_patch"})
        self.assertEqual([event["type"] for event in child],
                         ["task_started", "heartbeat"])
        self.assertEqual(child[0]["payload"]["parent_agent_id"],
                         "codex:thr_redacted_root")
        self.assertEqual(child[0]["payload"]["turn_id"], "turn_redacted")
        self.assertEqual(child[0]["payload"]["agent_type"], "reviewer")

        village = {villager["id"]: villager for villager in self.project(events)}
        self.assertEqual(village["codex:thr_redacted_root"]["state"], "working")
        self.assertEqual(village["codex:thr_redacted_root"]["project"], "burrow")
        self.assertEqual(village["codex:agent_redacted_review"]["state"], "working")

    def test_session_end_removes_only_the_root_codex_session(self):
        village = {villager["id"]: villager
                   for villager in self.project(self.deliver(self.hooks))}
        self.assertNotIn("codex:thr_redacted_root", village)
        self.assertIn("codex:agent_redacted_review", village)

    def test_long_root_identity_is_preserved_exactly_in_child_lineage(self):
        session_id = "root-" + "r" * 200
        events = self.deliver([
            {
                "session_id": session_id,
                "cwd": "/workspace/burrow",
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Delegate work",
            },
            {
                "session_id": session_id,
                "cwd": "/workspace/burrow",
                "hook_event_name": "SubagentStart",
                "agent_id": "child",
            },
        ])

        root = next(event for event in events
                    if event["agent_id"] == "codex:" + session_id)
        child = next(event for event in events
                     if event["agent_id"] == "codex:child")
        self.assertEqual(child["payload"]["parent_agent_id"], root["agent_id"])


class RunnerSelectionEndToEndTest(unittest.TestCase):
    def run_hook(self, args):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, BURROW_MIRROR="")
            env.pop("BURROW_URL", None)
            proc = subprocess.run(
                [sys.executable, str(EMIT), *args],
                input=json.dumps({
                    "session_id": "redacted-session",
                    "cwd": "/workspace/burrow",
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Do work",
                }),
                text=True, capture_output=True, env=env,
            )
            log = pathlib.Path(home) / ".burrow" / "events.jsonl"
            return proc, log.exists()

    def test_malformed_runner_options_emit_nothing(self):
        for args in (
            ["--runner", "unknown"],
            ["--runner"],
            ["--runner=codex"],
            ["--runner", "codex", "--runner", "codex"],
            ["--runner", "codex", "--runner", "claude"],
            ["unexpected"],
            ["--runner", "codex", "unexpected"],
            ["--unknown"],
        ):
            proc, log_exists = self.run_hook(args)
            self.assertEqual(proc.returncode, 0, (args, proc.stderr))
            self.assertEqual(proc.stdout, "", args)
            self.assertFalse(log_exists, args)


class DefensiveCodexInputTest(unittest.TestCase):
    def test_tool_names_are_bounded_in_pre_and_post_tool_events(self):
        long_tool = "mcp__server__" + "x" * 200
        bounded = "mcp__server__" + "x" * 107

        self.assertEqual(emit.codex_events({
            "hook_event_name": "PreToolUse", "tool_name": long_tool,
        }), [("tool_called", {"tool": bounded})])
        self.assertEqual(emit.codex_events({
            "hook_event_name": "PostToolUse", "tool_name": long_tool,
        }), [("heartbeat", {"tool": bounded})])
        self.assertEqual(emit.codex_events({
            "hook_event_name": "PostToolUse", "tool_name": "apply_patch" + "x" * 200,
        }), [("heartbeat", {"tool": "apply_patch" + "x" * 109})])

    def test_stop_is_a_bounded_lifecycle_heartbeat(self):
        self.assertEqual(emit.codex_events({
            "hook_event_name": "Stop",
            "turn_id": "turn-" + "z" * 200,
            "stop_hook_active": True,
            "last_assistant_message": "must not be copied",
        }), [("heartbeat", {
            "phase": "stop",
            "turn_id": "turn-" + "z" * 115,
            "stop_hook_active": True,
        })])

    def test_subagent_stop_is_a_bounded_lineage_heartbeat(self):
        self.assertEqual(emit.codex_events({
            "hook_event_name": "SubagentStop",
            "session_id": "root-" + "r" * 200,
            "agent_id": "child",
            "agent_type": "reviewer-" + "a" * 200,
            "turn_id": "turn",
            "stop_hook_active": False,
        }), [("heartbeat", {
            "phase": "subagent_stop",
            "turn_id": "turn",
            "agent_type": "reviewer-" + "a" * 111,
            "parent_agent_id": "codex:root-" + "r" * 200,
            "stop_hook_active": False,
        })])

    def test_permission_request_is_bounded_working_context_not_human_blockage(self):
        event = emit.codex_events({
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash" + "x" * 200,
            "tool_input": {"description": "approval reason " + "y" * 200},
        })
        self.assertEqual(event[0][0], "heartbeat")
        self.assertEqual(event[0][1]["phase"], "approval_requested")
        self.assertEqual(event[0][1]["tool"], "Bash" + "x" * 116)
        self.assertEqual(event[0][1]["detail"], "approval reason " + "y" * 104)

    def test_successful_apply_patch_reports_every_applied_path(self):
        self.assertEqual(emit.codex_events({
            "hook_event_name": "PostToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": (
                "*** Begin Patch\n"
                "*** Update File: hooks/emit.py\n"
                "*** Add File: tests/new.py\n"
                "*** End Patch"
            )},
            "tool_response": "Done!",
        }), [
            ("artifact_produced", {"artifact": "hooks/emit.py"}),
            ("artifact_produced", {"artifact": "tests/new.py"}),
        ])

    def test_successful_apply_patch_reports_resulting_paths_only(self):
        self.assertEqual(emit.codex_events({
            "hook_event_name": "PostToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": (
                "*** Begin Patch\n"
                "*** Add File: added.py\n"
                "+new\n"
                "*** Update File: source.py\n"
                "*** Move to: moved.py\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** Delete File: deleted.py\n"
                "*** End Patch"
            )},
            "tool_response": "Done!",
        }), [
            ("artifact_produced", {"artifact": "added.py"}),
            ("artifact_produced", {"artifact": "moved.py"}),
        ])

    def test_successful_deletion_only_patch_is_a_heartbeat(self):
        self.assertEqual(emit.codex_events({
            "hook_event_name": "PostToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": (
                "*** Begin Patch\n"
                "*** Delete File: obsolete.py\n"
                "*** End Patch"
            )},
            "tool_response": "Done!",
        }), [("heartbeat", {"tool": "apply_patch"})])

    def test_failed_apply_patch_reports_only_working(self):
        self.assertEqual(emit.codex_events({
            "hook_event_name": "PostToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": (
                "*** Begin Patch\n"
                "*** Update File: hooks/emit.py\n"
                "*** End Patch"
            )},
            "tool_response": "Failed to find expected lines",
        }), [("heartbeat", {"tool": "apply_patch"})])

    def test_partially_failed_apply_patch_claims_no_paths(self):
        self.assertEqual(emit.codex_events({
            "hook_event_name": "PostToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": (
                "*** Begin Patch\n"
                "*** Update File: hooks/emit.py\n"
                "*** Update File: tests/new.py\n"
                "*** End Patch"
            )},
            "tool_response": "Done!\nFailed to update tests/new.py",
        }), [("heartbeat", {"tool": "apply_patch"})])

    def test_ambiguous_apply_patch_response_claims_no_paths(self):
        for response in (None, {}, "Patch command completed", {"output": "Done!"},
                         {"output": "Done!", "error": "write failed"}):
            hook = {
                "hook_event_name": "PostToolUse",
                "tool_name": "apply_patch",
                "tool_input": {"command": (
                    "*** Begin Patch\n"
                    "*** Update File: hooks/emit.py\n"
                    "*** End Patch"
                )},
            }
            if response is not None:
                hook["tool_response"] = response
            self.assertEqual(emit.codex_events(hook), [
                ("heartbeat", {"tool": "apply_patch"}),
            ], response)

    def test_subagent_without_an_identifier_invents_no_agent(self):
        self.assertEqual(emit.codex_events({
            "hook_event_name": "SubagentStart", "session_id": "root",
            "agent_type": "reviewer",
        }), [])

    def test_missing_optional_tool_fields_still_report_only_supported_facts(self):
        self.assertEqual(emit.codex_events({"hook_event_name": "PreToolUse"}), [
            ("tool_called", {"tool": "?"}),
        ])
        self.assertEqual(emit.codex_events({"hook_event_name": "PostToolUse",
                                            "tool_name": "apply_patch"}), [
            ("heartbeat", {"tool": "apply_patch"}),
        ])


if __name__ == "__main__":
    unittest.main()
