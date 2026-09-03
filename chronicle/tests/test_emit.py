"""Hook -> event mapping for the emitter (docs/protocol.md, issue #4).

python3 -m unittest discover tests     (run from the repo root)
"""

import fcntl
import glob
import hashlib
import importlib.util
import io
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMIT = os.path.join(ROOT, "hooks", "emit.py")

_spec = importlib.util.spec_from_file_location("chronicle_emit", EMIT)
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

# Both spellings are cleared even though warren#361 left only CHRONICLE_ being read.
# The old names cost nothing to clear and a machine that still exports one should not
# be able to make a test look different from CI.
TRANSPORT_SETTINGS = ("URL", "TOKEN", "MIRROR", "MIRROR_TOKEN", "AGENT_ID", "PROJECT")
BOTH_SPELLINGS = tuple(
    prefix + name
    for name in TRANSPORT_SETTINGS
    for prefix in ("CHRONICLE_", "BURROW_")
)


def without_transport_settings(environ):
    """A copy of ``environ`` with every transport setting, either spelling, gone."""
    return {k: v for k, v in environ.items() if k not in BOTH_SPELLINGS}


def _increment_diagnostics(path, count):
    emit.DIAGNOSTICS = path
    for _ in range(count):
        emit._diagnose("failure", reason="race")


def _race_outbox(path, gate, action, records):
    emit.OUTBOX = path
    emit.OUTBOX_RECORDS = 3
    emit.OUTBOX_BYTES = 100000
    gate.wait()
    if action == "main":
        emit._update_outbox({("t", "delivered")}, records)
    else:
        emit._journal_outbox(records)


def _commit_outbox_record(path, record):
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _defer_events(log, diagnostics, events, gate):
    emit.LOG = log
    emit.DIAGNOSTICS = diagnostics
    emit.DEFERRED_RECORDS = 4
    emit.DEFERRED_BYTES = 100000
    gate.wait()
    for event in events:
        emit._defer_local(event)


@contextmanager
def delayed_target(delay, received=None):
    """A real HTTP boundary whose accepted delivery IDs are observable."""

    class Target(BaseHTTPRequestHandler):
        def do_POST(self):
            time.sleep(delay)
            if received is not None:
                received.append(self.headers["X-Burrow-Delivery-ID"])
            self.send_response(204)
            self.end_headers()

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ToEventTest(unittest.TestCase):
    def test_pre_tool_use_still_starts_a_tool(self):
        etype, payload = emit.to_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "make build"},
            }
        )
        self.assertEqual(etype, "tool_called")
        self.assertEqual(payload["tool"], "Bash")

    def test_write_still_produces_an_artifact(self):
        for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            etype, payload = emit.to_event(
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": tool,
                    "tool_input": {"file_path": "/w/burrow/serve.py"},
                }
            )
            self.assertEqual(etype, "artifact_produced", tool)
            self.assertEqual(payload["artifact"], "/w/burrow/serve.py")

    def test_every_other_tool_finishing_is_a_heartbeat(self):
        for hook in (
            {"tool_name": "Bash", "tool_input": {"command": "make build"}},
            {"tool_name": "Grep", "tool_input": {"pattern": "stale"}},
            {"tool_name": "Read", "tool_input": {"file_path": "/w/README.md"}},
            {"tool_name": "WebSearch", "tool_input": {}},
        ):
            hook["hook_event_name"] = "PostToolUse"
            etype, payload = emit.to_event(hook)
            self.assertEqual(etype, "heartbeat", hook["tool_name"])
            self.assertEqual(payload, {"tool": hook["tool_name"]})

    def test_no_post_tool_use_is_dropped(self):
        """Every completion is a signal; dropping one is what made runs look stale."""
        etype, _ = emit.to_event(
            {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {}}
        )
        self.assertIsNotNone(etype)

    def test_notebook_path_and_failed_tools_are_truthful(self):
        self.assertEqual(
            emit.to_event(
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "NotebookEdit",
                    "tool_input": {"notebook_path": "/w/analysis.ipynb"},
                }
            ),
            ("artifact_produced", {"artifact": "/w/analysis.ipynb"}),
        )
        self.assertEqual(
            emit.to_event(
                {
                    "hook_event_name": "PostToolUseFailure",
                    "tool_name": "Bash",
                    "error": "exit code 2",
                }
            ),
            ("tool_failed", {"tool": "Bash", "error": "exit code 2"}),
        )

    def test_only_real_approval_and_elicitation_notifications_knock(self):
        for notification_type in ("permission_prompt", "elicitation_dialog"):
            self.assertEqual(
                emit.to_event(
                    {
                        "hook_event_name": "Notification",
                        "notification_type": notification_type,
                        "message": "Please decide",
                    }
                ),
                ("needs_human", {"message": "Please decide"}),
            )
        for notification_type in ("idle_prompt", "auth_success", None):
            self.assertEqual(
                emit.to_event(
                    {
                        "hook_event_name": "Notification",
                        "notification_type": notification_type,
                        "message": "informational",
                    }
                ),
                (None, None),
            )


class DetailPolicyTest(unittest.TestCase):
    EVENT = {
        "v": 0,
        "ts": "2026-08-24T12:00:00.000Z",
        "source": "test",
        "agent_id": "test:one",
        "project": "burrow",
        "cwd": "/secret/project",
        "type": "artifact_produced",
        "payload": {"artifact": "/secret/project/notes.md"},
    }

    def delivered_payload(self, runner, hook, policy="safe"):
        event_type, payload = emit.adapt_hook(runner, hook)[0]
        event = dict(self.EVENT, type=event_type, payload=payload)
        posted = []
        with (
            mock.patch.dict(
                os.environ,
                {
                    "CHRONICLE_DETAIL": policy,
                    "CHRONICLE_URL": "http://village",
                    "CHRONICLE_MIRROR": "",
                },
            ),
            mock.patch.object(
                emit,
                "post_event",
                side_effect=lambda url, outgoing, token, delivery_id="": posted.append(
                    outgoing
                )
                or True,
            ),
        ):
            emit.deliver(event)
        self.assertEqual(len(posted), 1)
        return posted[0]["payload"]

    def test_safe_redacts_a_command_containing_a_url_and_query_token(self):
        payload = self.delivered_payload(
            "codex",
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {
                    "command": "curl https://api.example.test/items?token=secret",
                },
            },
        )
        self.assertEqual(payload["detail"], "[redacted]")

    def test_safe_redacts_url_query_description_and_approval_reason(self):
        cases = (
            ("WebFetch", {"url": "https://private.test/team/report"}, "detail"),
            ("WebSearch", {"query": "site:private.test/team report"}, "detail"),
            ("Bash", {"description": "Deploy team/private service"}, "detail"),
        )
        for tool, tool_input, field in cases:
            with self.subTest(tool_input=tool_input):
                payload = self.delivered_payload(
                    "codex",
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": tool,
                        "tool_input": tool_input,
                    },
                )
                self.assertEqual(payload[field], "[redacted]")

        approval = self.delivered_payload(
            "codex",
            {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"description": "Approve team/private deploy"},
            },
        )
        self.assertEqual(approval["message"], "[redacted]")

    def test_safe_basenames_only_explicit_file_and_notebook_paths(self):
        for field, value, expected in (
            ("file_path", "/secret/project/report.txt", "report.txt"),
            ("notebook_path", "/secret/project/analysis.ipynb", "analysis.ipynb"),
            ("file_path", r"C:\secret\project\report.txt", "report.txt"),
            ("notebook_path", r"C:\secret\project\analysis.ipynb", "analysis.ipynb"),
        ):
            with self.subTest(field=field):
                hook = {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Read",
                    "tool_input": {field: value},
                }
                original = json.loads(json.dumps(hook))
                payload = self.delivered_payload("codex", hook)
                self.assertEqual(payload["detail"], expected)
                self.assertEqual(hook, original)

    def test_full_keeps_and_off_redacts_tool_detail(self):
        hook = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "curl https://private.test/team"},
        }
        original = json.loads(json.dumps(hook))
        full_detail = self.delivered_payload("codex", hook, "full")["detail"]
        self.assertEqual(full_detail, hook["tool_input"]["command"])
        self.assertIs(type(full_detail), str)
        self.assertEqual(
            self.delivered_payload("codex", hook, "off")["detail"], "[redacted]"
        )
        self.assertEqual(hook, original)

    def test_full_safe_and_off_are_applied_without_mutating_input(self):
        for policy, cwd, artifact in (
            ("full", "/secret/project", "/secret/project/notes.md"),
            ("safe", "", "notes.md"),
            ("off", "", "[redacted]"),
        ):
            with (
                self.subTest(policy),
                mock.patch.dict(os.environ, {"CHRONICLE_DETAIL": policy}),
            ):
                redacted = emit.redact_event(self.EVENT)
                self.assertEqual(redacted["cwd"], cwd)
                self.assertEqual(redacted["payload"]["artifact"], artifact)
        self.assertEqual(self.EVENT["cwd"], "/secret/project")
        self.assertEqual(self.EVENT["payload"]["artifact"], "/secret/project/notes.md")

    def test_unknown_policy_fails_private(self):
        with mock.patch.dict(os.environ, {"CHRONICLE_DETAIL": "typo"}):
            self.assertEqual(emit.detail_policy(), "safe")

    def test_delivery_redacts_before_each_remote_transport(self):
        posted = []
        with (
            mock.patch.dict(
                os.environ,
                {
                    "CHRONICLE_DETAIL": "safe",
                    "CHRONICLE_URL": "http://village",
                    "CHRONICLE_MIRROR": "http://mirror",
                },
            ),
            mock.patch.object(
                emit,
                "post_event",
                side_effect=lambda url, event, token, delivery_id="": posted.append(
                    event
                )
                or True,
            ),
        ):
            emit.deliver(self.EVENT)
        self.assertEqual(len(posted), 2)
        self.assertTrue(all(event["cwd"] == "" for event in posted))
        self.assertTrue(
            all(event["payload"]["artifact"] == "notes.md" for event in posted)
        )

    def test_local_fallback_is_also_redacted(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                os.environ,
                {
                    "CHRONICLE_DETAIL": "off",
                    "CHRONICLE_URL": "",
                    "CHRONICLE_MIRROR": "",
                },
            ),
            mock.patch.object(emit, "LOG_DIR", directory),
            mock.patch.object(emit, "LOG", os.path.join(directory, "events.jsonl")),
        ):
            emit.deliver(self.EVENT)
            with open(emit.LOG, encoding="utf-8") as stream:
                written = json.load(stream)
        self.assertEqual(written["cwd"], "")
        self.assertEqual(written["payload"]["artifact"], "[redacted]")

    def project_from_main(self, cwd, policy, project=None):
        delivered = []
        environment = {"CHRONICLE_DETAIL": policy}
        if project is not None:
            environment["CHRONICLE_PROJECT"] = project
        hook = {"hook_event_name": "Stop", "session_id": "s1", "cwd": cwd}
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(hook))),
            mock.patch.object(emit, "deliver", side_effect=delivered.append),
        ):
            emit.main()
        self.assertEqual(len(delivered), 1)
        return delivered[0]["project"]

    def test_windows_cwd_uses_private_cross_platform_basename(self):
        cwd = r"C:\Users\alice\secret-project"
        for policy in ("safe", "off"):
            with self.subTest(policy=policy):
                project = self.project_from_main(cwd, policy)
                self.assertEqual(project, "secret-project")
                self.assertNotIn("alice", project)

    def test_explicit_project_is_preserved_for_windows_cwd(self):
        self.assertEqual(
            self.project_from_main(
                r"C:\Users\alice\secret-project", "off", "public-label"
            ),
            "public-label",
        )


