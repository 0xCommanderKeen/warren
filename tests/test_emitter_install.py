import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class InstalledEmitterTest(unittest.TestCase):
    def install(self, root):
        bundle = pathlib.Path(root) / ".local" / "lib" / "burrow-emitter"
        bundle.mkdir(parents=True, mode=0o700)
        for name in ("emit.py", "durable.py", "burrow-emit"):
            shutil.copy2(ROOT / "hooks" / name, bundle / name)
        (bundle / "burrow-emit").chmod(0o700)
        return bundle

    def test_documented_bundle_runs_outside_repo_and_failed_transport_is_fail_open(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            bundle = self.install(home)
            event = {"session_id": "installed-layout", "cwd": cwd,
                     "hook_event_name": "UserPromptSubmit", "prompt": "work"}
            env = dict(os.environ, HOME=home, BURROW_MIRROR="",
                       BURROW_URL="http://127.0.0.1:1")
            result = subprocess.run(
                [str(bundle / "burrow-emit"), "--runner", "codex"],
                input=json.dumps(event), text=True, cwd=cwd, env=env,
                capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            log = pathlib.Path(home) / ".burrow" / "events.jsonl"
            records = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(records[-1]["agent_id"], "codex:installed-layout")

    def test_launcher_is_exit_zero_when_bundle_dependency_is_missing(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cwd:
            bundle = self.install(home)
            (bundle / "durable.py").unlink()
            result = subprocess.run([str(bundle / "burrow-emit")], input="{}",
                                    text=True, cwd=cwd, capture_output=True)
            self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
