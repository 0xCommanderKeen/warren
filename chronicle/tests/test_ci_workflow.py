import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "chronicle.yml"


class CiWorkflowTests(unittest.TestCase):
    def test_ci_uses_the_locked_python_environment(self):
        workflow = WORKFLOW.read_text()

        self.assertIn('python-version: "3.14"', workflow)
        self.assertIn("astral-sh/setup-uv@", workflow)
        self.assertIn("uv sync --frozen", workflow)
        self.assertIn("uv run sh tests/run.sh", workflow)


if __name__ == "__main__":
    unittest.main()
