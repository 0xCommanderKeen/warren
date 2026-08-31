"""The resident runtime image, checked without a docker anywhere near CI.

``docker/resident/`` is the image a provisioned resident runs (steward #51), and CI has no
docker in it — building here would be slow, unreliable, and would still not prove the
thing that actually breaks. What actually breaks is *drift*: the vendored emitter falling
behind burrow's, a hook quietly disappearing out of ``settings.json``, the compose default
naming an image nobody builds. Those are all readable off the files, so they are asserted
off the files.

The one thing these tests cannot say is that the image builds and that a container made
from it reaches the village. ``docker/resident/smoke.sh`` says that, from inside the
container, against a real burrow — it is checked for syntax here and run by a human (or by
the pilot) there.
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import REPO_ROOT, ResidentWriter, valid_manifest
from steward.deploy import DEFAULT_IMAGE, render_compose, target_for
from steward.manifest import load_manifest

IMAGE_DIR = REPO_ROOT / "docker" / "resident"
DOCKERFILE = IMAGE_DIR / "Dockerfile"
EMITTER = IMAGE_DIR / "burrow-emit.py"
CHECKSUM = IMAGE_DIR / "burrow-emit.sha256"
SETTINGS = IMAGE_DIR / "settings.json"
SMOKE = IMAGE_DIR / "smoke.sh"
ENTRYPOINT = IMAGE_DIR / "entrypoint.sh"

#: The line the vendoring header ends with. Everything after it is burrow's file, verbatim.
MARKER = "# --- upstream copy begins below; every byte after this line is burrow's, verbatim ---\n"

#: Every hook the Mac's ~/.claude/settings.json wires the emitter into, which is the whole
#: set burrow's protocol has a mapping for. A resident missing any of them is a villager
#: who is silently only half-visible.
HOOKS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification", "Stop", "SessionEnd")


# ------------------------------------------------------------------ the default image


def test_a_resident_defaults_to_the_image_this_repo_builds() -> None:
    """`steward-resident:latest` is docker/resident/Dockerfile, and it has to exist."""
    assert DEFAULT_IMAGE == "steward-resident:latest"
    assert DOCKERFILE.is_file()


def test_the_compose_template_renders_the_new_default(write_resident: ResidentWriter) -> None:
    """A manifest with no deploy block gets the image with claude and the emitter in it."""
    resident = load_manifest(write_resident(valid_manifest()))
    service = yaml.safe_load(render_compose(resident, target_for(resident.manifest)))["services"]

    assert service["test-agent"]["image"] == "steward-resident:latest"


def test_a_manifest_can_still_name_another_image(write_resident: ResidentWriter) -> None:
    """The default is a default. life-agent's own manifest depends on this working."""
    manifest = valid_manifest() | {"deploy": {"image": "node:22"}}
    resident = load_manifest(write_resident(manifest))
    service = yaml.safe_load(render_compose(resident, target_for(resident.manifest)))["services"]

    assert service["test-agent"]["image"] == "node:22"


def test_the_claude_config_volume_is_where_the_emitter_lands(
    write_resident: ResidentWriter,
) -> None:
    """The entrypoint seeds /root/.claude, so the compose fragment has to mount it there."""
    resident = load_manifest(write_resident(valid_manifest()))
    service = yaml.safe_load(render_compose(resident, target_for(resident.manifest)))["services"]

    assert "./claude:/root/.claude" in service["test-agent"]["volumes"]


# ------------------------------------------------------------------- the vendored emitter


def recorded() -> dict[str, str]:
    """Read docker/resident/burrow-emit.sha256 — what `make vendor-emitter` wrote down."""
    values = {}
    for line in CHECKSUM.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        values[key.strip()] = value.strip()
    return values


def test_the_vendored_emitter_is_burrows_file_byte_for_byte() -> None:
    """The copy in this repo must hash to exactly what was vendored from burrow.

    This is the whole safety argument for vendoring rather than submoduling: the copy is
    allowed to be a copy precisely because a test says it is *the same* copy. An emitter
    that drifted would put a resident on a protocol version the village does not read, and
    would do it silently — a villager that simply never appears.
    """
    raw = EMITTER.read_text(encoding="utf-8")
    assert MARKER in raw, f"{EMITTER} has no vendoring marker; re-run `make vendor-emitter`"

    header, _, upstream = raw.partition(MARKER)
    digest = hashlib.sha256(upstream.encode("utf-8")).hexdigest()
    expected = recorded()

    assert re.fullmatch(r"[0-9a-f]{40}", expected.get("commit", "")), (
        f"{CHECKSUM} must record the full upstream commit"
    )
    assert digest == expected["sha256"], (
        f"{EMITTER} no longer matches the emitter recorded in {CHECKSUM.name}.\n"
        f"  recorded: {expected['sha256']} (burrow commit {expected['commit']})\n"
        f"  on disk:  {digest}\n"
        f"Do not hand-edit the vendored copy. Change hooks/emit.py in burrow, commit it "
        f"there, then run:  make vendor-emitter BURROW=/path/to/burrow"
    )
    assert expected["commit"] in header, "the header must name the commit the bytes came from"
    assert "DO NOT EDIT HERE" in header


def test_the_vendored_emitter_still_needs_only_the_standard_library() -> None:
    """One file, no pip: it is why the image installs python3 and nothing else for this."""
    upstream = EMITTER.read_text(encoding="utf-8").partition(MARKER)[2]
    imported = {
        line.removeprefix("import ").strip()
        for line in upstream.splitlines()
        if line.startswith("import ")
    }

    assert imported <= {
        "datetime",
        "fcntl",
        "hashlib",
        "json",
        "os",
        "sys",
        "time",
        "urllib.request",
    }, f"the emitter grew a dependency ({imported}); the image installs no pip"


