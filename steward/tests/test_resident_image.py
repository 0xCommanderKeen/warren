"""The resident runtime image, checked off the files rather than by building it.

``docker/resident/`` is the image a provisioned resident runs (steward #51). CI *does* now
build it — the `image` job in ``.github/workflows/steward.yml`` builds it, runs the
entrypoint and runs ``smoke.sh`` against a stub village (steward #158) — but a build says
nothing about *drift*: the vendored emitter falling behind chronicle's, a hook quietly
disappearing out of ``settings.json``, the compose default naming an image nobody builds,
the two copies of the ``CLAUDE_VERSION`` pin walking apart. Those are all readable off the
files, and reading them costs a millisecond where a build costs minutes. So they are
asserted off the files here, and the build job proves the layers actually run.

The one thing neither can say is that a container made from this image reaches the *real*
village. ``docker/resident/smoke.sh`` says that, from inside the container, against a real
chronicle — run by a human (or by the pilot) there.
"""

import ast
import hashlib
import json
import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import REPO_ROOT, ResidentWriter, valid_manifest
from steward.deploy import DEFAULT_IMAGE, memory_mount, render_compose, target_for
from steward.manifest import load_manifest

IMAGE_DIR = REPO_ROOT / "docker" / "resident"
DOCKERFILE = IMAGE_DIR / "Dockerfile"
EMITTER = IMAGE_DIR / "chronicle-emit.py"
SETTINGS = IMAGE_DIR / "settings.json"
SMOKE = IMAGE_DIR / "smoke.sh"
ENTRYPOINT = IMAGE_DIR / "entrypoint.sh"
MAKEFILE = REPO_ROOT / "Makefile"

#: Chronicle, in the same monorepo since 2026-08-31 — so the emitter's source is always
#: right there and the drift check can *build* rather than trust a number somebody wrote
#: down. ``make vendor-emitter CHRONICLE=…`` still exists for a checkout elsewhere; this
#: test does not, because in this repository the sibling is not optional.
BUNDLE_BUILD = REPO_ROOT.parent / "chronicle" / "hooks" / "build.py"

#: This service's CI, at the repository root: path-filtered per service, which is why the
#: filter itself is something this suite has an opinion about.
WORKFLOW = REPO_ROOT.parent / ".github" / "workflows" / "steward.yml"
CONTROL_PLANE_DOCKERFILE = REPO_ROOT / "docker" / "control-plane" / "Dockerfile"
DEPLOY_COMPOSE = REPO_ROOT / "deploy" / "compose.yaml"

#: Every hook the Mac's ~/.claude/settings.json wires the emitter into, which is the whole
#: set burrow's protocol has a mapping for. A resident missing any of them is a villager
#: who is silently only half-visible.
HOOKS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification", "Stop", "SessionEnd")


# ------------------------------------------------------------------ the default image


def test_a_resident_defaults_to_the_image_this_repo_builds() -> None:
    """`steward-resident:latest` is docker/resident/Dockerfile, and it has to exist."""
    assert DEFAULT_IMAGE == "steward-resident:latest"
    assert DOCKERFILE.is_file()


def test_runtime_switch_keeps_resident_identity_and_changes_event_source() -> None:
    """The wire join key belongs to the Resident; source belongs to the producer."""
    emitter = runpy.run_path(str(EMITTER))
    identity = "resident:7e36d76a-1ad8-4d65-a619-8c6e7fb93ed9"

    assert emitter["hook_agent_id"]("claude", {}, identity) == identity
    assert emitter["hook_agent_id"]("codex", {}, identity) == identity
    assert emitter["RUNNER_SOURCES"] == {"claude": "claude-code", "codex": "codex"}


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


