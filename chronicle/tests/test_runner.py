"""Regression tests for the repository's dependency-free test runner."""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests" / "run.sh"


class RunnerDiscoveryTest(unittest.TestCase):
    def test_list_reports_every_tracked_repository_test_exactly_once(self):
        tracked_output = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        tracked = [os.fsdecode(path) for path in tracked_output.split(b"\0") if path]
        expected = sorted(
            path
            for path in tracked
            if pathlib.PurePosixPath(path).name.startswith("test_")
            and pathlib.PurePosixPath(path).suffix == ".py"
        )

        result = subprocess.run(
            ["sh", str(RUNNER), "--list"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        listed = [
            line.removeprefix("== ")
            for line in result.stdout.splitlines()
            if line.startswith("== ")
        ]

        self.assertEqual(listed, expected)
        self.assertEqual([path for path in listed if path.endswith(".js")], [])

    def test_normal_run_executes_each_tracked_test_once(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = pathlib.Path(directory)
            (repo / "tests").mkdir()
            shutil.copy2(RUNNER, repo / "tests" / "run.sh")
            shutil.copy2(ROOT / "tests" / "runner.py", repo / "tests" / "runner.py")

            python_tests = [
                repo / "test_alpha.py",
                repo / "nested" / "test_odd\nname.py",
            ]
            python_tests[1].parent.mkdir()
            for test in python_tests:
                test.write_text(
                    "import os\n"
                    "with open(os.environ['BURROW_RUNNER_LOG'], 'a') as log:\n"
                    "    log.write(os.path.basename(__file__).replace('\\n', '<NL>') + '\\n')\n"
                )
            # Burrow runs no JavaScript. A tracked .js test is discovered by
            # nothing and executed by nothing, rather than quietly requiring a
            # Node toolchain the suite no longer installs.
            javascript_tests = [
                repo / "test_root.js",
                repo / "nested" / "model.test.js",
            ]
            for test in javascript_tests:
                test.write_text(
                    "require('fs').appendFileSync(process.env.BURROW_RUNNER_LOG, "
                    f"'{test.name}\\n');\n"
                )
            (repo / "test_untracked.py").write_text(
                "raise SystemExit('must not run')\n"
            )

            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "add",
                    "tests/run.sh",
                    "tests/runner.py",
                    "test_alpha.py",
                    "nested/test_odd\nname.py",
                    "test_root.js",
                    "nested/model.test.js",
                ],
                cwd=repo,
                check=True,
            )
            log = repo / "executed.log"
            env = dict(os.environ, BURROW_RUNNER_LOG=str(log), PYTHON=sys.executable)
            result = subprocess.run(
                ["sh", "tests/run.sh"],
                cwd=repo,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(
                sorted(log.read_text().splitlines()),
                ["test_alpha.py", "test_odd<NL>name.py"],
            )
            self.assertEqual(result.stdout.splitlines()[-1], "all green")


if __name__ == "__main__":
    unittest.main()
