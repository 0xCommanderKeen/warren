"""What a real ``claude`` does with the settings document steward declares (steward #264).

The unit tests in ``test_runners.py`` pin the argv and the document's shape against a
stubbed brain. This module proves the two claims a stub cannot, and they are the two
halves of one sentence:

- a session launched by **steward's own argv** fires steward's emit hook, and
- a ``.claude/settings.json`` in that same session's working directory still fires nothing.

That pairing is the whole of #264. ``--setting-sources ""`` (steward #206) closed the
settings channel because a resident's working directory *is* its memory directory, and it
closed the chronicle telemetry riding on that channel along with it. Restoring the
telemetry is only safe if it restores exactly one direction: steward may declare hooks, and
a resident still may not.

Skipped wholesale when no ``claude`` is on PATH, so the suite stays runnable on CI and on a
machine that has never installed it — the same shape as ``test_container_integration.py``.

**It costs nothing to run.** The session is launched under a scratch ``CLAUDE_CONFIG_DIR``
with no credentials in it, with every model credential removed from the environment, and
with ``ANTHROPIC_BASE_URL`` pointed at a port nothing listens on. It dies unauthenticated —
which is fine, because a ``UserPromptSubmit`` hook fires *before* the first API call, so the
sentinel is written by a session that never reaches anybody's API. Nothing here asserts on
what the session said; a session's self-report of its own settings was measured to be
confabulation (``docs/settings-sources.md``), so only files on disk are evidence.
"""

import json
import shutil
from pathlib import Path

import pytest

from steward import runners as r
from steward.manifest import Runner as RunnerSpec
from steward.manifest import ToolGrant

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None, reason="no claude on PATH to measure against"
)

UNRESTRICTED = ToolGrant("unrestricted")

#: A stand-in for chronicle's emitter: stdlib-only python that touches a file. What is
#: under test is whether the hook *runs*, and the real emitter would need a village.
#:
#: It takes its sentinel from the environment rather than from an argument, because
#: :data:`~steward.runners.SESSION_EMITTER_ENV` names a *script*, not a command line —
#: steward shell-quotes it into `python3 <path>`. So the test reaches the sentinel the way
#: anything else reaches a session: through the passthrough hatch.
SENTINEL_ENV = "STEWARD_TEST_SENTINEL"
SENTINEL_EMITTER = f"""\
import os
import pathlib

pathlib.Path(os.environ[{SENTINEL_ENV!r}]).write_text("fired", encoding="utf-8")
"""

#: Where a session could not reach Anthropic even if it somehow held a credential.
UNREACHABLE_API = "http://127.0.0.1:1"


@pytest.fixture
def sealed_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the session at a scratch config dir with no credentials and nowhere to call.

    Returns the working directory the session runs in. Every name here is one steward's
    own allowlist would forward (``SESSION_ENV_BASE`` and ``CLAUDE_ENV_NAMES``), so this
    seals the real launch path rather than a copy of it.
    """
    config = tmp_path / "config"
    config.mkdir()
    (config / ".claude.json").write_text("{}", encoding="utf-8")
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", UNREACHABLE_API)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return workdir


def sentinel_emitter(path: Path) -> Path:
    """Write a stand-in emitter beside ``path`` and return it, for steward to name."""
    script = path.parent / f"{path.name}-emitter.py"
    script.write_text(SENTINEL_EMITTER, encoding="utf-8")
    return script


def test_steward_declares_hooks_a_resident_still_cannot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sealed_claude: Path
) -> None:
    """One session, two settings documents, one of them steward's — only steward's fires."""
    ours = tmp_path / "steward-fired"
    theirs = tmp_path / "resident-fired"
    monkeypatch.setenv(r.SESSION_EMITTER_ENV, str(sentinel_emitter(ours)))
    monkeypatch.setenv(SENTINEL_ENV, str(ours))
    monkeypatch.setenv(r.SESSION_ENV_PASSTHROUGH_ENV, SENTINEL_ENV)

    # What a resident with write access to its own memory directory would plant. Its hook
    # is `SessionStart`, which fires earliest of all — so if the sources were open at all,
    # this is the sentinel that would appear first. It names its own file directly: nothing
    # about a resident's settings file goes through steward, which is the point.
    resident_settings = sealed_claude / ".claude"
    resident_settings.mkdir()
    (resident_settings / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"touch {theirs}",
                                    "timeout": 5,
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    r.build_runner(RunnerSpec(kind="claude")).run(
        r.RunRequest(
            prompt="say ok",
            workdir=sealed_claude,
            timeout_s=120,
            tools=UNRESTRICTED,
        )
    )

    assert ours.exists(), "steward's declared hook did not fire; per-session telemetry is off"
    assert not theirs.exists(), (
        "a settings file in the session's own working directory registered a hook — "
        "steward #206's boundary has been reopened"
    )


def test_a_session_with_no_emitter_named_declares_nothing_and_still_runs(
    monkeypatch: pytest.MonkeyPatch, sealed_claude: Path
) -> None:
    """The unconfigured local default has to launch, not fail on a settings file.

    Measured 2026-09-04 against CLI 2.1.260: `--settings <missing path>` is `Error:
    Settings file not found` and exit 1 with no session at all. That is why steward names
    a document only where it has an emitter to name, and this is the test that the
    "names nothing" branch really is a session and not a launch failure.
    """
    monkeypatch.delenv(r.SESSION_EMITTER_ENV, raising=False)
    runner = r.ClaudeRunner(RunnerSpec(kind="claude"))
    request = r.RunRequest(
        prompt="say ok", workdir=sealed_claude, timeout_s=120, tools=UNRESTRICTED
    )

    assert r.SETTINGS_FLAG not in runner.argv(request)

    result = runner.run(request)

    # It dies unauthenticated, which is the point of the seal; what matters is *how*.
    assert "Settings file not found" not in (result.error or "")
