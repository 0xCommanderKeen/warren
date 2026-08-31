"""The emitter bundle: one stdlib-only file, built from hooks/, that still runs.

``hooks/build.py`` flattens ``hooks/emit.py`` and ``hooks/durable.py`` into a single
self-contained script. Anything that deploys the emitter as one file — steward's
resident image vendors it into ``docker/resident/burrow-emit.py`` — runs *this*
artifact, so the constraints that make a one-file emitter possible are asserted here,
in the suite of the repository that can break them.
"""

import ast
import contextlib
import hashlib
import http.server
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = ROOT / "hooks" / "build.py"
HOOKS = ROOT / "hooks"

#: Every transport setting, under both the new and the pre-rename spelling. The machine
#: running this suite emits to a real village of its own; a test that cleared only one
#: spelling would have the other leak straight back in through the emitter's fallback.
BOTH_SPELLINGS = tuple(
    prefix + name
    for name in ("URL", "TOKEN", "MIRROR", "MIRROR_TOKEN", "AGENT_ID", "PROJECT", "DETAIL")
    for prefix in ("CHRONICLE_", "BURROW_")
)


def build_to(destination):
    """Run the build the way a human or a Makefile does, and return the artifact."""
    subprocess.run(
        [sys.executable, str(BUILD), "--output", str(destination)],
        capture_output=True,
        text=True,
        check=True,
    )
    return pathlib.Path(destination).read_text(encoding="utf-8")


class BundleShapeTest(unittest.TestCase):
    def test_the_bundle_is_one_file_that_compiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = pathlib.Path(temporary) / "emit-bundle.py"
            text = build_to(artifact)

            self.assertEqual([artifact.name], [p.name for p in artifact.parent.iterdir()])
            compile(text, str(artifact), "exec")

    def test_the_bundle_carries_provenance_and_forbids_hand_edits(self):
        """A copy of a generated file has to say where to make the change instead."""
        with tempfile.TemporaryDirectory() as temporary:
            text = build_to(pathlib.Path(temporary) / "emit-bundle.py")

            self.assertTrue(text.startswith("#!/usr/bin/env python3\n"))
            header = text.split("\nimport ", 1)[0]
            self.assertIn("DO NOT EDIT", header)
            self.assertIn("hooks/build.py", header)
            for name in ("emit.py", "durable.py"):
                digest = hashlib.sha256((HOOKS / name).read_bytes()).hexdigest()
                self.assertIn(digest, header, "%s's digest is not in the header" % name)

    def test_the_bundle_carries_durables_source_verbatim(self):
        """Not a rewrite of durable.py: the same bytes, materialized as a module."""
        with tempfile.TemporaryDirectory() as temporary:
            text = build_to(pathlib.Path(temporary) / "emit-bundle.py")

            embedded = embedded_durable(text)
            self.assertEqual(embedded, (HOOKS / "durable.py").read_text(encoding="utf-8"))

    def test_the_bundle_imports_no_sibling_module(self):
        """A file that still imports ``durable`` is not self-contained, whatever else it is."""
        with tempfile.TemporaryDirectory() as temporary:
            text = build_to(pathlib.Path(temporary) / "emit-bundle.py")

            imported = {name for name, _ in imports(ast.parse(text))}
            self.assertNotIn("durable", imported)
            self.assertNotIn("hooks", imported)