def test_the_vendored_emitter_is_the_bundle_chronicle_builds_today(tmp_path: Path) -> None:
    """Build chronicle's emitter bundle at HEAD and compare. Not a number, the bytes.

    What used to stand here re-hashed the copy against a checksum ``make vendor-emitter``
    had written down beside it. That can only ever catch somebody editing the *copy*: a
    pinned hash stays green forever while the source sails away, which is exactly what
    happened (warren#234). Comparing against a live build cannot go stale, because there
    is nothing recorded to go stale.

    An emitter that drifted would put a resident on a protocol version the village does not
    read, and would do it silently — a villager that simply never appears.
    """
    assert BUNDLE_BUILD.is_file(), (
        f"{BUNDLE_BUILD} is missing. chronicle is a sibling directory in this monorepo, "
        f"and the resident image's emitter is built from it."
    )
    built = tmp_path / "chronicle-emit.py"

    result = subprocess.run(  # noqa: S603 — a fixed argv, no shell, no template
        [sys.executable, str(BUNDLE_BUILD), "--output", str(built)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"chronicle's bundle build failed:\n{result.stderr}"
    # Compared as digests rather than as two 70KB strings: the answer is yes or no, and a
    # failure should say what to run, not print a two-thousand-line diff nobody reads.
    on_disk = hashlib.sha256(EMITTER.read_bytes()).hexdigest()
    rebuilt = hashlib.sha256(built.read_bytes()).hexdigest()

    assert on_disk == rebuilt, (
        f"{EMITTER} is not what chronicle/hooks builds today.\n"
        f"  vendored: {on_disk}\n"
        f"  rebuilt:  {rebuilt}\n"
        f"Do not hand-edit the vendored copy — it is a generated artifact. Change "
        f"chronicle/hooks/emit.py or chronicle/hooks/durable.py, commit it, then run:  "
        f"make vendor-emitter   (in warren/steward/)"
    )


def test_ci_runs_this_suite_when_the_emitters_source_changes() -> None:
    """The comparison above is only a guard if CI runs it on the PR that breaks it.

    The root workflows are path-filtered per service, so a chronicle-only change would not
    have run steward's suite at all — the drift would have waited for whoever next ran
    `make vendor-emitter`, which is exactly how the copy went stale in the first place.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # `on:` is YAML 1.1's boolean true, and pyyaml reads it as one.
    triggers = workflow.get("on", workflow.get(True))

    for event in ("push", "pull_request"):
        assert "chronicle/hooks/**" in triggers[event]["paths"], (
            f"steward.yml does not run on {event} for chronicle/hooks/**, so an emitter "
            f"change would not run the test that says the vendored copy is stale"
        )


def imported_modules(source: str) -> set[str]:
    """Every top-level module a source imports, wherever in the file the import sits."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def embedded_module(source: str) -> str:
    """Read the durable-outbox source the bundle carries, statically, off the artifact.

    It lives in a string literal, so an import walk over the file cannot see what it
    imports — and what the artifact *executes* is both halves.
    """
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and "_DURABLE_SOURCE" in [t.id for t in node.targets if isinstance(t, ast.Name)]
        ):
            return str(node.value.value)
    raise AssertionError(f"{EMITTER} carries no embedded module; the bundle format changed")


def test_the_vendored_emitter_still_needs_only_the_standard_library() -> None:
    """No pip: it is why the image installs python3 and nothing else for this.

    Both halves of the artifact, because both halves run. Chronicle's suite owns the
    authoritative version of this check — it is the side that can break the constraint —
    and this is the image's own stake in it: what ``apt-get install python3`` has to be
    enough for. What it cannot say is that the *image's* python is new enough to run the
    file; the `image` job says that, by running it inside the container, where a
    SyntaxError is an exit status ``smoke.sh`` fails on.
    """
    artifact = EMITTER.read_text(encoding="utf-8")
    imported = imported_modules(artifact) | imported_modules(embedded_module(artifact))

    assert imported <= sys.stdlib_module_names, (
        f"the emitter grew a dependency ({sorted(imported - sys.stdlib_module_names)}); "
        f"the image installs no pip"
    )


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
    assert all("chronicle-emit.py" in command for command in commands)


def test_the_hooks_carry_no_url_and_no_token() -> None:
    """The village address comes from the container's environment, never from this file.

    The Mac's settings.json inlines `CHRONICLE_URL=…` because a laptop has no compose
    .env. A resident does: steward writes CHRONICLE_URL and CHRONICLE_TOKEN into a 0600
    .env at provision time, and a hardcoded copy here would be a second place to keep in
    step and a first place for a secret to leak into an image.
    """
    text = SETTINGS.read_text(encoding="utf-8")

    assert "CHRONICLE_URL" not in text
    assert "CHRONICLE_TOKEN" not in text
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


def seed_volume(
    tmp_path: Path,
    settings: str | None,
    emitter_names: tuple[str, ...],
    *,
    settings_writable: bool = True,
) -> tuple[Path, str]:
    """Run the entrypoint against a fake claude volume; return the volume and what it said.

    The two directories the script uses are absolute container paths, so a copy with those
    two assignments repointed is what runs here. Everything under test — which file is
    copied in, what happens to a settings.json that is already there — is the copy's own
    logic, unmodified. The image job in CI runs the real script in the real image; this is
    the same behaviour at a millisecond, so a broken migration is red before a build.
    """
    volume, baked = tmp_path / "claude", tmp_path / "baked"
    volume.mkdir()
    baked.mkdir()
    (baked / "chronicle-emit.py").write_text("# the emitter\n", encoding="utf-8")
    (baked / "settings.json").write_text(SETTINGS.read_text(encoding="utf-8"), encoding="utf-8")
    if settings is not None:
        (volume / "settings.json").write_text(settings, encoding="utf-8")
        if not settings_writable:
            (volume / "settings.json").chmod(0o444)
    for name in emitter_names:
        (volume / name).write_text("# a stale emitter\n", encoding="utf-8")

    script = tmp_path / "entrypoint.sh"
    script.write_text(
        re.sub(
            r"^BAKED_DIR=.*$",
            f"BAKED_DIR={baked}",
            re.sub(
                r"^CONFIG_DIR=.*$",
                f"CONFIG_DIR={volume}",
                ENTRYPOINT.read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            ),
            flags=re.MULTILINE,
        ),
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 — a fixed argv, no shell, no template
        ["/bin/sh", str(script), "true"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    return volume, result.stdout


def test_a_pre_rename_volume_is_repointed_at_the_new_emitter(tmp_path: Path) -> None:
    """warren#361: the claude volume outlives the image, and the rename is not a code change.

    A resident provisioned before the rename comes back up with `burrow-emit.py` in its
    volume and a settings.json naming it — a file the new image does not ship. Nothing
    fails: claude starts, the agent works, and the hooks run a script that is not there.
    So the entrypoint repoints that one string and removes the stale copy.
    """
    legacy = SETTINGS.read_text(encoding="utf-8").replace("chronicle-emit.py", "burrow-emit.py")
    document = json.loads(legacy)
    # An operator's own addition, to prove the repoint is surgical rather than a clobber.
    document["permissions"] = {"allow": ["Bash(ls:*)"]}

    volume, said = seed_volume(tmp_path, json.dumps(document, indent=2), ("burrow-emit.py",))

    settings = json.loads((volume / "settings.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for group in settings["hooks"].values()
        for entry in group
        for hook in entry["hooks"]
    ]
    assert commands
    assert all("chronicle-emit.py" in command for command in commands)
    assert "burrow-emit.py" not in json.dumps(settings)
    assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert not (volume / "burrow-emit.py").exists()
    assert (volume / "chronicle-emit.py").is_file()
    assert "repointed" in said


def test_a_stale_emitter_stays_while_anything_still_names_it(tmp_path: Path) -> None:
    """The removal is conditional, and the condition has to be able to hold.

    If the repoint fails, the reference to the old name survives — and deleting the file
    it points at would then be exactly the silent failure the whole block exists to avoid.
    A read-only settings.json is the reachable version of that: the file still parses and
    still names `burrow-emit.py`, and the write is refused. (Under docker the entrypoint
    is root, for which a mode bit is not a refusal; a read-only bind mount is.)
    """
    legacy = SETTINGS.read_text(encoding="utf-8").replace("chronicle-emit.py", "burrow-emit.py")

    volume, said = seed_volume(tmp_path, legacy, ("burrow-emit.py",), settings_writable=False)

    assert "could not repoint" in said
    assert "burrow-emit.py" in (volume / "settings.json").read_text(encoding="utf-8")
    assert (volume / "burrow-emit.py").is_file()


def test_an_unreadable_settings_json_is_reported_rather_than_rewritten(tmp_path: Path) -> None:
    """Parsed before it is touched: a file that is already broken is a message, not a write."""
    volume, said = seed_volume(tmp_path, "{not json", ())

    assert "unreadable" in said
    assert (volume / "settings.json").read_text(encoding="utf-8") == "{not json"


def test_a_volume_seeded_after_the_rename_is_left_alone(tmp_path: Path) -> None:
    """The migration is a no-op on a resident that never saw the old name."""
    volume, said = seed_volume(tmp_path, SETTINGS.read_text(encoding="utf-8"), ())

    assert (volume / "settings.json").read_text(encoding="utf-8") == SETTINGS.read_text(
        encoding="utf-8"
    )
    assert "repointed" not in said
    assert "does not wire" not in said


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


def test_the_canary_image_boots_pips_heartbeat_end_to_end_in_ci() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "SMOKE_AGENT_ID=steward:pip" in workflow
    assert "CHRONICLE_PROJECT=pip" in workflow


def test_the_control_plane_carries_no_local_brain_or_login() -> None:
    dockerfile = CONTROL_PLANE_DOCKERFILE.read_text(encoding="utf-8")
    instructions = "\n".join(
        line for line in dockerfile.lower().splitlines() if not line.lstrip().startswith("#")
    )
    compose = yaml.safe_load(DEPLOY_COMPOSE.read_text(encoding="utf-8"))

    assert "claude-code" not in instructions
    assert "npm install" not in instructions
    assert "/usr/local/bin/node" not in instructions
    assert all(
        "claude-config" not in str(volume)
        for service in compose["services"].values()
        for volume in service.get("volumes", [])
    )


def test_both_daemons_read_the_same_resident_neutral_tree() -> None:
    document = yaml.safe_load(DEPLOY_COMPOSE.read_text(encoding="utf-8"))
    commands = [document["services"][name]["command"] for name in ("scheduler", "watchdog")]

    # The burrow's checkout, not the tree baked into the image (warren#351); the same path
    # for both, and the checkout's residents tree rather than any resident's directory.
    assert all(
        command[-2:] == ["--residents", "/checkout/steward/residents"] for command in commands
    )
    shipped_ids = {path.parent.name for path in (REPO_ROOT / "residents").glob("*/manifest.yaml")}
    assert not any(
        resident_id in " ".join(command) for resident_id in shipped_ids for command in commands
    )


# ----------------------------------------------------------------------------- Dockerfile


def test_the_claude_cli_is_pinned_by_build_arg() -> None:
    """An image whose brain changes version under it on a rebuild explains nothing later."""
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG CLAUDE_VERSION=" in text
    assert "@anthropic-ai/claude-code@${CLAUDE_VERSION}" in text
    assert "claude --version" in text, "the build must fail here, not inside a 07:00 routine"


def test_the_two_copies_of_the_claude_pin_agree() -> None:
    """The pin is written twice, so the two copies have to be held together by a test.

    ``make image`` passes the Makefile's value; a bare ``docker build docker/resident``
    takes the Dockerfile's ARG default. When they drift, which brain a resident got depends
    on how somebody happened to build the image — the exact thing the pin exists to stop.
    """
    makefile = MAKEFILE.read_text(encoding="utf-8")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    from_makefile = re.search(r"^CLAUDE_VERSION\s*\?=\s*(\S+)", makefile, re.MULTILINE)
    from_dockerfile = re.search(r"^ARG\s+CLAUDE_VERSION=(\S+)", dockerfile, re.MULTILINE)

    assert from_makefile, f"{MAKEFILE} no longer sets CLAUDE_VERSION"
    assert from_dockerfile, f"{DOCKERFILE} no longer defaults ARG CLAUDE_VERSION"
    assert from_makefile[1] == from_dockerfile[1], (
        f"the claude pin disagrees with itself: Makefile says {from_makefile[1]}, "
        f"Dockerfile's ARG default says {from_dockerfile[1]}. Bump both, or a bare "
        f"`docker build docker/resident` builds a different brain than `make image` does."
    )


def test_the_base_image_is_pinned_by_digest() -> None:
    """A tag is a moving target; a digest is the image. Two rebuilds must mean one image.

    The Dockerfile's own argument for pinning claude — "a resident whose brain silently
    changed version under it on a rebuild is a resident nobody can say anything true
    about" — is exactly as true of the ~150MB of Debian and node underneath it (#158).
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    images = re.findall(r"^\s*FROM\s+(\S+)", text, re.MULTILINE | re.IGNORECASE)

    assert images, f"{DOCKERFILE} has no FROM at all"
    unpinned = [
        image for image in images if not re.fullmatch(r"[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}", image)
    ]
    assert not unpinned, (
        f"not pinned: {unpinned}. Every FROM here must read `<image>:<tag>@sha256:<64 hex>` "
        f"— the tag so a human can see what this is, the digest because that is what docker "
        f"actually pulls."
    )
    assert "docker buildx imagetools inspect" in text, (
        "a digest with no refresh procedure beside it is a digest nobody dares to move"
    )


def test_the_image_records_the_commit_it_was_built_from() -> None:
    """An image that cannot say what it was built from is an image nobody can debug.

    ``org.opencontainers.image.revision`` is the one label that turns "the resident is
    running steward-resident:latest" into a statement with a commit in it. The ARG defaults
    to ``unknown`` rather than to a lie: a bare ``docker build`` genuinely does not know.
    """
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "ARG IMAGE_REVISION=unknown" in dockerfile
    assert 'LABEL org.opencontainers.image.revision="${IMAGE_REVISION}"' in dockerfile
    assert "--build-arg IMAGE_REVISION=$(REVISION)" in makefile
    assert "git rev-parse HEAD" in makefile, "the revision has to come from git, not by hand"


def test_the_image_labels_point_at_the_repo_that_exists() -> None:
    """`steward` was renamed to `warren` (2026-08-31); a label pointing at the tombstone lies."""
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "github.com/0xCommanderKeen/warren" in text
    assert "github.com/0xCommanderKeen/steward" not in text


def test_the_image_carries_everything_a_session_needs() -> None:
    """python3 for the emitter, git for the work, and the two files the entrypoint seeds."""
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "python3" in text
    assert "git" in text
    assert "COPY chronicle-emit.py /opt/steward/chronicle-emit.py" in text
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
    """Nothing listens on the container's own loopback; every hook would pay for finding out.

    One spelling since warren#361. The vendored emitter selects a setting on *presence*,
    not truthiness, so ``CHRONICLE_MIRROR=""`` alone already means "no mirrors" and the
    second bake bought nothing.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert 'CHRONICLE_MIRROR=""' in text
    assert "BURROW_MIRROR" not in text


# --------------------------------------------------------------- life-agent, as it deploys


def test_life_agent_declares_the_address_the_nursery_provisions() -> None:
    """Steward #40: Hob's deploy block is the nursery layout, not the hand-built container.

    Until this cut over it said `life-agent` / `~/docker/life-agent` / `node:22`, read off
    the container Hob had been running in since before steward existed (#52). These values
    are the other kind of true: they are what the nursery resolves for this resident, and
    the only address a container-placed resident can have. Merging the manifest is the
    operator's half of the cutover; `steward provision life-agent` puts the reviewed bundle
    on the NAS.
    """
    hob = load_manifest(REPO_ROOT / "residents" / "life-agent" / "manifest.yaml")
    deploy = hob.manifest.deploy

    assert deploy.container == "steward-life-agent"
    assert deploy.host == "dxp2800"
    assert deploy.user == "Miha"
    assert deploy.path == "~/docker/warren/residents/life-agent"
    assert deploy.image == DEFAULT_IMAGE, (
        "Hob runs the image this repo builds and ships, so his container has a brain "
        "before a session opens instead of installing one on every cold start"
    )
    assert not deploy.command, (
        "the default is the truth now: a resident's container is a place for sessions to "
        "happen, and `sleep infinity` under docker's init is exactly that"
    )


def test_life_agent_runs_its_sessions_inside_that_container() -> None:
    """The deploy block is an address; `placement` is what makes steward use it (#58, #40).

    Explicit, never inferred: a resident may declare a container for the watchdog to
    supervise while its sessions still run on the control plane, which is what every
    resident deployed before #58 does. Hob no longer does — which is also what puts the
    two sides of his memory mount on different machines.
    """
    hob = load_manifest(REPO_ROOT / "residents" / "life-agent" / "manifest.yaml")

    assert hob.manifest.runner.placement == "container"
    assert hob.manifest.runner.container_placed is True
    host_side, container_side = memory_mount(hob.manifest)
    assert host_side == "~/docker/warren/residents/life-agent/memory"
    assert container_side == "/data/residents/life-agent/memory"


def test_pip_renders_the_nursery_container_and_memory_mount() -> None:
    """Issue #40/#332: Pip is the second explicit operator placement proposal."""
    pip = load_manifest(REPO_ROOT / "residents" / "pip" / "manifest.yaml")
    target = target_for(pip.manifest)
    service = yaml.safe_load(render_compose(pip, target))["services"]["pip"]

    assert target.container == "steward-pip"
    assert target.path == "~/docker/warren/residents/pip"
    assert service["container_name"] == "steward-pip"
    assert service["image"] == DEFAULT_IMAGE
    assert service["command"] == ["sleep", "infinity"]
    assert memory_mount(pip.manifest) == (
        "~/docker/warren/residents/pip/memory",
        "/data/residents/pip/memory",
    )
