"""CI must invoke the runner whose locked environment test_runner.py proves.

This is a narrow policy for a direct runner step, not a shell interpreter: if CI
moves orchestration into another script, test that script's behavior instead.
"""

import pathlib
import shlex
import unittest

import yaml


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "chronicle.yml"


def join_shell_continuations(script):
    """shlex retains escaped newlines; POSIX shells remove them before splitting.

    Preserve quoted literals and escaped backslashes. This only joins physical
    lines, leaving command recognition to shlex and the direct-step policy.
    """
    result = []
    quote = None
    index = 0
    while index < len(script):
        char = script[index]
        if char == "\\" and quote != "'" and index + 1 < len(script):
            following = script[index + 1]
            if following != "\n":
                result.extend((char, following))
            index += 2
            continue
        if char == "#" and quote is None:
            # shlex's comments=True ignores the rest of this physical line.
            end = script.find("\n", index)
            if end == -1:
                end = len(script)
            result.append(script[index:end])
            index = end
            continue
        if char == quote:
            quote = None
        elif quote is None and char in ("'", '"'):
            quote = char
        result.append(char)
        index += 1
    return "".join(result)


def invokes_runner(workflow):
    config = yaml.safe_load(workflow)
    job = config.get("jobs", {}).get("test", {})
    # Require an unconditional step; text in disabled/conditional work cannot
    # establish that the suite runs on every invocation of the test job.
    if "if" in job:
        return False
    directory = job.get("defaults", {}).get("run", {}).get(
        "working-directory",
        config.get("defaults", {}).get("run", {}).get("working-directory"),
    )
    for step in job.get("steps", []):
        if "if" in step or step.get("working-directory", directory) != "chronicle":
            continue
        command = shlex.split(join_shell_continuations(step.get("run", "")), comments=True)
        if command[:2] == ["uv", "run"]:
            command = command[2:]
            if command[:1] == ["--frozen"]:
                command = command[1:]
        if command == ["sh", "tests/run.sh"]:
            return True
    return False


class CiWorkflowTests(unittest.TestCase):
    def test_ci_executes_the_locked_environment_runner(self):
        self.assertTrue(invokes_runner(WORKFLOW.read_text()))

    def test_equivalent_workflow_formatting_and_wrappers(self):
        for command in ("sh 'tests/run.sh'", 'uv run sh "tests/run.sh"',
                        "uv run --frozen sh tests/run.sh"):
            with self.subTest(command=command):
                self.assertTrue(invokes_runner(f"""
# No setup-node, node-version, pnpm or npm required.
defaults: {{run: {{working-directory: 'chronicle'}}}}
jobs:
  test:
    defaults: {{run: {{shell: bash}}}}
    steps:
      - uses: actions/setup-python@v7
        with: {{python-version: '3.14'}}
      - run: |
          {command} # run the suite
"""))

    def test_shell_line_continuations_preserve_runner_arguments(self):
        continuation = "\\\n"
        for command, expected in (
            ("sh " + continuation + "  tests/run.sh", True),
            ('uv run sh "tests/' + continuation + 'run.sh"', True),
            ("sh 'tests/" + continuation + "run.sh'", False),
            ("sh tests/\\" + continuation + "run.sh", False),
        ):
            with self.subTest(command=command):
                workflow = (
                    "defaults: {run: {working-directory: chronicle}}\n"
                    "jobs:\n  test:\n    steps:\n      - run: |\n          "
                    + command.replace("\n", "\n          ") + "\n"
                )
                self.assertEqual(invokes_runner(workflow), expected)

    def test_missing_runner_cannot_be_supplied_by_comments_or_another_job(self):
        for step in (
            "run: uv sync --frozen",
            "run: echo 'sh tests/run.sh'",
            "run: sh tests/run.sh --list",
            "run: sh tests/run.sh\n        if: false",
            "run: sh tests/run.sh\n        working-directory: steward",
        ):
            with self.subTest(step=step):
                self.assertFalse(invokes_runner(f"""
# sh tests/run.sh; uv run sh tests/run.sh
defaults: {{run: {{working-directory: chronicle}}}}
jobs:
  test:
    steps:
      - {step}
      # - run: sh tests/run.sh
  unrelated:
    steps:
      - run: sh tests/run.sh
"""))

    def test_disabled_test_job_does_not_establish_execution(self):
        self.assertFalse(invokes_runner("""
defaults: {run: {working-directory: chronicle}}
jobs:
  test:
    if: false
    steps:
      - run: sh tests/run.sh
"""))


if __name__ == "__main__":
    unittest.main()