# ------------------------------------------------------------------------ settings.json


def test_the_settings_template_is_valid_json() -> None:
    """An unparseable settings.json means claude starts with no hooks and says nothing."""
    json.loads(SETTINGS.read_text(encoding="utf-8"))


@pytest.mark.parametrize("hook", HOOKS)
def test_every_burrow_hook_is_wired(hook: str) -> None:
    """Each of the six hooks burrow maps must call the emitter, or the villager half-lives."""
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    entries = settings["hooks"][hook]

    commands = [
        step["command"]
        for entry in entries
        for step in entry["hooks"]
        if step.get("type") == "command"
    ]
    assert commands, f"{hook} is declared but runs nothing"
    assert all("burrow-emit.py" in command for command in commands)


def test_the_hooks_carry_no_url_and_no_token() -> None:
    """The village address comes from the container's environment, never from this file.

    The Mac's settings.json inlines `BURROW_URL=…` because a laptop has no compose .env.
    A resident does: steward writes BURROW_URL and BURROW_TOKEN into a 0600 .env at
    provision time, and a hardcoded copy here would be a second place to keep in step and
    a first place for a secret to leak into an image.
    """
    text = SETTINGS.read_text(encoding="utf-8")

    assert "BURROW_URL" not in text
    assert "BURROW_TOKEN" not in text


def test_the_settings_template_names_no_other_hooks() -> None:
    """The image wires burrow and nothing else; a surprise hook in a resident is a bug."""
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))

    assert set(settings["hooks"]) == set(HOOKS)
    assert set(settings) == {"hooks"}


# --------------------------------------------------------------------------- the scripts


@pytest.mark.parametrize("script", [SMOKE, ENTRYPOINT])
def test_the_shell_scripts_parse(script: Path) -> None:
    """`sh -n` on both: a container whose entrypoint has a typo is a resident that never starts."""
    result = subprocess.run(  # noqa: S603 — a fixed argv, no shell, no template
        ["/bin/sh", "-n", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"{script.name}: {result.stderr.strip()}"


def test_the_smoke_test_asserts_the_status_code_issue_51_asks_for() -> None:
    """#51's acceptance is a 204 from inside the container. It has to be checked for."""
    text = SMOKE.read_text(encoding="utf-8")

    assert "204" in text
    assert "$url/events" in text
    assert 'exit "$failed"' in text, "a smoke test that always exits 0 proves nothing"


def test_the_smoke_test_only_ever_posts_a_heartbeat_under_a_probe_identity() -> None:
    """A test may prove the pipe works. It may not conjure a villager who did no work.

    heartbeat is liveness-only in burrow's projection, and `steward-smoke:<host>` is
    visibly not a resident — so running the smoke test can never leave the village showing
    work that nobody did, which is the one rule the whole fleet is built around.
    """
    text = SMOKE.read_text(encoding="utf-8")

    assert '"type":"heartbeat"' in text
    assert "steward-smoke:" in text
    for forged in ("task_started", "artifact_produced", "needs_human"):
        assert forged not in text


# ----------------------------------------------------------------------------- Dockerfile


def test_the_claude_cli_is_pinned_by_build_arg() -> None:
    """An image whose brain changes version under it on a rebuild explains nothing later."""
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG CLAUDE_VERSION=" in text
    assert "@anthropic-ai/claude-code@${CLAUDE_VERSION}" in text
    assert "claude --version" in text, "the build must fail here, not inside a 07:00 routine"


def test_the_image_carries_everything_a_session_needs() -> None:
    """python3 for the emitter, git for the work, and the two files the entrypoint seeds."""
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "python3" in text
    assert "git" in text
    assert "COPY burrow-emit.py /opt/steward/burrow-emit.py" in text
    assert "COPY settings.json /opt/steward/settings.json" in text


def test_the_container_still_just_stays_up() -> None:
    """A resident's container is a place for sessions to happen, not a process doing work.

    That is true under either placement (steward #58): steward drives the brain from
    outside — locally by default, or `docker exec`-ed in for `placement: container` — so
    `sleep infinity` stays the honest default command either way.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert 'CMD ["sleep", "infinity"]' in text
    assert 'ENTRYPOINT ["/usr/local/bin/steward-entrypoint"]' in text


def test_the_local_dev_mirror_is_off_inside_a_container() -> None:
    """Nothing listens on the container's own loopback; every hook would pay for finding out."""
    assert 'BURROW_MIRROR=""' in DOCKERFILE.read_text(encoding="utf-8")


# ------------------------------------------------------------------- life-agent, as it is


def test_life_agent_declares_where_it_actually_runs() -> None:
    """Steward #52: Hob's container is hand-deployed and named life-agent. Say so.

    Without `deploy.container` the watchdog calls Hob *unsupervised*, which was a fact
    about steward rather than about the world. These values were read off the running
    container, not chosen.
    """
    hob = load_manifest(REPO_ROOT / "residents" / "life-agent" / "manifest.yaml")
    deploy = hob.manifest.deploy

    assert deploy.container == "life-agent", "not steward-life-agent: nobody renamed it"
    assert deploy.host == "dxp2800"
    assert deploy.user == "Miha"
    assert deploy.path == "~/docker/life-agent"
    assert deploy.image == "node:22", (
        "Hob still runs the hand-rolled node:22 container that installs claude at every "
        "cold start; moving him onto steward-resident is a migration with a cutover"
    )
    assert not deploy.command, (
        "Hob's real command is a 40-line bash bootstrap in a compose file steward does "
        "not own, so the manifest declares nothing about it rather than something tidier"
    )