class BundleBehaviourTest(unittest.TestCase):
    """What the artifact does when a runner fires it, which is the only thing that matters.

    emit.py's charter is to never break the hosting agent, so it swallows everything and
    exits 0 — including, silently, a bundle whose embedded module failed to materialize.
    A fail-open assertion alone would therefore pass for a completely broken artifact.
    These tests assert the positive: the event comes out the other end.
    """

    def run_bundle(self, artifact, payload, home, **environment):
        return subprocess.run(
            [sys.executable, str(artifact)],
            input=payload,
            text=True,
            cwd=home,
            env={
                # Both spellings of every transport setting are stripped first: this
                # machine is itself a villager, and whatever its shell exports would
                # otherwise decide where the test's events go.
                **{k: v for k, v in os.environ.items() if k not in BOTH_SPELLINGS},
                "HOME": home,
                "CHRONICLE_MIRROR": "",
                "CHRONICLE_URL": "http://127.0.0.1:1",
                **environment,
            },
            capture_output=True,
            timeout=15,
        )

    def test_an_unreachable_village_leaves_the_event_in_the_durable_outbox(self):
        """The outbox is written by durable.Spool before any network work: the embed, proved.

        Asserted on the outbox rather than on the local fallback log, because the fallback
        append is gated on the emitter's one-second hook budget — a loaded machine can
        legitimately spend the whole budget and leave the event queued for replay and
        nowhere else, which is not a failure and must not read as one.
        """
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as home:
            artifact = pathlib.Path(temporary) / "emit-bundle.py"
            build_to(artifact)
            event = {
                "session_id": "queued",
                "cwd": home,
                "hook_event_name": "UserPromptSubmit",
                "prompt": "work",
            }

            result = self.run_bundle(artifact, json.dumps(event), home)

            self.assertEqual(result.returncode, 0, result.stderr)
            outbox = pathlib.Path(home) / ".chronicle" / "primary-outbox.jsonl"
            queued = [json.loads(line) for line in outbox.read_text().splitlines()]
            self.assertEqual(
                [record["event"]["agent_id"] for record in queued],
                ["claude-code:queued"],
            )

    def test_a_village_that_was_never_configured_falls_back_to_the_local_log(self):
        """No target at all is the one path that appends locally under no deadline."""
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as home:
            artifact = pathlib.Path(temporary) / "emit-bundle.py"
            build_to(artifact)
            event = {
                "session_id": "offline",
                "cwd": home,
                "hook_event_name": "UserPromptSubmit",
                "prompt": "work",
            }

            result = self.run_bundle(artifact, json.dumps(event), home, CHRONICLE_URL="")

            self.assertEqual(result.returncode, 0, result.stderr)
            log = pathlib.Path(home) / ".chronicle" / "events.jsonl"
            records = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(records[-1]["agent_id"], "claude-code:offline")

    def test_the_bundle_delivers_to_a_village_that_answers(self):
        """The delivered path takes the durable outbox, not just the local log."""
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as home,
            village() as (url, received),
        ):
            artifact = pathlib.Path(temporary) / "emit-bundle.py"
            build_to(artifact)
            event = {
                "session_id": "delivered",
                "cwd": home,
                "hook_event_name": "UserPromptSubmit",
                "prompt": "work",
            }

            result = self.run_bundle(artifact, json.dumps(event), home, CHRONICLE_URL=url)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                [json.loads(body)["agent_id"] for body in received],
                ["claude-code:delivered"],
            )
            self.assertFalse((pathlib.Path(home) / ".chronicle" / "events.jsonl").exists())

    def test_the_bundle_is_fail_open_on_anything_at_all(self):
        """A hook that exits non-zero takes the session with it. Nothing may do that."""
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as home:
            artifact = pathlib.Path(temporary) / "emit-bundle.py"
            build_to(artifact)

            for payload in ("", "not json at all", "[]", '{"hook_event_name": 7}', "\0\0\0"):
                with self.subTest(payload=payload):
                    result = self.run_bundle(artifact, payload, home)

                    self.assertEqual(result.returncode, 0, result.stderr)