class EndToEndTest(unittest.TestCase):
    """Run the real hook script and read back the event it appended."""

    def run_hook(self, home, hook):
        env = without_transport_settings(os.environ)
        env |= {"HOME": home, "CHRONICLE_MIRROR": ""}
        proc = subprocess.run(
            [sys.executable, EMIT],
            input=json.dumps(hook),
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_long_bash_run_writes_a_heartbeat(self):
        with tempfile.TemporaryDirectory() as home:
            self.run_hook(
                home,
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "s1",
                    "cwd": "/w/burrow",
                    "tool_name": "Bash",
                    "tool_input": {"command": "make build"},
                },
            )
            with open(os.path.join(home, ".chronicle", "events.jsonl")) as f:
                events = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["type"], "heartbeat")
        self.assertEqual(event["payload"], {"tool": "Bash"})
        self.assertEqual(event["agent_id"], "claude-code:s1")
        self.assertEqual(event["project"], "burrow")
        self.assertTrue(event["ts"].endswith("Z"))

    def test_total_storage_failure_is_exit_zero_with_bounded_private_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            unusable_home = os.path.join(directory, "not-a-directory")
            with open(unusable_home, "w", encoding="utf-8") as stream:
                stream.write("x")
            env = dict(
                os.environ,
                HOME=unusable_home,
                CHRONICLE_URL="http://offline",
                CHRONICLE_MIRROR="",
                CHRONICLE_DETAIL="off",
            )
            proc = subprocess.run(
                [sys.executable, EMIT],
                input=json.dumps(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "secret-session",
                        "cwd": "/private/secret/project",
                    }
                ),
                text=True,
                capture_output=True,
                env=env,
            )
        self.assertEqual(proc.returncode, 0)
        self.assertRegex(proc.stderr, r"^chronicle transport failure: \w+\n$")
        self.assertNotIn("secret", proc.stderr)
        self.assertLess(len(proc.stderr), 128)


class SettingResolutionTest(unittest.TestCase):
    """CHRONICLE_ is the only spelling read, and an empty value is a value.

    The pre-rename BURROW_ names were read alongside these until warren#361: hook
    environment is frozen when a session starts, so the rename had to keep both
    alive until every session holding an old one had ended. They have, and the
    burrow's own environment was checked to be free of the old names before the
    fallback went — so what these tests now pin is that a stale name is *ignored*
    rather than quietly honoured.
    """

    def setUp(self):
        # The whole environment, saved and restored — not a hand-kept list of names.
        # A list has to be extended every time a test touches a new setting, and the
        # time it was not, `CHRONICLE_DETAIL` escaped this class and redacted the
        # payloads of every module that ran after it in the same interpreter. That was
        # invisible to `tests/run.sh`, which gives each module its own process
        # (warren#361).
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in BOTH_SPELLINGS:
            os.environ.pop(key, None)

    def test_the_pre_rename_spelling_routes_nothing(self):
        """A stale BURROW_URL is not a target; it is not read at all."""
        os.environ["BURROW_URL"] = "http://village:8737"
        os.environ["BURROW_TOKEN"] = "s3cret"
        os.environ["CHRONICLE_MIRROR"] = ""
        self.assertEqual(emit.targets(), [])

    def test_the_new_spelling_is_the_only_one_read(self):
        os.environ["BURROW_URL"] = "http://old:8737"
        os.environ["CHRONICLE_URL"] = "http://new:8737"
        os.environ["BURROW_TOKEN"] = "old-secret"
        os.environ["CHRONICLE_TOKEN"] = "new-secret"
        os.environ["BURROW_MIRROR"] = "http://old-mirror:9000"
        os.environ["CHRONICLE_MIRROR"] = ""
        self.assertEqual(emit.targets(), [("http://new:8737", "new-secret")])

    def test_an_empty_value_turns_the_mirror_off(self):
        '''"" is a value, not an absence: it must not read as "no setting".'''
        os.environ["CHRONICLE_URL"] = "http://village:8737"
        os.environ["CHRONICLE_MIRROR"] = ""
        self.assertEqual(emit.targets(), [("http://village:8737", "")])

    def test_no_setting_at_all_still_means_mirror_to_the_local_dev_server(self):
        """``None`` and ``""`` have to stay distinguishable, or the default is lost."""
        self.assertEqual(emit.targets(), [(emit.DEFAULT_MIRROR, "")])

    def test_the_resident_identity_reads_the_new_spelling(self):
        os.environ["CHRONICLE_AGENT_ID"] = "life-agent"
        self.assertEqual(emit._setting("AGENT_ID"), "life-agent")

    def test_a_pre_rename_identity_is_not_read(self):
        os.environ["BURROW_AGENT_ID"] = "life-agent"
        self.assertIsNone(emit._setting("AGENT_ID"))

    def test_the_detail_policy_defaults_to_full_and_reads_the_new_spelling(self):
        self.assertEqual(emit.detail_policy(), "full")
        os.environ["BURROW_DETAIL"] = "safe"
        self.assertEqual(emit.detail_policy(), "full")
        os.environ["CHRONICLE_DETAIL"] = "off"
        self.assertEqual(emit.detail_policy(), "off")


class StateDirectoryTest(unittest.TestCase):
    """Where the offline fallback log lives, now that the rename is finished."""

    def resolved(self, existing):
        with tempfile.TemporaryDirectory() as home:
            for name in existing:
                os.mkdir(os.path.join(home, name))
            with mock.patch.dict(os.environ, {"HOME": home}):
                return os.path.relpath(emit._state_dir(), home)

    def test_a_machine_with_neither_directory_gets_the_new_name(self):
        self.assertEqual(self.resolved([]), ".chronicle")

    def test_a_leftover_burrow_directory_is_no_longer_adopted(self):
        """warren#361: the fallback is gone, so the old spool is a static archive."""
        self.assertEqual(self.resolved([".burrow"]), ".chronicle")

    def test_the_new_directory_is_used_when_both_exist(self):
        self.assertEqual(self.resolved([".chronicle", ".burrow"]), ".chronicle")

    def test_the_emitter_writes_the_new_directory_beside_a_leftover_burrow(self):
        """End to end: the real script, a HOME that predates the rename.

        The negative half is the one that matters and it can fail: before
        warren#361 this same run wrote ``.burrow/events.jsonl`` and created no
        ``.chronicle`` at all.
        """
        with tempfile.TemporaryDirectory() as home:
            os.mkdir(os.path.join(home, ".burrow"))
            env = without_transport_settings(os.environ)
            env |= {"HOME": home, "CHRONICLE_MIRROR": ""}
            proc = subprocess.run(
                [sys.executable, EMIT],
                input=json.dumps(
                    {
                        "hook_event_name": "PostToolUse",
                        "session_id": "s1",
                        "cwd": "/w/chronicle",
                        "tool_name": "Bash",
                        "tool_input": {"command": "make build"},
                    }
                ),
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(
                os.path.exists(os.path.join(home, ".chronicle", "events.jsonl"))
            )
            self.assertFalse(
                os.path.exists(os.path.join(home, ".burrow", "events.jsonl"))
            )


class TargetsTest(unittest.TestCase):
    """Delivery targets: the village plus any mirror (a local dev server)."""

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in BOTH_SPELLINGS}
        for k in self.saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_local_dev_server_is_mirrored_by_default(self):
        """The whole point: run serve.py locally and see the live fleet without
        touching settings or deploying to the village."""
        os.environ["CHRONICLE_URL"] = "http://village:8737"
        os.environ["CHRONICLE_TOKEN"] = "s3cret"
        self.assertEqual(
            emit.targets(),
            [("http://village:8737", "s3cret"), (emit.DEFAULT_MIRROR, "")],
        )

    def test_mirror_never_gets_the_village_secret(self):
        os.environ["CHRONICLE_URL"] = "http://village:8737"
        os.environ["CHRONICLE_TOKEN"] = "s3cret"
        os.environ["CHRONICLE_MIRROR"] = "http://127.0.0.1:9000"
        self.assertEqual(dict(emit.targets())["http://127.0.0.1:9000"], "")
        os.environ["CHRONICLE_MIRROR_TOKEN"] = "dev"
        self.assertEqual(dict(emit.targets())["http://127.0.0.1:9000"], "dev")

    def test_empty_mirror_turns_it_off(self):
        os.environ["CHRONICLE_URL"] = "http://village:8737"
        os.environ["CHRONICLE_MIRROR"] = ""
        self.assertEqual(emit.targets(), [("http://village:8737", "")])

    def test_a_url_named_twice_does_not_double_the_event(self):
        os.environ["CHRONICLE_URL"] = "http://127.0.0.1:8737/"
        self.assertEqual(emit.targets(), [("http://127.0.0.1:8737", "")])

    def test_one_breaker_per_target(self):
        """A village that is down must not silence the dev server beside it."""
        self.assertNotEqual(
            emit.breaker_path("http://village:8737"),
            emit.breaker_path(emit.DEFAULT_MIRROR),
        )
        self.assertTrue(emit.is_loopback(emit.DEFAULT_MIRROR))
        self.assertFalse(emit.is_loopback("http://village:8737"))


