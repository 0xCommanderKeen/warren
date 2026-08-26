"""The canonical README deployment recipe must contain a runnable server tree."""

import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import unittest


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
            completed = subprocess.run(
                [os.environ.get("PYTHON", "python3"), "-c",
                 "import serve; server=serve.BurrowHTTPServer(('127.0.0.1', 0), "
                 "serve.Handler); server.server_close()"],
                cwd=staged, env=env, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
