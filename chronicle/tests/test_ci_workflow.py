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
        self.assertIn("- run: sh tests/run.sh", workflow)
        self.assertNotIn("uv run sh tests/run.sh", workflow)

    def test_ci_installs_no_node_toolchain(self):
        """The suite is single-language; a Node step here would be dead weight."""
        workflow = WORKFLOW.read_text()

        self.assertNotIn("setup-node", workflow)
        self.assertNotIn("node-version", workflow)
        self.assertNotIn("pnpm", workflow)
        self.assertNotIn("npm ", workflow)


if __name__ == "__main__":
    unittest.main()