class MirrorDeliveryTest(unittest.TestCase):
    """main() with a live village and a live mirror: both see the event, and
    nothing is written locally. With both down, the local log still catches it."""

    def emit_one(self, home, urls_up):
        posted = []

        class Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            if req.full_url not in urls_up:
                raise OSError("refused")
            posted.append(req.full_url)
            return Ctx()

        env = {"CHRONICLE_URL": "http://village:8737", "CHRONICLE_TOKEN": "s3cret"}
        saved_env = {k: os.environ.get(k) for k in BOTH_SPELLINGS}
        saved = (
            emit.LOG_DIR,
            emit.LOG,
            emit.OUTBOX,
            emit.DIAGNOSTICS,
            emit.BREAKER,
            emit.urllib.request.urlopen,
            sys.stdin,
        )
        for key in BOTH_SPELLINGS:
            os.environ.pop(key, None)
        os.environ.update(env)
        emit.LOG_DIR = home
        emit.LOG = os.path.join(home, "events.jsonl")
        emit.OUTBOX = os.path.join(home, "primary-outbox.jsonl")
        emit.DIAGNOSTICS = os.path.join(home, "transport-diagnostics.json")
        emit.BREAKER = os.path.join(home, ".post-failed")
        emit.urllib.request.urlopen = fake_urlopen
        sys.stdin = io.StringIO(
            json.dumps(
                {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/w/burrow"}
            )
        )
        try:
            emit.main()
        finally:
            (
                emit.LOG_DIR,
                emit.LOG,
                emit.OUTBOX,
                emit.DIAGNOSTICS,
                emit.BREAKER,
                emit.urllib.request.urlopen,
                sys.stdin,
            ) = saved
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
                home, {"http://village:8737/events", emit.DEFAULT_MIRROR + "/events"}
            )
        self.assertEqual(len(posted), 2, posted)
        self.assertEqual(lines, 0, "delivered remotely, so nothing to log locally")

    def test_mirror_alone_does_not_ack_the_primary(self):
        with tempfile.TemporaryDirectory() as home:
            posted, lines = self.emit_one(home, {emit.DEFAULT_MIRROR + "/events"})
        self.assertEqual(posted, [emit.DEFAULT_MIRROR + "/events"])
        self.assertEqual(lines, 1)

    def test_nothing_up_still_falls_back_to_the_local_log(self):
        with tempfile.TemporaryDirectory() as home:
            posted, lines = self.emit_one(home, set())
        self.assertEqual(posted, [])
        self.assertEqual(lines, 1)


