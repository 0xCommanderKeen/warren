"""The canonical README deployment recipe must contain a runnable server tree."""

import json
import os
import pathlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from tests.runner import without_chronicle_settings


ROOT = pathlib.Path(__file__).resolve().parents[1]


class DeploymentBundleTest(unittest.TestCase):
    def test_documented_tar_inputs_start_server_outside_repository(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(r"`tar -cf -\s+(.*?)\s+\|\s+ssh\b", readme, re.DOTALL)
        self.assertIsNotNone(match, "canonical tar-over-SSH recipe is documented")
        inputs = shlex.split(re.sub(r"\s+", " ", match.group(1)))

        with tempfile.TemporaryDirectory() as temporary:
            temporary = pathlib.Path(temporary)
            archive = temporary / "deployment.tar"
            staged = temporary / "app"
            staged.mkdir()
            subprocess.run(["tar", "-cf", archive, *inputs], cwd=ROOT, check=True)
            subprocess.run(["tar", "-xf", archive, "-C", staged], check=True)
            self.assertTrue(
                (staged / "villagers").is_dir(),
                "an empty resident fleet still ships its manifest directory",
            )

            # Burrow ships no browser client. Nothing a browser would load may
            # ride along to the NAS, or the deployed origin keeps answering with
            # a page this repository no longer builds or tests.
            shipped = [
                str(path.relative_to(staged))
                for path in staged.rglob("*")
                if path.is_file()
            ]
            self.assertNotIn("viewer", {name.split("/")[0] for name in shipped})
            self.assertEqual(
                [name for name in shipped if name.endswith((".js", ".html", ".css"))],
                [],
            )

            env = without_chronicle_settings(os.environ)
            env.pop("PYTHONPATH", None)
            uv = shutil.which("uv")
            self.assertIsNotNone(uv, "deployment requires uv")
            installed = subprocess.run(
                [uv, "sync", "--frozen", "--no-dev", "--python", sys.executable],
                cwd=staged,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)

            # Both documented entry points must read the environment they are
            # given: `python serve.py` is what the compose file runs, and
            # `uvicorn serve:app` is the README's local command — the NAS ran the
            # latter for six days with every setting silently ignored
            # (warren#313). The token must gate ingest and the log must land at
            # CHRONICLE_EVENTS, whichever way the tree was started.
            for name, command in (
                ("python serve.py", lambda port: ["python", "serve.py", str(port)]),
                (
                    "uvicorn serve:app",
                    lambda port: [
                        "uvicorn",
                        "serve:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--log-level",
                        "error",
                    ],
                ),
            ):
                with self.subTest(entry_point=name):
                    events = temporary / name.replace(" ", "-") / "events.jsonl"
                    self._assert_staged_server_reads_environment(
                        uv, staged, command, {**env, "CHRONICLE_EVENTS": str(events)}
                    )
                    self.assertTrue(events.is_file(), "log lands at CHRONICLE_EVENTS")
                    self.assertIn(
                        "test:staged-bundle",
                        [
                            json.loads(line)["agent_id"]
                            for line in events.read_text(encoding="utf-8").splitlines()
                        ],
                    )

    def _assert_staged_server_reads_environment(self, uv, staged, command, env):
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        server = subprocess.Popen(
            [uv, "run", *command(port)],
            cwd=staged,
            env={**env, "CHRONICLE_TOKEN": "staged-secret"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while True:
                if server.poll() is not None:
                    _, stderr = server.communicate()
                    self.fail(f"staged ASGI server exited early: {stderr}")
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/villagers", timeout=0.2
                    ) as response:
                        self.assertEqual(response.status, 200)
                        break
                except urllib.error.HTTPError:
                    self.fail("staged ASGI server does not serve /villagers")
                except OSError:
                    if time.monotonic() >= deadline:
                        self.fail("staged ASGI server did not become ready")
                    time.sleep(0.05)

            # The root path used to hand back the viewer; it must now 404.
            with self.assertRaises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
            self.assertEqual(refused.exception.code, 404)

            event = json.dumps(
                {
                    "v": 0,
                    "ts": "2026-09-02T12:00:00.000Z",
                    "source": "test",
                    "agent_id": "test:staged-bundle",
                    "project": "chronicle",
                    "cwd": "",
                    "type": "heartbeat",
                    "payload": {},
                }
            ).encode()

            def ingest_request(headers):
                return urllib.request.Request(
                    f"http://127.0.0.1:{port}/events",
                    data=event,
                    headers={"Content-Type": "application/json", **headers},
                    method="POST",
                )

            with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                urllib.request.urlopen(ingest_request({}), timeout=2)
            self.assertEqual(unauthorized.exception.code, 401)
            with urllib.request.urlopen(
                ingest_request({"Authorization": "Bearer staged-secret"}), timeout=2
            ) as accepted:
                self.assertEqual(accepted.status, 204)
        finally:
            if server.poll() is None:
                server.terminate()
            server.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