class BundleContractTest(unittest.TestCase):
    def test_the_bundle_needs_only_the_standard_library(self):
        """No pip anywhere it runs: a bare python3 in a container, on a laptop, on the NAS.

        Checked over the embedded source too — what the artifact *executes* is both files,
        and a third-party import inside durable.py would be exactly as fatal.
        """
        with tempfile.TemporaryDirectory() as temporary:
            text = build_to(pathlib.Path(temporary) / "emit-bundle.py")

            found = imports(ast.parse(text)) | imports(ast.parse(embedded_durable(text)))
            outside = {full for top, full in found if top not in sys.stdlib_module_names}
            self.assertEqual(outside, set())

    def test_the_build_is_deterministic(self):
        """Byte-identical output is what lets a vendored copy be compared to a rebuild."""
        with tempfile.TemporaryDirectory() as temporary:
            first = pathlib.Path(temporary) / "first.py"
            second = pathlib.Path(temporary) / "second.py"

            self.assertEqual(build_to(first), build_to(second))

    def test_the_build_refuses_a_source_it_cannot_flatten(self):
        """A silent no-op here would ship a one-file artifact that still imports a sibling."""
        source = build_module()
        emit = (HOOKS / "emit.py").read_text(encoding="utf-8")
        durable = (HOOKS / "durable.py").read_text(encoding="utf-8")

        with self.assertRaises(ValueError):
            source.bundle(emit.replace(source.IMPORT_ANCHOR, "import durable\n"), durable)
        with self.assertRaises(ValueError):
            source.bundle(emit, durable + "TRIPLE = %s\n" % ("'" * 3))
        with self.assertRaises(ValueError):
            source.bundle(emit, durable.rstrip("\n"))
        with self.assertRaises(ValueError):
            source.bundle(emit.removeprefix("#!/usr/bin/env python3\n"), durable)

    def test_the_copy_steward_vendors_is_this_bundle(self):
        """Both suites scream, whichever service a PR happened to touch.

        steward's own suite asserts this too (tests/test_resident_image.py) and its CI is
        path-filtered to include chronicle/hooks/** so an emitter change turns it red. The
        same assertion here means a chronicle developer sees the drift in chronicle's
        suite, where the change was made, rather than in a sibling service's CI. Skipped
        when there is no steward beside us, so chronicle stays runnable on its own.
        """
        vendored = ROOT.parent / "steward" / "docker" / "resident" / "burrow-emit.py"
        if not vendored.is_file():
            self.skipTest("no steward checkout beside this one")

        with tempfile.TemporaryDirectory() as temporary:
            # Digests, not two 70KB strings: the answer is yes or no, and a failure should
            # say what to run rather than print a diff of the whole emitter.
            built = build_to(pathlib.Path(temporary) / "emit-bundle.py")
            self.assertEqual(
                hashlib.sha256(vendored.read_bytes()).hexdigest(),
                hashlib.sha256(built.encode("utf-8")).hexdigest(),
                "the resident image's vendored emitter is not what hooks/ builds today; "
                "run `make vendor-emitter` in warren/steward/ and commit the result",
            )

    def test_the_build_leaves_no_artifact_behind_when_it_fails(self):
        """`make vendor-emitter` redirects into a committed path; a torn write would land there."""
        with tempfile.TemporaryDirectory() as temporary:
            hooks = pathlib.Path(temporary) / "hooks"
            hooks.mkdir()
            (hooks / "emit.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            (hooks / "durable.py").write_text("x = 1\n", encoding="utf-8")
            output = pathlib.Path(temporary) / "bundle.py"

            result = subprocess.run(
                [sys.executable, str(BUILD), "--hooks", str(hooks), "--output", str(output)],
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
            self.assertEqual([], list(pathlib.Path(temporary).glob(".bundle.*")))


def build_module():
    """hooks/build.py as an importable module, for the pure function underneath the CLI."""
    spec = importlib.util.spec_from_file_location("chronicle_build", BUILD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def village():
    """A village that answers 204 on POST /events and records what it was given."""
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            received.append(body.decode("utf-8"))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def embedded_durable(text):
    """The source the bundle carries for durable.py, read statically off the artifact."""
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "_DURABLE_SOURCE" in targets:
                return node.value.value
    raise AssertionError("the bundle carries no _DURABLE_SOURCE")


def imports(tree):
    """Every module a parsed source imports, as (top-level name, full name)."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add((alias.name.split(".")[0], alias.name))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add((node.module.split(".")[0], node.module))
    return found


if __name__ == "__main__":
    unittest.main()