class DurablePrimaryDeliveryTest(unittest.TestCase):
    EVENT = {
        "v": 0,
        "ts": "2026-08-24T12:00:00.000Z",
        "source": "test",
        "agent_id": "test:one",
        "project": "burrow",
        "cwd": "/private/work",
        "type": "tool_called",
        "payload": {"tool": "Read", "detail": "/private/a"},
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patches = [
            mock.patch.object(emit, "LOG_DIR", self.tmp.name),
            mock.patch.object(emit, "LOG", os.path.join(self.tmp.name, "events.jsonl")),
            mock.patch.object(
                emit, "OUTBOX", os.path.join(self.tmp.name, "outbox.jsonl")
            ),
            mock.patch.object(
                emit,
                "DIAGNOSTICS",
                os.path.join(self.tmp.name, "transport-diagnostics.json"),
            ),
            mock.patch.object(
                emit, "BREAKER", os.path.join(self.tmp.name, ".post-failed")
            ),
            mock.patch.dict(
                os.environ,
                {
                    "CHRONICLE_DETAIL": "safe",
                    "CHRONICLE_URL": "http://primary",
                    "CHRONICLE_MIRROR": "http://mirror",
                },
            ),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def outbox(self):
        if not os.path.exists(emit.OUTBOX):
            return []
        with open(emit.OUTBOX, encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]

    @staticmethod
    def deferred_threads():
        deferred = []

        class DeferredThread:
            def __init__(self, target, args=(), **_kwargs):
                self.target = target
                self.args = args

            def start(self):
                deferred.append(self)

            def join(self, _timeout=None):
                pass

            def is_alive(self):
                return False

            def run(self):
                self.target(*self.args)

        return deferred, DeferredThread

    def test_slow_link_acknowledges_every_post_completed_before_the_deadline(self):
        delivered = []
        with delayed_target(0.08, delivered) as target:
            with (
                mock.patch.dict(
                    os.environ, {"CHRONICLE_URL": target, "CHRONICLE_MIRROR": ""}
                ),
                mock.patch.object(emit, "HOOK_BUDGET", 0.35),
                mock.patch.object(emit, "POST_TIMEOUT", 0.2),
                mock.patch.object(emit, "post_event", return_value=False),
            ):
                for second in range(3):
                    emit.deliver(
                        dict(self.EVENT, ts="2026-08-24T12:00:0%d.000Z" % second)
                    )
            before = len(self.outbox())

            with (
                mock.patch.dict(
                    os.environ, {"CHRONICLE_URL": target, "CHRONICLE_MIRROR": ""}
                ),
                mock.patch.object(emit, "HOOK_BUDGET", 0.35),
                mock.patch.object(emit, "POST_TIMEOUT", 0.2),
            ):
                emit.deliver(dict(self.EVENT, ts="2026-08-24T12:00:03.000Z"))

        self.assertTrue(delivered)
        self.assertLess(len(self.outbox()), before)

    def test_ack_reserve_harvests_a_response_published_after_the_post_window(self):
        """Scheduler delay after HTTP acceptance may consume part of the ack tail."""
        delivered = []
        with delayed_target(0.08, delivered) as target:
            with (
                mock.patch.dict(
                    os.environ, {"CHRONICLE_URL": target, "CHRONICLE_MIRROR": ""}
                ),
                mock.patch.object(emit, "post_event", return_value=False),
            ):
                for second in range(3):
                    emit.deliver(
                        dict(self.EVENT, ts="2026-08-24T12:00:0%d.000Z" % second)
                    )
            before = self.outbox()
            real_post = emit.post_event

            def scheduled_after_response(*args, **kwargs):
                accepted = real_post(*args, **kwargs)
                time.sleep(0.24)
                return accepted

            with (
                mock.patch.dict(
                    os.environ, {"CHRONICLE_URL": target, "CHRONICLE_MIRROR": ""}
                ),
                mock.patch.object(emit, "HOOK_BUDGET", 0.45),
                mock.patch.object(emit, "ACK_RESERVE", 0.15),
                mock.patch.object(emit, "POST_TIMEOUT", 0.2),
                mock.patch.object(emit, "post_event", side_effect=scheduled_after_response),
            ):
                emit.deliver(dict(self.EVENT, ts="2026-08-24T12:00:03.000Z"))

        self.assertTrue(delivered)
        remaining = self.outbox()
        self.assertLessEqual(len(remaining), len(before))
        self.assertTrue(
            set(delivered).isdisjoint(record["delivery_id"] for record in remaining)
        )

    def test_deferred_primary_worker_keeps_its_calls_transport_and_diagnostics(self):
        deferred, deferred_thread = self.deferred_threads()

        this_call = mock.Mock(return_value=True)
        with (
            mock.patch.dict(
                os.environ,
                {"CHRONICLE_URL": "http://primary", "CHRONICLE_MIRROR": ""},
            ),
            mock.patch.object(emit, "post_event", this_call),
            mock.patch.object(emit.threading, "Thread", deferred_thread),
        ):
            emit.deliver(self.EVENT, deadline=time.monotonic() + 1)
        with open(emit.DIAGNOSTICS, encoding="utf-8") as stream:
            retries_before_worker = json.load(stream).get("retries", 0)

        next_call = mock.Mock(return_value=True)
        next_diagnostics = os.path.join(self.tmp.name, "next-diagnostics.json")
        with (
            mock.patch.object(emit, "post_event", next_call),
            mock.patch.object(emit, "DIAGNOSTICS", next_diagnostics),
        ):
            for worker in deferred:
                worker.run()

        this_call.assert_called_once()
        next_call.assert_not_called()
        self.assertFalse(os.path.exists(next_diagnostics))
        with open(emit.DIAGNOSTICS, encoding="utf-8") as stream:
            self.assertEqual(
                json.load(stream)["retries"], retries_before_worker + 1
            )

    def test_delayed_delivery_keeps_its_outbox_diagnostic_destination(self):
        entered_outbox_diagnostic = threading.Event()
        release_outbox_diagnostic = threading.Event()
        transport_finished = threading.Event()
        failures = []
        original_time = emit.time.time

        def offline_primary(*_args, **_kwargs):
            transport_finished.set()
            return False

        def pause_after_transport():
            if transport_finished.is_set() and threading.current_thread() is delivery:
                entered_outbox_diagnostic.set()
                release_outbox_diagnostic.wait(5)
            return original_time()

        def deliver_in_background():
            try:
                emit.deliver(self.EVENT)
            except Exception as error:  # surface thread failures in the test
                failures.append(error)

        first_diagnostics = emit.DIAGNOSTICS
        later_diagnostics = os.path.join(self.tmp.name, "later-diagnostics.json")
        with (
            mock.patch.dict(os.environ, {"CHRONICLE_MIRROR": ""}),
            mock.patch.object(emit, "post_event", side_effect=offline_primary),
            mock.patch.object(emit.time, "time", side_effect=pause_after_transport),
        ):
            delivery = threading.Thread(target=deliver_in_background)
            delivery.start()
            self.assertTrue(entered_outbox_diagnostic.wait(5))
            with mock.patch.object(emit, "DIAGNOSTICS", later_diagnostics):
                release_outbox_diagnostic.set()
                delivery.join(5)

        self.assertFalse(delivery.is_alive())
        self.assertEqual(failures, [])
        with open(first_diagnostics, encoding="utf-8") as stream:
            self.assertIn("outbox", json.load(stream))
        with open(later_diagnostics, encoding="utf-8") as stream:
            self.assertNotIn("outbox", json.load(stream))

    def assert_deferred_real_transport_keeps_settings(self, *, primary):
        deferred, deferred_thread = self.deferred_threads()
        first_opener = mock.MagicMock()
        first_opener.return_value.__enter__.return_value = mock.MagicMock()
        settings = {
            "CHRONICLE_URL": "http://primary" if primary else "",
            "CHRONICLE_MIRROR": "" if primary else "http://mirror",
        }
        with (
            mock.patch.dict(os.environ, settings),
            mock.patch.object(emit, "POST_TIMEOUT", 0.11),
            mock.patch.object(emit.urllib.request, "urlopen", first_opener),
            mock.patch.object(emit.threading, "Thread", deferred_thread),
        ):
            emit.deliver(self.EVENT, deadline=time.monotonic() + 1)

        next_opener = mock.MagicMock()
        next_log = os.path.join(self.tmp.name, "next-log")
        with (
            mock.patch.object(emit, "POST_TIMEOUT", 0.99),
            mock.patch.object(emit, "BREAKER", os.path.join(next_log, ".breaker")),
            mock.patch.object(emit, "LOG_DIR", next_log),
            mock.patch.object(emit.urllib.request, "urlopen", next_opener),
        ):
            for worker in deferred:
                worker.run()

        self.assertEqual(first_opener.call_count, 1)
        self.assertEqual(first_opener.call_args.kwargs["timeout"], 0.11)
        next_opener.assert_not_called()
        self.assertFalse(os.path.exists(next_log))

    def test_deferred_primary_worker_keeps_real_transport_settings(self):
        self.assert_deferred_real_transport_keeps_settings(primary=True)

    def test_deferred_mirror_worker_keeps_real_transport_settings(self):
        self.assert_deferred_real_transport_keeps_settings(primary=False)

    def test_a_slow_retry_diagnostic_cannot_turn_an_accepted_event_into_fallback(self):
        """Acceptance is transport state before best-effort diagnostic fsync (#333)."""
        with (
            mock.patch.dict(
                os.environ,
                {"CHRONICLE_URL": "http://primary", "CHRONICLE_MIRROR": ""},
            ),
            mock.patch.object(emit, "HOOK_BUDGET", 0.5),
            mock.patch.object(emit, "ACK_RESERVE", 0.1),
            mock.patch.object(emit, "POST_TIMEOUT", 0.05),
            mock.patch.object(emit, "post_event", return_value=True),
            mock.patch.object(
                emit,
                "_diagnose",
                side_effect=lambda kind, **_kwargs: (
                    time.sleep(0.5) if kind == "retry" else None
                ),
            ),
        ):
            emit.deliver(self.EVENT)

        self.assertEqual(self.outbox(), [])
        self.assertFalse(
            os.path.exists(emit.LOG), "an accepted event must not be duplicated locally"
        )

    def test_slow_retry_diagnostic_keeps_unattempted_records_durable(self):
        with (
            mock.patch.dict(
                os.environ,
                {"CHRONICLE_URL": "http://primary", "CHRONICLE_MIRROR": ""},
            ),
            mock.patch.object(emit, "post_event", return_value=False),
        ):
            emit.deliver(dict(self.EVENT, ts="older"))
        older_id = self.outbox()[0]["delivery_id"]

        with (
            mock.patch.dict(
                os.environ,
                {"CHRONICLE_URL": "http://primary", "CHRONICLE_MIRROR": ""},
            ),
            mock.patch.object(emit, "HOOK_BUDGET", 0.5),
            mock.patch.object(emit, "ACK_RESERVE", 0.1),
            mock.patch.object(emit, "POST_TIMEOUT", 0.05),
            mock.patch.object(emit, "post_event", return_value=True),
            mock.patch.object(
                emit,
                "_diagnose",
                side_effect=lambda kind, **_kwargs: (
                    time.sleep(0.5) if kind == "retry" else None
                ),
            ),
        ):
            emit.deliver(dict(self.EVENT, ts="current"))

        remaining = self.outbox()
        self.assertEqual(len(remaining), 1)
        self.assertNotEqual(remaining[0]["delivery_id"], older_id)
        self.assertEqual(remaining[0]["event"]["ts"], "current")

    def test_capped_outbox_without_acknowledgements_is_named_stuck(self):
        with (
            mock.patch.object(emit, "OUTBOX_RECORDS", 2),
            mock.patch.object(emit, "STUCK_OUTBOX_HOOKS", 2),
            mock.patch.object(emit, "post_event", return_value=False),
        ):
            for second in range(2):
                emit.deliver(
                    dict(self.EVENT, ts="2026-08-24T12:00:0%d.000Z" % second)
                )

        with open(emit.DIAGNOSTICS, encoding="utf-8") as stream:
            outbox = json.load(stream)["outbox"]
        self.assertEqual(outbox["status"], "stuck")
        self.assertEqual(outbox["records"], 2)
        self.assertEqual(outbox["capacity"], 2)
        self.assertEqual(outbox["hooks_without_ack"], 2)
        self.assertGreaterEqual(outbox["oldest_age_seconds"], 0)
        self.assertTrue(outbox["oldest_queued_at"].endswith("Z"))

    def test_status_command_names_the_stuck_outbox_for_an_operator(self):
        report = {
            "outbox": {
                "status": "stuck",
                "records": 1024,
                "capacity": 1024,
                "oldest_queued_at": "2026-09-01T11:38:00.000Z",
                "oldest_age_seconds": 90000,
                "hooks_without_ack": 10,
                "last_ack_at": None,
            }
        }
        with open(emit.DIAGNOSTICS, "w", encoding="utf-8") as stream:
            json.dump(report, stream)
        output = io.StringIO()

        with mock.patch.object(sys, "stdout", output):
            emit.print_emitter_status()

        self.assertEqual(
            output.getvalue(),
            "chronicle emitter outbox: stuck; 1024/1024 queued; oldest "
            "2026-09-01T11:38:00.000Z (90000s); 10 hooks without ack; last ack never\n",
        )

    def test_acknowledgement_precedes_stalled_transport_diagnostics(self):
        with mock.patch.object(emit, "post_event", return_value=False):
            emit.deliver(self.EVENT)
        queued_id = self.outbox()[0]["delivery_id"]

        with open(emit.DIAGNOSTICS + ".lock", "a+") as diagnostic_lock:
            fcntl.flock(diagnostic_lock, fcntl.LOCK_EX)
            with (
                mock.patch.object(emit, "HOOK_BUDGET", 0.2),
                mock.patch.object(emit, "ACK_RESERVE", 0.1),
                mock.patch.object(emit, "POST_TIMEOUT", 0.01),
                mock.patch.object(emit, "post_event", return_value=True),
            ):
                worker = threading.Thread(
                    target=emit.deliver,
                    args=(dict(self.EVENT, ts="2026-08-24T12:00:01.000Z"),),
                )
                worker.start()
                # Polled, not slept: the claim is that the ack lands *before* the worker
                # blocks on the diagnostics lock, and a fixed wait turns that into a claim
                # about how fast the machine is. A loaded CI runner failed this on 0.25 s
                # while the ordering it tests was perfectly correct (warren#361). The
                # deadline is generous because it is only ever paid by a real failure.
                deadline = time.monotonic() + 5
                queued = [record["delivery_id"] for record in self.outbox()]
                while queued_id in queued and time.monotonic() < deadline:
                    time.sleep(0.01)
                    queued = [record["delivery_id"] for record in self.outbox()]
                self.assertNotIn(queued_id, queued)
                fcntl.flock(diagnostic_lock, fcntl.LOCK_UN)
                worker.join(1)
                self.assertFalse(worker.is_alive())

    def test_replay_batch_is_sized_by_available_time_instead_of_sixteen_records(self):
        with delayed_target(0.01) as target:
            with (
                mock.patch.dict(
                    os.environ, {"CHRONICLE_URL": target, "CHRONICLE_MIRROR": ""}
                ),
                mock.patch.object(emit, "post_event", return_value=False),
            ):
                for second in range(30):
                    emit.deliver(dict(self.EVENT, ts="seed-%02d" % second))
            before = len(self.outbox())

            with mock.patch.dict(
                os.environ, {"CHRONICLE_URL": target, "CHRONICLE_MIRROR": ""}
            ):
                emit.deliver(dict(self.EVENT, ts="current"))

        self.assertLess(len(self.outbox()), before - 15)

    def test_delayed_link_drains_a_capacity_outbox_without_reposting_acked_ids(self):
        received = []
        with delayed_target(0.08, received) as target:
            target_key = emit._target_id(target)
            records = emit._stamp_enqueue_order(
                [
                    {
                        "target": target_key,
                        "delivery_id": "queued-%04d" % index,
                        "event": dict(self.EVENT, ts="seed-%04d" % index),
                    }
                    for index in range(emit.OUTBOX_RECORDS)
                ]
            )
            self.assertTrue(emit._update_outbox(set(), records)[1])

            with (
                mock.patch.dict(
                    os.environ, {"CHRONICLE_URL": target, "CHRONICLE_MIRROR": ""}
                ),
                mock.patch.object(emit, "HOOK_BUDGET", 1.0),
                mock.patch.object(emit, "ACK_RESERVE", 0.1),
                mock.patch.object(emit, "POST_TIMEOUT", 0.75),
            ):
                for index in range(400):
                    emit.deliver(dict(self.EVENT, ts="hook-%04d" % index))
                    remaining = len(self.outbox())
                    if not remaining:
                        break

        self.assertEqual(remaining, 0)
        self.assertEqual(len(received), len(set(received)))
        with open(emit.DIAGNOSTICS, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["outbox"]["records"], 0)

    def test_mirror_success_does_not_ack_primary_and_later_hook_replays_oldest_first(
        self,
    ):
        calls = []

        def offline_primary(url, event, token="", delivery_id=""):
            calls.append((url, event["ts"], delivery_id))
            return url == "http://mirror"

        with mock.patch.object(emit, "post_event", side_effect=offline_primary):
            emit.deliver(self.EVENT)
        queued = self.outbox()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["event"]["cwd"], "")
        self.assertEqual(queued[0]["event"]["payload"]["detail"], "[redacted]")

        recovered = dict(self.EVENT, ts="2026-08-24T12:00:01.000Z")
        calls.clear()
        with mock.patch.object(
            emit,
            "post_event",
            side_effect=lambda url, event, token="", delivery_id="": calls.append(
                (url, event["ts"], delivery_id)
            )
            or True,
        ):
            emit.deliver(recovered)
        primary_times = [ts for url, ts, _ in calls if url == "http://primary"]
        self.assertEqual(primary_times, [self.EVENT["ts"], recovered["ts"]])
        self.assertEqual(self.outbox(), [])

    def test_retry_keeps_a_stable_delivery_id_for_server_deduplication(self):
        ids = []
        with mock.patch.object(
            emit,
            "post_event",
            side_effect=lambda url, event, token="", delivery_id="": ids.append(
                delivery_id
            )
            or url == "http://mirror",
        ):
            emit.deliver(self.EVENT)
        with mock.patch.object(
            emit,
            "post_event",
            side_effect=lambda url, event, token="", delivery_id="": ids.append(
                delivery_id
            )
            or True,
        ):
            emit.deliver(dict(self.EVENT, ts="2026-08-24T12:00:01.000Z"))
        primary_ids = [value for value in ids if value]
        self.assertGreaterEqual(len(primary_ids), 3)
        self.assertEqual(primary_ids[0], primary_ids[1])

    def test_independent_targets_share_one_hook_latency_budget(self):
        with (
            mock.patch.dict(
                os.environ,
                {
                    "CHRONICLE_URL": "http://one,http://two",
                    "CHRONICLE_MIRROR": "http://three",
                },
            ),
            mock.patch.object(
                emit,
                "post_event",
                side_effect=lambda *args, **kwargs: time.sleep(0.2) or False,
            ),
        ):
            started = time.monotonic()
            emit.deliver(self.EVENT)
        self.assertLess(time.monotonic() - started, 0.45)
        with open(emit.DIAGNOSTICS, encoding="utf-8") as stream:
            report = json.load(stream)
        self.assertGreaterEqual(report["failures"], 1)
        self.assertLessEqual(report["recent"].__len__(), emit.DIAGNOSTIC_HISTORY)

    def test_outbox_capacity_drops_oldest_with_a_bounded_diagnostic(self):
        with (
            mock.patch.object(emit, "OUTBOX_RECORDS", 2),
            mock.patch.object(emit, "post_event", return_value=False),
        ):
            for second in range(3):
                emit.deliver(dict(self.EVENT, ts=f"2026-08-24T12:00:0{second}.000Z"))
        queued = self.outbox()
        self.assertEqual(
            [record["event"]["ts"] for record in queued],
            ["2026-08-24T12:00:01.000Z", "2026-08-24T12:00:02.000Z"],
        )
        with open(emit.DIAGNOSTICS, encoding="utf-8") as stream:
            report = json.load(stream)
        self.assertGreaterEqual(report["drops"], 1)

    def test_each_primary_keeps_an_independent_pending_record(self):
        with (
            mock.patch.dict(
                os.environ,
                {
                    "CHRONICLE_URL": "http://one,http://two",
                    "CHRONICLE_MIRROR": "",
                },
            ),
            mock.patch.object(emit, "post_event", return_value=False),
        ):
            emit.deliver(self.EVENT)
        queued = self.outbox()
        self.assertEqual(len(queued), 2)
        self.assertEqual(len({record["target"] for record in queued}), 2)
        self.assertEqual(len({record["delivery_id"] for record in queued}), 1)

    def test_interrupted_rewrite_never_promotes_an_unverified_temp(self):
        with mock.patch.object(emit, "post_event", return_value=False):
            emit.deliver(self.EVENT)
        before = self.outbox()
        newer = dict(self.EVENT, ts="2026-08-24T12:00:01.000Z")
        with (
            mock.patch.object(emit, "post_event", return_value=False),
            mock.patch.object(emit.os, "replace", side_effect=OSError("crash")),
        ):
            emit.deliver(newer)
        self.assertEqual(self.outbox(), before)
        self.assertTrue(os.path.exists(emit.OUTBOX + ".pending"))
        for orphan in (
            "",
            '{"target":"valid-prefix"}\n',
            json.dumps({"target": "valid-prefix"}) + "\n{",
        ):
            with open(emit.OUTBOX + ".pending", "w", encoding="utf-8") as stream:
                stream.write(orphan)
            emit._recover_outbox()
            self.assertEqual(self.outbox(), before)
            self.assertFalse(os.path.exists(emit.OUTBOX + ".pending"))

    def test_corrupt_diagnostics_cannot_block_local_fallback_or_outbox(self):
        with open(emit.DIAGNOSTICS, "w", encoding="utf-8") as stream:
            stream.write('{"failures":"not-a-counter","recent":42}')
        with mock.patch.object(emit, "post_event", return_value=False):
            emit.deliver(self.EVENT)
        self.assertEqual(len(self.outbox()), 1)
        with open(emit.LOG, encoding="utf-8") as stream:
            self.assertEqual(sum(1 for line in stream if line.strip()), 1)
        with open(emit.DIAGNOSTICS, encoding="utf-8") as stream:
            report = json.load(stream)
        self.assertTrue(any(item["kind"] == "repair" for item in report["recent"]))

    def test_contended_outbox_never_waits_and_current_event_is_local(self):
        lock_path = emit.OUTBOX + ".lock"
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "a+") as held:
            emit.fcntl.flock(held, emit.fcntl.LOCK_EX | emit.fcntl.LOCK_NB)
            started = time.monotonic()
            with mock.patch.object(emit, "post_event", return_value=False):
                emit.deliver(self.EVENT)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.2)
        with open(emit.LOG, encoding="utf-8") as stream:
            self.assertEqual(sum(1 for line in stream if line.strip()), 1)
        journals = glob.glob(emit.OUTBOX + ".journal.*")
        self.assertEqual(len(journals), 1)
        with open(journals[0], encoding="utf-8") as stream:
            durable = [json.loads(line) for line in stream if line.strip()]
        self.assertEqual(len(durable), 1)
        self.assertEqual(durable[0]["event"]["ts"], self.EVENT["ts"])

        with mock.patch.object(emit, "post_event", return_value=True):
            emit.deliver(dict(self.EVENT, ts="2026-08-24T12:00:01.000Z"))
        self.assertFalse(glob.glob(emit.OUTBOX + ".journal.*"))
        self.assertEqual(self.outbox(), [])

    def test_contended_journals_share_outbox_capacity_and_report_drops(self):
        lock_path = emit.OUTBOX + ".lock"
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with (
            open(lock_path, "a+") as held,
            mock.patch.object(emit, "OUTBOX_RECORDS", 2),
            mock.patch.object(emit, "OUTBOX_BYTES", 100000),
            mock.patch.object(emit, "post_event", return_value=False),
        ):
            emit.fcntl.flock(held, emit.fcntl.LOCK_EX | emit.fcntl.LOCK_NB)
            for second in range(5):
                emit.deliver(dict(self.EVENT, ts=f"2026-08-24T12:00:0{second}.000Z"))
        records = []
        for path in glob.glob(emit.OUTBOX + ".journal.*"):
            valid, _ = emit._read_outbox_journal(path)
            records.extend(valid or [])
        self.assertLessEqual(len(records), 2)
        with open(emit.DIAGNOSTICS, encoding="utf-8") as stream:
            report = json.load(stream)
        self.assertGreaterEqual(report["drops"], 3)

    def test_contended_delivery_attempts_older_journal_before_current_event(self):
        target = "http://primary"
        target_key = hashlib.sha256(target.encode()).hexdigest()[:16]
        older = dict(self.EVENT, ts="2026-08-24T11:59:59.000Z")
        emit._journal_outbox(
            [
                {
                    "target": target_key,
                    "delivery_id": "older",
                    "event": older,
                }
            ]
        )
        attempted = []
        lock_path = emit.OUTBOX + ".lock"
        with (
            open(lock_path, "a+") as held,
            mock.patch.dict(
                os.environ,
                {
                    "CHRONICLE_URL": target,
                    "CHRONICLE_MIRROR": "",
                },
            ),
            mock.patch.object(
                emit,
                "post_event",
                side_effect=lambda _url, event, *args: attempted.append(event["ts"])
                or False,
            ),
        ):
            emit.fcntl.flock(held, emit.fcntl.LOCK_EX | emit.fcntl.LOCK_NB)
            emit.deliver(self.EVENT)
        self.assertGreaterEqual(len(attempted), 1)
        self.assertEqual(attempted[0], older["ts"])

    def test_durable_snapshot_uses_enqueue_order_across_authorities(self):
        newer = {
            "target": "t",
            "delivery_id": "new",
            "enqueue_order": "0002",
            "event": dict(self.EVENT, ts="new"),
        }
        older = {
            "target": "t",
            "delivery_id": "old",
            "enqueue_order": "0001",
            "event": dict(self.EVENT, ts="old"),
        }
        with open(emit.OUTBOX, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(newer) + "\n")
        emit._journal_outbox([older])
        self.assertEqual(
            [r["delivery_id"] for r in emit._read_durable_outbox_snapshot()],
            ["old", "new"],
        )

    def test_fallback_keeps_order_allocated_before_failed_main_lock(self):
        target = "http://primary"
        target_key = hashlib.sha256(target.encode()).hexdigest()[:16]
        original_journal = emit._journal_outbox
        attempted = []

        def paused_fallback(records):
            newer = {
                "target": target_key,
                "delivery_id": "newer",
                "enqueue_order": "0002",
                "event": dict(self.EVENT, ts="newer"),
            }
            process = multiprocessing.Process(
                target=_commit_outbox_record, args=(emit.OUTBOX, newer)
            )
            process.start()
            process.join(10)
            self.assertEqual(process.exitcode, 0)
            return original_journal(records)

        with (
            mock.patch.dict(os.environ, {"CHRONICLE_URL": target, "CHRONICLE_MIRROR": ""}),
            mock.patch.object(emit, "_new_enqueue_order", return_value="0001"),
            mock.patch.object(emit, "_update_outbox", return_value=(0, False)),
            mock.patch.object(emit, "_journal_outbox", side_effect=paused_fallback),
            mock.patch.object(
                emit,
                "post_event",
                side_effect=lambda _url, event, *_: attempted.append(event["ts"])
                or False,
            ),
        ):
            emit.deliver(self.EVENT)
        self.assertEqual(attempted[0], self.EVENT["ts"])

    def test_diagnostics_counter_is_not_lost_between_processes(self):
        processes = [
            multiprocessing.Process(
                target=_increment_diagnostics, args=(emit.DIAGNOSTICS, 20)
            )
            for _ in range(4)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        with open(emit.DIAGNOSTICS, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["failures"], 80)

    def test_main_commit_and_aux_compaction_share_one_bounded_authority(self):
        delivered = {
            "target": "t",
            "delivery_id": "delivered",
            "enqueue_order": "0000",
            "event": self.EVENT,
        }
        with open(emit.OUTBOX, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(delivered) + "\n")
        additions = [
            {"target": "t", "delivery_id": "main", "event": dict(self.EVENT, ts="main")}
        ]
        auxiliary = [
            {
                "target": "t",
                "delivery_id": "aux-%d" % index,
                "event": dict(self.EVENT, ts="aux-%d" % index),
            }
            for index in range(4)
        ]
        gate = multiprocessing.Barrier(2)
        processes = [
            multiprocessing.Process(
                target=_race_outbox, args=(emit.OUTBOX, gate, "main", additions)
            ),
            multiprocessing.Process(
                target=_race_outbox, args=(emit.OUTBOX, gate, "aux", auxiliary)
            ),
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        snapshot = emit._read_durable_outbox_snapshot()
        self.assertLessEqual(len(snapshot), 3)
        self.assertNotIn(("t", "delivered"), {emit._record_key(r) for r in snapshot})

    def test_torn_journal_replays_valid_prefix_and_quarantines_tail(self):
        record = {
            "target": "target-key",
            "delivery_id": "delivery-id",
            "event": self.EVENT,
        }
        journal = emit.OUTBOX + ".journal.torn-write"
        os.makedirs(os.path.dirname(journal), exist_ok=True)
        torn = b'{"target":"unfinished'
        with open(journal, "wb") as stream:
            stream.write((json.dumps(record) + "\n").encode("utf-8"))
            stream.write(torn)
            stream.flush()
            os.fsync(stream.fileno())

        dropped, updated = emit._update_outbox(set(), [])

        self.assertTrue(updated)
        self.assertEqual(dropped, 0)
        self.assertEqual(self.outbox(), [record])
        self.assertFalse(os.path.exists(journal))
        quarantines = glob.glob(emit.OUTBOX + ".torn.*")
        self.assertEqual(len(quarantines), 1)
        with open(quarantines[0], "rb") as stream:
            self.assertEqual(stream.read(), torn)

    def _torn_journal(self, record):
        """A journal whose valid prefix is followed by a half-written record."""
        journal = emit.OUTBOX + ".journal.torn-write"
        os.makedirs(os.path.dirname(journal), exist_ok=True)
        torn = b'{"target":"unfinished'
        with open(journal, "wb") as stream:
            stream.write((json.dumps(record) + "\n").encode("utf-8"))
            stream.write(torn)
            stream.flush()
            os.fsync(stream.fileno())
        return journal, torn

    def test_journal_compaction_quarantines_torn_suffix_before_retiring_it(self):
        """The contended path keeps evidence before erasure, like the main one.

        Asserted as the literal operation sequence rather than the end state:
        the quarantine file, holding the torn bytes, must already exist at the
        instant the journal that carried them is unlinked (docs/spool.md, G3).
        """
        record = {
            "target": "target-key",
            "delivery_id": "delivery-id",
            "event": self.EVENT,
        }
        journal, torn = self._torn_journal(record)
        durable = emit.durable
        operations = []
        evidence_at_retire = []
        real_replace = durable.os.replace
        real_unlink = durable.os.unlink
        real_quarantine_tail = durable.Spool.quarantine_tail

        def replace(source, target):
            operations.append("replace")
            real_replace(source, target)

        def unlink(target):
            if target == journal:
                operations.append("retire")
                for path in sorted(glob.glob(emit.OUTBOX + ".torn.*")):
                    with open(path, "rb") as sample:
                        evidence_at_retire.append(sample.read())
            real_unlink(target)

        def quarantine_tail(spool, tail, source=None):
            operations.append("quarantine")
            return real_quarantine_tail(spool, tail, source)

        with (
            mock.patch.object(durable.os, "replace", side_effect=replace),
            mock.patch.object(durable.os, "unlink", side_effect=unlink),
            mock.patch.object(durable.Spool, "quarantine_tail", quarantine_tail),
            mock.patch.object(
                durable,
                "fsync_parent",
                side_effect=lambda _path: operations.append("dir-fsync"),
            ),
        ):
            self.assertEqual(emit._journal_outbox([]), 0)

        self.assertEqual(
            operations,
            ["replace", "dir-fsync", "quarantine", "dir-fsync", "retire", "dir-fsync"],
        )
        self.assertEqual(evidence_at_retire, [torn])
        self.assertFalse(os.path.exists(journal))
        self.assertEqual(
            [item["delivery_id"] for item in emit._read_durable_outbox_snapshot()],
            ["delivery-id"],
        )

    def test_crash_between_journal_quarantine_and_discard_keeps_the_evidence(self):
        """A crash in the window leaves the torn bytes and the record readable."""
        record = {
            "target": "target-key",
            "delivery_id": "delivery-id",
            "event": self.EVENT,
        }
        journal, torn = self._torn_journal(record)
        durable = emit.durable
        real_unlink = durable.os.unlink

        class Crash(Exception):
            """Not an OSError: _journal_outbox degrades on those, not on this."""

        def unlink(target):
            if target == journal:
                raise Crash("power lost between quarantine and discard")
            real_unlink(target)

        with mock.patch.object(durable.os, "unlink", side_effect=unlink):
            with self.assertRaises(Crash):
                emit._journal_outbox([])

        quarantines = glob.glob(emit.OUTBOX + ".torn.*")
        self.assertEqual(len(quarantines), 1)
        with open(quarantines[0], "rb") as stream:
            self.assertEqual(stream.read(), torn)
        # The interrupted retirement leaves the record in two durable homes,
        # which every reader dedupes (G2/G9).
        self.assertTrue(os.path.exists(journal))
        self.assertEqual(
            [item["delivery_id"] for item in emit._read_durable_outbox_snapshot()],
            ["delivery-id"],
        )

    def test_torn_tail_quarantine_reclaims_old_files_to_documented_caps(self):
        with (
            mock.patch.object(emit, "OUTBOX_TORN_FILES", 2),
            mock.patch.object(emit, "OUTBOX_TORN_BYTES", 9),
        ):
            for index in range(4):
                emit._quarantine_outbox_tail(bytes([index]) * 4)
        quarantines = sorted(glob.glob(emit.OUTBOX + ".torn.*"))
        self.assertLessEqual(len(quarantines), 2)
        self.assertLessEqual(sum(os.path.getsize(path) for path in quarantines), 9)
        with open(quarantines[-1], "rb") as stream:
            self.assertEqual(stream.read(), b"\x03" * 4)

    def test_targets_over_worker_cap_are_durably_deferred(self):
        urls = ",".join(f"http://primary-{index}" for index in range(10))
        with (
            mock.patch.dict(os.environ, {"CHRONICLE_URL": urls, "CHRONICLE_MIRROR": ""}),
            mock.patch.object(emit, "MAX_TARGETS", 2),
            mock.patch.object(emit, "post_event", return_value=False),
        ):
            emit.deliver(self.EVENT)
        self.assertEqual(len(self.outbox()), 10)

    def test_primary_scheduling_is_fair_across_hooks(self):
        urls = [f"http://primary-{index}" for index in range(6)]
        attempted = []
        with (
            mock.patch.dict(
                os.environ,
                {
                    "CHRONICLE_URL": ",".join(urls),
                    "CHRONICLE_MIRROR": "",
                },
            ),
            mock.patch.object(emit, "MAX_TARGETS", 2),
            mock.patch.object(
                emit,
                "post_event",
                side_effect=lambda url, *args: attempted.append(url) or False,
            ),
        ):
            for second in range(3):
                emit.deliver(dict(self.EVENT, ts=f"2026-08-24T12:00:0{second}.000Z"))
        self.assertEqual(set(attempted), set(urls))

    def test_primary_fairness_survives_mixed_success_and_restart(self):
        urls = [f"http://primary-{index}" for index in range(6)]
        attempted = []

        def mixed(url, *args):
            attempted.append(url)
            return url.endswith(("0", "2", "4"))

        with (
            mock.patch.dict(
                os.environ,
                {
                    "CHRONICLE_URL": ",".join(urls),
                    "CHRONICLE_MIRROR": "",
                },
            ),
            mock.patch.object(emit, "MAX_TARGETS", 2),
            mock.patch.object(emit, "post_event", side_effect=mixed),
        ):
            for second in range(3):
                emit.deliver(dict(self.EVENT, ts=f"2026-08-24T12:00:0{second}.000Z"))
        self.assertEqual(set(attempted), set(urls))

    def test_schedule_prunes_target_churn_and_preserves_fairness(self):
        live = ["http://live-0", "http://live-1"]
        stale = {"stale-%04d" % index: index for index in range(200)}
        with open(emit._schedule_path(), "w", encoding="utf-8") as stream:
            json.dump(stale, stream)
        attempted = []
        with (
            mock.patch.dict(
                os.environ, {"CHRONICLE_URL": ",".join(live), "CHRONICLE_MIRROR": ""}
            ),
            mock.patch.object(emit, "MAX_TARGETS", 1),
            mock.patch.object(
                emit,
                "post_event",
                side_effect=lambda url, *_: attempted.append(url) or False,
            ),
        ):
            emit.deliver(self.EVENT)
            emit.deliver(dict(self.EVENT, ts="later"))
        with open(emit._schedule_path(), encoding="utf-8") as stream:
            schedule = json.load(stream)
        self.assertLessEqual(len(schedule), 2)
        self.assertEqual(set(attempted), set(live))

    def test_oversized_schedule_is_rejected(self):
        with mock.patch.object(emit, "SCHEDULE_BYTES", 8):
            with open(emit._schedule_path(), "w", encoding="utf-8") as stream:
                stream.write('{"oversized":1}')
            self.assertEqual(emit._read_schedule(), {})

    def test_cli_budget_bounds_slow_durable_io(self):
        original_stdin = sys.stdin
        with (
            mock.patch.object(emit, "HOOK_BUDGET", 0.08),
            mock.patch.object(emit.os, "fsync", side_effect=lambda _fd: time.sleep(2)),
        ):
            sys.stdin = io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "slow",
                        "cwd": "/private",
                    }
                )
            )
            try:
                started = time.monotonic()
                emit.run_hook_bounded("claude")
            finally:
                sys.stdin = original_stdin
        self.assertLess(time.monotonic() - started, 0.35)

    def test_real_hook_boundary_acks_before_its_outer_work_deadline(self):
        with (
            mock.patch.dict(
                os.environ,
                {"CHRONICLE_URL": "http://delayed", "CHRONICLE_MIRROR": ""},
            ),
            mock.patch.object(emit, "post_event", return_value=False),
        ):
            emit.deliver(self.EVENT)
        queued_id = self.outbox()[0]["delivery_id"]
        original_stdin = sys.stdin

        def delayed_post(*_args, **_kwargs):
            time.sleep(0.08)
            return True

        with (
            mock.patch.dict(
                os.environ,
                {"CHRONICLE_URL": "http://delayed", "CHRONICLE_MIRROR": ""},
            ),
            mock.patch.object(emit, "HOOK_BUDGET", 0.5),
            mock.patch.object(emit, "HOOK_REAP_BUDGET", 0.05),
            mock.patch.object(emit, "ACK_RESERVE", 0.1),
            mock.patch.object(emit, "POST_TIMEOUT", 0.1),
            mock.patch.object(emit, "post_event", new=delayed_post),
        ):
            sys.stdin = io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "Stop",
                        "session_id": "outer-budget",
                        "cwd": "/private",
                    }
                )
            )
            try:
                emit.run_hook_bounded("claude")
            finally:
                sys.stdin = original_stdin

        self.assertNotIn(
            queued_id, [record["delivery_id"] for record in self.outbox()]
        )

    def test_timeout_polls_reap_through_same_deadline_without_blocking(self):
        waits = []
        stderr = io.StringIO()
        clock = iter((0, 0, 0.94, 0.95, 0.96, 0.97, 0.98, 1.0))
        wait_results = iter(((0, 0), (0, 0), (123, 9)))
        with (
            mock.patch.object(emit, "HOOK_BUDGET", 1),
            mock.patch.object(emit, "HOOK_REAP_BUDGET", 0.05),
            mock.patch.object(emit.time, "monotonic", side_effect=lambda: next(clock)),
            mock.patch.object(emit.time, "sleep"),
            mock.patch.object(emit.os, "fork", return_value=123),
            mock.patch.object(emit.os, "kill"),
            mock.patch.object(
                emit.os,
                "waitpid",
                side_effect=lambda pid, options: waits.append(options)
                or next(wait_results),
            ),
            mock.patch.object(emit.sys, "stderr", stderr),
        ):
            emit.run_hook_bounded("claude")
        self.assertEqual(waits, [emit.os.WNOHANG] * 3)
        self.assertEqual(stderr.getvalue(), "chronicle transport timeout\n")


