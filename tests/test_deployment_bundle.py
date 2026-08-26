"""The canonical README deployment recipe must contain a runnable server tree."""

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
import urllib.request


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

            env = os.environ.copy()
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

            with socket.socket() as available:
                available.bind(("127.0.0.1", 0))
                port = available.getsockname()[1]
            server = subprocess.Popen(
                [
                    uv,
                    "run",
                    "uvicorn",
                    "serve:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "error",
                ],
                cwd=staged,
                env=env,
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
                            f"http://127.0.0.1:{port}/", timeout=0.2
                        ) as response:
                            self.assertEqual(response.status, 200)
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail("staged ASGI server did not become ready")
                        time.sleep(0.05)
            finally:
                if server.poll() is None:
                    server.terminate()
                server.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