class LocalOnlyDeliveryTest(unittest.TestCase):
    def test_multiprocess_deferred_commits_share_one_bounded_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            log = os.path.join(directory, "events.jsonl")
            diagnostics = os.path.join(directory, "diag.json")
            events = [
                dict(
                    DurablePrimaryDeliveryTest.EVENT,
                    ts="2026-08-24T12:00:%02d.000Z" % index,
                )
                for index in range(12)
            ]
            gate = multiprocessing.Barrier(3)
            processes = [
                multiprocessing.Process(
                    target=_defer_events,
                    args=(log, diagnostics, events[index::3], gate),
                )
                for index in range(3)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            with open(log + ".deferred", encoding="utf-8") as stream:
                retained = [json.loads(line) for line in stream if line.strip()]
            self.assertEqual(len(retained), 4)
            self.assertEqual(
                len({item[emit._DEFERRED_ID_FIELD] for item in retained}), 4
            )
            with open(diagnostics, encoding="utf-8") as stream:
                self.assertEqual(json.load(stream)["drops"], 8)

    def test_deferred_authority_is_bounded_and_oldest_drops_are_diagnosed(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(emit, "LOG_DIR", directory),
            mock.patch.object(emit, "LOG", os.path.join(directory, "events.jsonl")),
            mock.patch.object(
                emit, "DIAGNOSTICS", os.path.join(directory, "diag.json")
            ),
            mock.patch.object(emit, "DEFERRED_RECORDS", 2),
            mock.patch.object(emit, "DEFERRED_BYTES", 100000),
        ):
            events = [
                dict(
                    DurablePrimaryDeliveryTest.EVENT,
                    ts="2026-08-24T12:00:0%d.000Z" % index,
                )
                for index in range(5)
            ]
            for event in events:
                emit._defer_local(event)
            authority = [emit.LOG + ".deferred"] + glob.glob(
                emit.LOG + ".deferred.replay.*"
            )
            records = []
            for candidate in authority:
                with open(candidate, encoding="utf-8") as stream:
                    records.extend(json.loads(line) for line in stream if line.strip())
            self.assertEqual(
                [record["ts"] for record in records],
                [events[-2]["ts"], events[-1]["ts"]],
            )
            with open(emit.DIAGNOSTICS, encoding="utf-8") as stream:
                report = json.load(stream)
            self.assertEqual(report["drops"], 3)
            self.assertEqual(report["recent"][-1]["reason"], "local deferred capacity")

            emit._append_local(dict(events[-1], ts="2026-08-24T12:00:09.000Z"))
            with open(emit.LOG, encoding="utf-8") as stream:
                timestamps = [json.loads(line)["ts"] for line in stream if line.strip()]
            self.assertEqual(
                timestamps,
                [events[-2]["ts"], events[-1]["ts"], "2026-08-24T12:00:09.000Z"],
            )

    def test_deferred_authority_enforces_encoded_byte_cap(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(emit, "LOG", os.path.join(directory, "events.jsonl")),
            mock.patch.object(
                emit, "DIAGNOSTICS", os.path.join(directory, "diag.json")
            ),
            mock.patch.object(emit, "DEFERRED_RECORDS", 100),
            mock.patch.object(emit, "DEFERRED_BYTES", 100),
        ):
            event = dict(
                DurablePrimaryDeliveryTest.EVENT, payload={"message": "x" * 500}
            )
            emit._defer_local(event)
            self.assertEqual(os.path.getsize(emit.LOG + ".deferred"), 0)
            with open(emit.DIAGNOSTICS, encoding="utf-8") as stream:
                self.assertEqual(json.load(stream)["drops"], 1)

    def test_deferred_victim_is_retained_when_drop_diagnostic_cannot_commit(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(emit, "LOG", os.path.join(directory, "events.jsonl")),
            mock.patch.object(emit, "DEFERRED_RECORDS", 1),
            mock.patch.object(emit, "DEFERRED_BYTES", 100000),
        ):
            first = DurablePrimaryDeliveryTest.EVENT
            second = dict(first, ts="2026-08-24T12:00:01.000Z")
            emit._defer_local(first)
            with mock.patch.object(emit, "_diagnose", return_value=False):
                with self.assertRaises(OSError):
                    emit._defer_local(second)
            with open(emit.LOG + ".deferred", encoding="utf-8") as stream:
                retained = [json.loads(line)["ts"] for line in stream if line.strip()]
            self.assertEqual(retained, [first["ts"]])

    def test_local_only_is_a_healthy_local_append(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                os.environ,
                {
                    "CHRONICLE_URL": "",
                    "CHRONICLE_MIRROR": "",
                },
            ),
            mock.patch.object(emit, "LOG_DIR", directory),
            mock.patch.object(emit, "LOG", os.path.join(directory, "events.jsonl")),
            mock.patch.object(
                emit, "DIAGNOSTICS", os.path.join(directory, "diag.json")
            ),
        ):
            emit.deliver(DurablePrimaryDeliveryTest.EVENT)
            self.assertFalse(os.path.exists(emit.DIAGNOSTICS))
            with open(emit.LOG, encoding="utf-8") as stream:
                self.assertEqual(sum(1 for line in stream if line.strip()), 1)

    def test_contended_local_log_is_durably_deferred_then_recovered(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                os.environ,
                {
                    "CHRONICLE_URL": "",
                    "CHRONICLE_MIRROR": "",
                },
            ),
            mock.patch.object(emit, "LOG_DIR", directory),
            mock.patch.object(emit, "LOG", os.path.join(directory, "events.jsonl")),
            mock.patch.object(
                emit, "DIAGNOSTICS", os.path.join(directory, "diag.json")
            ),
        ):
            with open(emit.LOG, "a+") as held:
                emit.fcntl.flock(held, emit.fcntl.LOCK_EX | emit.fcntl.LOCK_NB)
                started = time.monotonic()
                emit.deliver(DurablePrimaryDeliveryTest.EVENT)
                self.assertLess(time.monotonic() - started, 0.2)
            self.assertTrue(os.path.getsize(emit.LOG + ".deferred") > 0)
            emit.deliver(
                dict(DurablePrimaryDeliveryTest.EVENT, ts="2026-08-24T12:00:01.000Z")
            )
            with open(emit.LOG, encoding="utf-8") as stream:
                self.assertEqual(sum(1 for line in stream if line.strip()), 2)

    def test_concurrent_append_after_handoff_stays_for_the_next_replay(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(emit, "LOG_DIR", directory),
            mock.patch.object(emit, "LOG", os.path.join(directory, "events.jsonl")),
        ):
            first = DurablePrimaryDeliveryTest.EVENT
            second = dict(first, ts="2026-08-24T12:00:01.000Z")
            current = dict(first, ts="2026-08-24T12:00:02.000Z")
            following = dict(first, ts="2026-08-24T12:00:03.000Z")
            emit._defer_local(first)
            real_replace = os.replace
            appenders = []

            def append_after_handoff(source, destination):
                real_replace(source, destination)
                worker = threading.Thread(target=emit._defer_local, args=(second,))
                worker.start()
                appenders.append(worker)

            with mock.patch.object(
                emit.os, "replace", side_effect=append_after_handoff
            ):
                emit._append_local(current)
            appenders[0].join(1)
            self.assertFalse(appenders[0].is_alive())
            emit._append_local(following)
            with open(emit.LOG, encoding="utf-8") as stream:
                timestamps = [json.loads(line)["ts"] for line in stream if line.strip()]
        self.assertEqual(
            timestamps, [first["ts"], current["ts"], second["ts"], following["ts"]]
        )

    def test_crash_after_replay_fsync_is_idempotent(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(emit, "LOG_DIR", directory),
            mock.patch.object(emit, "LOG", os.path.join(directory, "events.jsonl")),
        ):
            deferred = DurablePrimaryDeliveryTest.EVENT
            emit._defer_local(deferred)
            current = dict(deferred, ts="2026-08-24T12:00:01.000Z")
            following = dict(deferred, ts="2026-08-24T12:00:02.000Z")
            with mock.patch.object(emit.os, "unlink", side_effect=OSError("crash")):
                emit._append_local(current)
            emit._append_local(following)
            with open(emit.LOG, encoding="utf-8") as stream:
                timestamps = [json.loads(line)["ts"] for line in stream if line.strip()]
        self.assertEqual(timestamps.count(deferred["ts"]), 1)

    def test_torn_deferred_tail_is_quarantined_after_valid_prefix_replays(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(emit, "LOG_DIR", directory),
            mock.patch.object(emit, "LOG", os.path.join(directory, "events.jsonl")),
        ):
            path = emit.LOG + ".deferred.replay.crashed"
            with open(path, "wb") as stream:
                stream.write(
                    (json.dumps(DurablePrimaryDeliveryTest.EVENT) + "\n").encode()
                )
                stream.write(b'{"v":0,"payload":')
            emit._append_local(
                dict(DurablePrimaryDeliveryTest.EVENT, ts="2026-08-24T12:00:01.000Z")
            )
            with open(emit.LOG, encoding="utf-8") as stream:
                events = [json.loads(line) for line in stream if line.strip()]
            self.assertEqual(len(events), 2)
            self.assertFalse(os.path.exists(path))
            quarantined = glob.glob(path + ".torn.*")
            self.assertEqual(len(quarantined), 1)
            with open(quarantined[0], "rb") as stream:
                self.assertEqual(stream.read(), b'{"v":0,"payload":')

    def test_repeated_torn_deferred_generations_have_bounded_quarantine(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(emit, "LOG", os.path.join(directory, "events.jsonl")),
            mock.patch.object(emit, "DEFERRED_TORN_FILES", 2),
            mock.patch.object(emit, "DEFERRED_TORN_BYTES", 9),
        ):
            path = emit.LOG + ".deferred"
            for index in range(5):
                generation = path + ".replay.%d" % index
                with open(generation, "wb") as stream:
                    stream.write(bytes([index]) * 4)
                emit._compact_deferred_locked(path)
            quarantines = sorted(glob.glob(path + ".replay.*.torn.*"))
            self.assertLessEqual(len(quarantines), 2)
            self.assertLessEqual(sum(os.path.getsize(item) for item in quarantines), 9)

    def test_repeated_torn_active_deferred_files_share_quarantine_cap(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(emit, "LOG", os.path.join(directory, "events.jsonl")),
            mock.patch.object(emit, "DEFERRED_TORN_FILES", 2),
            mock.patch.object(emit, "DEFERRED_TORN_BYTES", 9),
        ):
            path = emit.LOG + ".deferred"
            for index in range(5):
                with open(path, "wb") as stream:
                    stream.write(bytes([index]) * 4)
                emit._compact_deferred_locked(path)
            quarantines = sorted(glob.glob(path + ".torn.*"))
            self.assertLessEqual(len(quarantines), 2)
            self.assertLessEqual(sum(os.path.getsize(item) for item in quarantines), 9)

    def test_deferred_disjoint_active_replay_pending_has_finite_physical_ceiling(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(emit, "LOG", os.path.join(directory, "events.jsonl")),
            mock.patch.object(emit, "DEFERRED_RECORDS", 2),
            mock.patch.object(emit, "DEFERRED_BYTES", 100000),
        ):
            path = emit.LOG + ".deferred"
            for suffix, seconds in (("", (0, 1)), (".replay.old", (2, 3))):
                with open(path + suffix, "w", encoding="utf-8") as stream:
                    for second in seconds:
                        record = dict(
                            DurablePrimaryDeliveryTest.EVENT,
                            ts="2026-08-24T12:00:0%d.000Z" % second,
                            _burrow_deferred_id="id-%d" % second,
                        )
                        stream.write(json.dumps(record) + "\n")
            observed = []
            real_replace = os.replace

            def line_count(candidate):
                with open(candidate, encoding="utf-8") as stream:
                    return sum(1 for _ in stream)

            def inspect_pending(source, destination):
                candidates = [path, path + ".pending", path + ".replay.old"]
                existing = [item for item in candidates if os.path.exists(item)]
                observed.append(
                    (
                        sum(os.path.getsize(item) for item in existing),
                        sum(line_count(item) for item in existing),
                    )
                )
                real_replace(source, destination)

            with mock.patch.object(emit.os, "replace", side_effect=inspect_pending):
                emit._compact_deferred_locked(path)
            self.assertEqual(
                max(count for _, count in observed), 3 * emit.DEFERRED_RECORDS
            )
            self.assertLessEqual(
                max(size for size, _ in observed), 3 * emit.DEFERRED_BYTES
            )


if __name__ == "__main__":
    unittest.main()
