# ruff: noqa: PLC0414 — explicit aliases preserve named imports beyond historical __all__.
"""Where a resident actually lives, and the one seam steward reaches it through.

The nursery's provision stage (:mod:`steward.nursery`) needs to do four things to a
machine that is not this one: put files on it, run ``docker compose`` on it, read back
what is already there, and be completely absent during a rehearsal. This module is those
four things and nothing else — the pipeline, the git commits, and the scheduler check
live next door.

## The transport seam

:class:`Transport` is a protocol with four methods, and every one of them is something a
deploy genuinely does:

:class:`SshTransport`
    The real one. ``ssh <user>@<host> …`` for commands, and a **tar-over-ssh pipe** for
    files, because UGOS's ``scp`` is broken on the NAS and a pipe is what actually works
    (chronicle's README has been deploying this way by hand for months). The archive is
    built in memory and fed to ``tar -xf -`` on stdin, so nothing is staged on disk here
    and nothing is staged on disk there.
:class:`LocalTransport`
    A directory that plays the part of a host. It extracts the *same tar bytes* the ssh
    transport would pipe, records every command it was asked to run, and never starts a
    process. The whole pipeline is tested against it, which is why the test suite has no
    network in it and no ``docker`` on PATH.

Both are constructed by the caller and injected, so the API, the CLI, and the tests all
drive one implementation of provisioning.

## Everything external goes through runners

``ssh`` and ``tar`` are processes, and steward starts processes in exactly one file
(:mod:`steward.runners`) — a rule ``tests/test_runners.py`` enforces by reading the source
tree. So :class:`SshTransport` holds a :data:`steward.runners.PipedRun` and calls it, and
nothing in this module ever launches anything itself.

## Explicit placement

Host and SSH user come from resident deploy fields, then STEWARD_DEPLOY_HOST and
STEWARD_DEPLOY_USER. Missing placement refuses rather than guessing a personal machine.
Path, image and command retain their documented defaults. Dry runs print the resolved target.

The image is this repo's own: ``docker/resident/Dockerfile``, built by ``make image``,
carrying the ``claude`` CLI and a vendored copy of chronicle's hook emitter. Steward does not
build it during a deploy and does not check the host has it — provisioning ships a compose
file, and the host's docker is what says whether the image exists. ``make image-ship`` is
how it gets there.
"""

import io
import os
import tarfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import yaml

from steward.deployment_rules import (
    BURROW_ENV,
    BURROW_HOME_ENV,
    DEFAULT_COMMAND,
    DEFAULT_IMAGE,
    DEFAULT_RESIDENTS_ROOT,
    DEFAULT_ROOT,
    DeploymentSettings,
    burrow_home_for,
    container_zone,
    memory_path_for,
    resolve_mount_host_path,
)
from steward.deployment_rules import CONTAINER_PREFIX as CONTAINER_PREFIX
from steward.deployment_rules import FALLBACK_MEMORY_PATH as FALLBACK_MEMORY_PATH
from steward.manifest import MANIFEST_FILENAME, Resident, ResidentManifest
from steward.runners import (
    CHRONICLE_TOKEN_ENV,
    CHRONICLE_URL_ENV,
    COMMAND_TIMEOUT_S,
    LOCAL_PLACEMENT,
    TRANSFER_TIMEOUT_S,
    CommandOutcome,
    PipedRun,
    Placement,
    run_argv,
)

__all__ = [
    "BUNDLE_NAMES",
    "BURROW_ENV",
    "BURROW_HOME_ENV",
    "COMPOSE_FILENAME",
    "DEFAULT_COMMAND",
    "DEFAULT_IMAGE",
    "DEFAULT_RESIDENTS_ROOT",
    "DEFAULT_ROOT",
    "ENV_FILENAME",
    "SSH_FAILURE_STATUS",
    "BurrowTransport",
    "DeployTarget",
    "LocalTransport",
    "SshTransport",
    "Transport",
    "TransportError",
    "bundle_changes",
    "bundle_for",
    "compose_argv",
    "emitter_env",
    "memory_host_dir",
    "memory_mount",
    "memory_path_for",
    "pack",
    "placement_for",
    "planned_env",
    "render_argv",
    "render_compose",
    "render_env",
    "target_for",
    "transport_for",
]

COMPOSE_FILENAME = "docker-compose.yaml"
ENV_FILENAME = ".env"
SOUL_FILENAME = "soul.md"

#: The comment header every rendered compose file opens with — "do not edit this on the
#: host", "the secrets are in .env, not here". Read once, then prepended to the dict body
#: :func:`render_compose` serialises, because ``yaml.safe_dump`` cannot carry a comment.
COMPOSE_HEADER = (Path(__file__).parent / "templates" / "resident-compose.yaml").read_text(
    encoding="utf-8"
)

#: The two variables that carry the village's address and its shared secret into the
#: container are :data:`~steward.runners.CHRONICLE_URL_ENV` and
#: :data:`~steward.runners.CHRONICLE_TOKEN_ENV`, imported above rather than respelled here:
#: ``runners`` compares the same two names against what a *session* inherits, and two
#: spellings of one variable is how those answers drift apart. Read from steward's own
#: environment at provision time and written into the remote ``.env``; never into the
#: compose file, never into a manifest, never into git.
STEWARD_URL_ENV = "STEWARD_URL"


#: ssh's reserved exit status for "ssh itself failed" — connection refused, host
#: unreachable, auth denied, host-key mismatch. Every other status belongs to the remote
#: command, so this is the one value that tells the two apart from the near side.
SSH_FAILURE_STATUS = 255

#: SSH must fail instead of asking a daemon (or an operator's terminal) for credentials.
SSH_OPTIONS: tuple[str, ...] = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
)

#: Compose operations do real lifecycle work and are not control-plane queries. Pull is
#: longest because image size and registry speed dominate it; up may create containers;
#: down only stops and removes an existing project.
COMPOSE_TIMEOUTS: Mapping[str, float] = {"pull": 600.0, "up": 300.0, "down": 120.0}


class TransportError(Exception):
    """Raised when a transport cannot reach the host at all.

    Distinct from a command that ran and failed: that comes back as a
    :class:`~steward.runners.CommandOutcome` with a non-zero status, because "docker said
    no" is an answer. This is for "there was nobody to ask".
    """


# --------------------------------------------------------------------------------------
# the target
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeployTarget:
    """The resolved address of one resident's container: manifest first, defaults after."""

    resident_id: str
    host: str
    user: str
    path: str
    container: str
    image: str
    command: tuple[str, ...]

    @property
    def service(self) -> str:
        """The compose service name — the resident id, which is already a slug."""
        return self.resident_id

    @property
    def compose_path(self) -> str:
        """Where the rendered compose file lands on the host."""
        return str(PurePosixPath(self.path) / COMPOSE_FILENAME)

    @property
    def env_path(self) -> str:
        """Where the secrets land on the host, and the only place they land."""
        return str(PurePosixPath(self.path) / ENV_FILENAME)

    def describe(self) -> str:
        """One line naming where this resident runs, for a plan or a log."""
        return f"{self.container} on {self.user}@{self.host}:{self.path} ({self.image})"

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view. No secret has ever been in here."""
        return {
            "resident": self.resident_id,
            "host": self.host,
            "user": self.user,
            "path": self.path,
            "container": self.container,
            "image": self.image,
            "command": list(self.command),
        }


def target_for(
    manifest: ResidentManifest, settings: DeploymentSettings | None = None
) -> DeployTarget:
    """Resolve resident fields over a snapshot of configured installation defaults."""
    defaults = settings if settings is not None else DeploymentSettings.from_env()
    deploy = manifest.deploy
    container = deploy.container or f"{CONTAINER_PREFIX}{manifest.id}"
    return DeployTarget(
        resident_id=manifest.id,
        host=defaults.resolve_host(deploy.host),
        user=defaults.resolve_user(deploy.user),
        path=deploy.path or f"{DEFAULT_RESIDENTS_ROOT}/{manifest.id}",
        container=container,
        image=deploy.image or DEFAULT_IMAGE,
        command=tuple(deploy.command) or DEFAULT_COMMAND,
    )


def memory_mount(manifest: ResidentManifest) -> tuple[str, str]:
    """Return both sides of one resident's memory volume: ``(host path, container path)``.

    Before steward #58 ``memory.path`` quietly meant two things at once — the control
    plane's read path and the container's mount point — which only held because nothing
    ever stood on both sides of the boundary. Container placement does, so the mapping is
    said once, here, derived from data steward already holds: the host side is the
    ``memory/`` directory inside the resident's deploy directory (exactly what
    :func:`render_compose` mounts as ``./memory``), and the container side is
    :func:`memory_path_for` (exactly what it mounts that directory *at*).

    Every path steward touches then picks a side deliberately: journal reads and writes
    and skills materialization use the host side; the session's ``docker exec -w``
    working directory uses the container side.
    """
    target = target_for(manifest)
    return str(PurePosixPath(target.path) / "memory"), memory_path_for(manifest)


def memory_host_dir(manifest: ResidentManifest, env: Mapping[str, str] | None = None) -> Path:
    """Return the directory on *this* host that holds the resident's memory.

    The declared ``memory.path`` for a locally placed resident — the meaning it has
    always had — and the host side of :func:`memory_mount` for a container-placed one,
    where ``memory.path`` names the mount point inside the container instead.

    A leading ``~`` is the deploy user's home **on the burrow** when :data:`BURROW_HOME_ENV`
    names it, and this process's own home otherwise. The deployed control plane runs as
    root in a container whose ``$HOME`` is ``/root``, which is nobody's home on the host;
    with the variable set, the API, the scheduler and the watchdog all compute the path
    the host actually has — the one their compose file mounts at that same path — rather
    than three views of one directory.
    """
    source = os.environ if env is None else env
    if manifest.runner.container_placed:
        host, _ = memory_mount(manifest)
    else:
        host = manifest.memory.path
    home = (source.get(BURROW_HOME_ENV) or "").strip()
    if home and (host == "~" or host.startswith("~/")):
        return Path(home) / host[2:] if host != "~" else Path(home)
    return Path(host).expanduser()


def placement_for(manifest: ResidentManifest) -> Placement:
    """Resolve where this resident's sessions run, as the runner seam's value object.

    The one place a ``runner.placement`` declaration meets the ``deploy`` block's
    address. Local placement resolves to :data:`~steward.runners.LOCAL_PLACEMENT`;
    container placement carries the container's resolved name and the *container side*
    of the memory mount as the session's working directory. Validation refuses a
    container placement with no declared ``deploy.container``, so the ``target_for``
    default here is only ever the declared name normalized through one spelling.
    """
    if not manifest.runner.container_placed:
        return LOCAL_PLACEMENT
    target = target_for(manifest)
    _, container_workdir = memory_mount(manifest)
    return Placement(container=target.container, workdir=container_workdir)


# --------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------


def render_compose(
    resident: Resident,
    target: DeployTarget,
    env: Mapping[str, str] | None = None,
) -> str:
    """Render the resident's compose fragment as data, never as markup. Deterministic.

    The fragment is built as a Python dict and serialised with ``yaml.safe_dump``, so any
    value a manifest carries — ``memory.path``, the container command, the project label —
    is emitted as one quoted scalar rather than as text spliced into a template. A value
    holding a newline, a ``:`` or a ``privileged: true`` of its own can no longer reopen
    the document from inside itself: the manifest patterns (#61) are the first line of that
    defence and this rendering is the second, so a value that somehow slipped past the
    patterns still cannot become a compose key.

    Deterministic matters more than it sounds: the provision stage decides whether to
    write anything by comparing this string to what is already on the host, so a render
    that varied by run — a timestamp, a dict iteration order — would make every deploy look
    like a change and every "converged" claim a lie. ``sort_keys=True`` and a header with
    no clock in it keep it stable.

    ``${CHRONICLE_URL:?…}`` and ``${CHRONICLE_TOKEN-}`` are literal string *values* here: docker
    compose interpolates them from the ``.env`` beside the file, and steward never
    substitutes them itself.
    """
    memory_path = memory_path_for(resident.manifest)
    source = os.environ if env is None else env
    burrow_home = burrow_home_for(target.user, source)

    service: dict[str, Any] = {
        "image": target.image,
        "container_name": target.container,
        "restart": "unless-stopped",
        # Docker's own tiny init as PID 1, because the container is where sessions are
        # killed (steward #58): a timed-out session's processes are reparented to PID 1
        # when the group dies, and the default `sleep infinity` never reaps, so every
        # kill would leave zombies accumulating against the PID table for the life of
        # the container.
        "init": True,
        "working_dir": memory_path,
        "environment": {
            "CHRONICLE_AGENT_ID": resident.agent_id,
            "CHRONICLE_PROJECT": resident.project,
            "CHRONICLE_URL": ("${CHRONICLE_URL:?steward writes this into .env at provision time}"),
            "CHRONICLE_TOKEN": "${CHRONICLE_TOKEN-}",
            "STEWARD_URL": "${STEWARD_URL:?steward writes this into .env at provision time}",
            "STEWARD_RESIDENT": resident.id,
            # The resident image runs as root (no USER: the vault and key mounts are
            # root-owned), and the claude CLI refuses `--permission-mode
            # bypassPermissions` for root — "cannot be used with root/sudo privileges" —
            # unless IS_SANDBOX=1 says the process is already inside a sandbox. This
            # container is that sandbox: the mounts under deploy.mounts and the
            # workspace grant are the boundary, not the CLI's per-call prompt, which a
            # headless session could never answer anyway. Stated here for every
            # resident because it is true of every resident; it changes nothing for a
            # manifest that names no permission mode. Measured 2026-09-04 in
            # steward-hob against CLI 2.1.243 (warren#391).
            "IS_SANDBOX": "1",
            # The container's clock follows the routines' wall clock (warren#386), so a
            # session that stamps "today" from `date` names the same day the schedule
            # meant; without it the container is UTC and every skill has to spell the
            # zone out. deploy.tz settles it when the routines disagree.
            "TZ": container_zone(resident.manifest),
        },
        "volumes": [
            f"./memory:{memory_path}",
            "./claude:/root/.claude",
            *(
                f"{resolve_mount_host_path(mount.host, burrow_home)}:{mount.container}"
                + (":ro" if mount.mode == "ro" else "")
                for mount in resident.manifest.deploy.mounts
            ),
        ],
        # The name the burrow's own containers use for the machine they run on — the
        # control plane's CHRONICLE_URL is http://dockerhost:8737 — so the village address
        # steward copies into the resident's .env at provision time resolves in there too.
        # A LAN address in .env keeps working; a bare hostname never did.
        "extra_hosts": ["dockerhost:host-gateway"],
        "command": list(target.command),
    }
    document = {"services": {target.service: service}}
    body = yaml.safe_dump(document, default_flow_style=False, sort_keys=True)
    return COMPOSE_HEADER + body


def render_env(values: Mapping[str, str]) -> str:
    """Render the remote ``.env``: one ``KEY=value`` per line, sorted, nothing else.

    Sorted so the file is comparable across runs, and refusing a value with a newline in
    it because a ``.env`` has no escaping — a secret that smuggled a second line in would
    silently become a second variable.
    """
    lines = []
    for key in sorted(values):
        value = values[key]
        if "\n" in value or "\r" in value:
            raise TransportError(
                f"the value of {key} contains a line break, and a .env file has no way to "
                f"quote one; fix the variable in steward's environment"
            )
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n" if lines else ""


def emitter_env(source: Mapping[str, str]) -> dict[str, str]:
    """Read the village's address and secret out of steward's own environment.

    Refuses without a URL, and the refusal is the point: a container with no village to
    post to is a resident that will never appear in chronicle however healthy it is, and
    finding that out three days later from an empty house is worse than finding it out
    here. A missing token is *not* a refusal — chronicle's ingest is open when its own
    token is unset — but the plan says so out loud.
    """
    url = (source.get(CHRONICLE_URL_ENV) or "").strip()
    if not url:
        raise TransportError(
            f"{CHRONICLE_URL_ENV} is unset in steward's environment, so the resident would "
            f"be deployed with nowhere to emit and would never appear in the village; "
            f"set {CHRONICLE_URL_ENV} to this installation's Chronicle URL and run this again"
        )
    values = {CHRONICLE_URL_ENV: url}
    steward_url = (source.get(STEWARD_URL_ENV) or "").strip()
    if not steward_url:
        raise TransportError(
            f"{STEWARD_URL_ENV} is unset in steward's environment, so the resident's "
            "session credential would have no API address to present itself to"
        )
    values[STEWARD_URL_ENV] = steward_url
    token = (source.get(CHRONICLE_TOKEN_ENV) or "").strip()
    if token:
        values[CHRONICLE_TOKEN_ENV] = token
    return values


def planned_env(source: Mapping[str, str]) -> dict[str, str]:
    """Read the village variables a run *would* carry, refusing nothing.

    :func:`emitter_env` refuses when no URL is set, because a real deploy with nowhere to
    emit is a resident that never appears in the village. A **rehearsal** reaches no host,
    so it cannot deploy anything wrong — it must be able to assemble and print the plan
    whatever the emitter environment says (#84). This is the lenient reader the dry-run
    path uses: whatever is set, named; nothing raised.

    It names whatever it finds, so the rehearsal reports the keys the real run would
    actually write rather than half of them.
    """
    values: dict[str, str] = {}
    for key in (CHRONICLE_URL_ENV, CHRONICLE_TOKEN_ENV, STEWARD_URL_ENV):
        value = (source.get(key) or "").strip()
        if value:
            values[key] = value
    return values


def bundle_for(
    resident: Resident,
    target: DeployTarget,
    env: Mapping[str, str],
    *,
    host_env: Mapping[str, str] | None = None,
) -> dict[str, bytes]:
    """Build the resident's whole runtime bundle, as ``{name: bytes}``, in memory.

    Everything the container needs and nothing it does not: the compose fragment, the
    ``.env`` steward writes from its own environment, the manifest and soul so the machine
    carries the same declaration git does, and two empty directories for the volumes so
    docker does not create them as root.
    """
    files: dict[str, bytes] = {
        COMPOSE_FILENAME: render_compose(resident, target, host_env).encode("utf-8"),
        ENV_FILENAME: render_env(env).encode("utf-8"),
        MANIFEST_FILENAME: resident.path.read_bytes(),
        "memory/.keep": b"",
        "claude/.keep": b"",
    }
    soul_path = resident.directory / resident.manifest.soul.file
    if soul_path.is_file():
        files[SOUL_FILENAME] = soul_path.read_bytes()
    return files


#: Every file a resident's bundle has, in the order a plan lists them. Named as a constant
#: so ``--dry-run`` can print the list without reading a manifest off disk that a rehearsal
#: has deliberately not written.
BUNDLE_NAMES: tuple[str, ...] = (
    COMPOSE_FILENAME,
    ENV_FILENAME,
    MANIFEST_FILENAME,
    SOUL_FILENAME,
    "memory/.keep",
    "claude/.keep",
)


def bundle_changes(transport: Transport, files: Mapping[str, bytes], path: str) -> tuple[str, ...]:
    """Return the bundle files the host does not already have, byte for byte.

    This is what makes a second deploy a no-op rather than a re-upload: an empty tuple
    means the host is already exactly what the repo says it should be, and steward writes
    nothing. The two placeholder files are skipped — they exist to create directories, and
    an empty file is either there or it is not.
    """
    changed: list[str] = []
    for name in sorted(files):
        if name.endswith("/.keep"):
            continue
        remote = transport.read(str(PurePosixPath(path) / name))
        if remote is None or remote.encode("utf-8") != files[name]:
            changed.append(name)
    return tuple(changed)


def pack(files: Mapping[str, bytes]) -> bytes:
    """Pack a bundle into a deterministic uncompressed tar archive.

    Deterministic in every field a tar has an opinion about — sorted names, a fixed
    mtime, uid and gid zero — because the archive is a *fact about the bundle* and two
    identical bundles that produced two different archives would leave the nursery unable
    to say whether anything had changed.

    ``.env`` goes in at ``0600``. Everything else is ``0644``.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(files):
            payload = files[name]
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = 0o600 if name == ENV_FILENAME else 0o644
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def compose_argv(target: DeployTarget, *args: str) -> tuple[str, ...]:
    """Build a ``docker compose`` argv for this target, addressed by absolute path.

    Explicit ``-f`` and ``--project-directory`` rather than a ``cd``: there is no shell
    here to ``cd`` in, and naming both means the command works the same whatever
    directory the far side happens to drop into.
    """
    return (
        "docker",
        "compose",
        "-f",
        target.compose_path,
        "--project-directory",
        target.path,
        "-p",
        target.resident_id,
        *args,
    )


# --------------------------------------------------------------------------------------
# the seam
# --------------------------------------------------------------------------------------


class Transport(Protocol):
    """Something that can put files on a host and run commands there.

    Four methods, and :meth:`plan` is one of them on purpose: ``--dry-run`` has to print
    the exact argv a real run would use, and a plan that was assembled by a *different*
    piece of code from the one that runs it would be a plan that could quietly drift out
    of agreement with reality.
    """

    kind: str

    def describe(self) -> str:
        """One line naming where this transport goes."""
        ...

    def plan(self, argv: Sequence[str]) -> tuple[str, ...]:
        """Return the exact argv this transport would run for a remote command."""
        ...

    def run(self, argv: Sequence[str]) -> CommandOutcome:
        """Run one command on the host and report what happened."""
        ...

    def send(self, files: Mapping[str, bytes], path: str) -> CommandOutcome:
        """Materialize a bundle under ``path`` on the host."""
        ...

    def read(self, path: str) -> str | None:
        """Return the contents of a file on the host, or ``None`` when there is none."""
        ...

    def exists(self, path: str) -> bool:
        """Report whether a path is there, for a caller that does not want the bytes."""
        ...


@dataclass
class SshTransport:
    """The real transport: ssh for commands, a tar pipe for files.

    Everything external goes through :data:`steward.runners.run_argv`, which is injectable
    as ``command`` so this class can be exercised against a fake without an ssh anywhere.
    """

    host: str = field(default_factory=lambda: DeploymentSettings.from_env().resolve_host())
    user: str = field(default_factory=lambda: DeploymentSettings.from_env().resolve_user())
    ssh: str = "ssh"
    command: PipedRun = run_argv
    kind: str = "ssh"

    @property
    def target(self) -> str:
        """The ``user@host`` ssh is given."""
        return f"{self.user}@{self.host}"

    def describe(self) -> str:
        """Name the machine this transport reaches."""
        return f"ssh {self.target}"

    def plan(self, argv: Sequence[str]) -> tuple[str, ...]:
        """Return the ssh argv for a remote command, exactly as :meth:`run` would use it."""
        return (self.ssh, *SSH_OPTIONS, self.target, *(str(part) for part in argv))

    def run(self, argv: Sequence[str]) -> CommandOutcome:
        """Run one command over ssh, bounded according to the work it performs."""
        parts = tuple(str(part) for part in argv)
        timeout_s = self._timeout_for(parts)
        return self.command(self.plan(parts), timeout_s)

    @staticmethod
    def _timeout_for(argv: Sequence[str]) -> float:
        """Give compose lifecycle operations room without weakening short queries."""
        try:
            compose = argv.index("compose")
        except ValueError:
            return COMMAND_TIMEOUT_S
        if compose == 0 or argv[compose - 1] != "docker":
            return COMMAND_TIMEOUT_S
        # Find the compose subcommand, not an option value (a project may itself be named
        # ``pull``). These are the value-taking options steward's compose argv emits.
        value_options = {"-f", "--file", "--project-directory", "-p", "--project-name"}
        cursor = compose + 1
        while cursor < len(argv):
            part = argv[cursor]
            if part in value_options:
                cursor += 2
                continue
            if part.startswith("-"):
                cursor += 1
                continue
            return COMPOSE_TIMEOUTS.get(part, COMMAND_TIMEOUT_S)
        return COMMAND_TIMEOUT_S

    def send(self, files: Mapping[str, bytes], path: str) -> CommandOutcome:
        """Create ``path`` and unpack the bundle into it, through one tar-over-ssh pipe.

        Two commands, not one, because ``tar -xf -`` into a directory that does not exist
        fails with a message about tar rather than about the directory. The ``mkdir``
        failing is reported as itself.
        """
        made = self.run(["mkdir", "-p", path])
        if not made.ok:
            return made
        return self.command(
            self.plan(["tar", "-xf", "-", "-C", path]),
            TRANSFER_TIMEOUT_S,
            stdin=pack(files),
        )

    def read(self, path: str) -> str | None:
        """Read a file on the host, or return ``None`` when it is not there.

        ``None`` means one thing only: *the host answered, and the file is absent*. Every
        other failure raises :class:`TransportError`, which is the word this module
        already has for "there was nobody to ask".

        The distinction is load-bearing rather than tidy. ``run`` reports a missing ssh
        binary, a host that never answered, and an auth refusal all as non-``ok``
        outcomes, so folding them into ``None`` told a caller the file was not there —
        about a machine steward never reached. ``steward retire`` read the compose file
        to decide whether there was a container to stop, took an unreachable NAS for an
        empty directory, and reported the resident retired while its container kept
        running and kept spending (steward #136).
        """
        outcome = self.run(["cat", path])
        if outcome.ok:
            return outcome.stdout
        self._require_reached(outcome)
        # ssh connected and ``cat`` exited non-zero. Usually that is "no such file", but
        # ``cat`` answers 1 for an unreadable file too and does not distinguish the two in
        # its status. A caller that must not confuse "absent" with "there but unreadable"
        # asks :meth:`exists` — which is the question, and the only question,
        # ``_stop_retired_container`` was ever using this method for.
        return None

    def exists(self, path: str) -> bool:
        """Report whether a path is there. Raises rather than guessing when unreachable.

        ``test -e`` where :meth:`read` uses ``cat``, because an unreadable file still
        exists. A false answer is not sufficient on its own, though: ``test -e`` also
        returns false when an ancestor cannot be traversed. Walk upward to the nearest
        ancestor the remote shell can see and require it to be searchable before calling
        the original path absent. Retiring a resident must fail closed when it cannot tell
        "missing" from "forbidden", or it can leave a container running (steward #136).
        """
        candidate = PurePosixPath(path)
        original = candidate
        while True:
            outcome = self.run(["test", "-e", str(candidate)])
            if outcome.ok:
                if candidate == original:
                    return True
                searchable = self.run(["test", "-x", str(candidate)])
                if searchable.ok:
                    return False
                self._require_reached(searchable)
                raise TransportError(
                    f"{self.target}: cannot inspect {path}: ancestor {candidate} is not searchable"
                )
            self._require_reached(outcome)
            parent = candidate.parent
            if parent == candidate:
                raise TransportError(f"{self.target}: cannot inspect {path}")
            candidate = parent

    def _require_reached(self, outcome: CommandOutcome) -> None:
        """Raise unless the far side actually ran the command and answered for itself.

        The shared half of :meth:`read` and :meth:`exists`: every way a command can fail
        *without having run* — no ssh binary, a host that never answered, ssh refusing the
        connection — is a :class:`TransportError`, because none of them say anything about
        what is on the host.
        """
        if outcome.error is not None:
            # The command never ran at all: no ssh binary, or it hung until steward gave
            # up on it. Either way this says nothing about what is on the far side.
            raise TransportError(f"{self.target}: {outcome.error}")
        if outcome.exit_status == SSH_FAILURE_STATUS:
            # ssh's own reserved status — refused, timed out, bad host key, no such user.
            # The far side never got as far as running anything.
            raise TransportError(
                f"{self.target}: ssh could not open the connection ({outcome.summary()})"
            )


@dataclass
class BurrowTransport:
    """The burrow provisioning its own residents: files through a mount, docker through the socket.

    The deployed control plane runs *on* the burrow, and :class:`SshTransport` from inside it
    would have to ssh back to its own host — a container with no key for that, no host entry,
    and a hostname it cannot even resolve (warren#356). So when a resident's ``deploy.host``
    is this burrow (:data:`BURROW_ENV`), the bundle is written straight into the residents
    directory, which the compose file mounts into the API, and ``docker compose up -d`` runs
    in this process against the socket the same file mounts. No ssh, no tar pipe, and the
    New resident form in townhall reaches the container it promised.

    ``home`` is the deploy user's home **on the host** (:data:`BURROW_HOME_ENV`), never this
    process's ``$HOME``. Every remote path steward renders starts with ``~/``; here it is
    resolved to a host path before anything runs, because the compose CLI in this process
    resolves the rendered file's relative binds (``./memory``) against the project directory
    and hands the daemon *host* paths — so the residents directory must be mounted at the
    same path in here as it has on the host. ``deploy/compose.yaml`` pins exactly that, and
    :meth:`plan` shows the resolved argv, which is the argv that runs.

    Files land owned by whoever this process runs as (root, in the image). Reads and
    existence checks fail closed the way :class:`SshTransport`'s do: a path this process is
    not allowed to look at is a :class:`TransportError`, never "absent" (steward #136).
    """

    burrow: str
    user: str = field(default_factory=lambda: DeploymentSettings.from_env().resolve_user())
    home: str = ""
    command: PipedRun = run_argv
    kind: str = "burrow"

    def __post_init__(self) -> None:
        """Default ``home`` to the deploy user's, the way the NAS lays its users out."""
        if not self.home:
            self.home = f"/home/{self.user}"

    def describe(self) -> str:
        """Name the machine: this one."""
        return f"{self.burrow} itself, through its own docker"

    def resolve(self, path: str) -> str:
        """Turn a rendered ``~/…`` path into the host path it means here."""
        if path == "~":
            return self.home
        if path.startswith("~/"):
            return f"{self.home}/{path[2:]}"
        return path

    def plan(self, argv: Sequence[str]) -> tuple[str, ...]:
        """Return the argv that runs: no prefix, every ``~/`` already a host path."""
        return tuple(self.resolve(str(part)) for part in argv)

    def run(self, argv: Sequence[str]) -> CommandOutcome:
        """Run one command here, bounded the way the same command over ssh would be."""
        parts = self.plan(argv)
        return self.command(parts, SshTransport._timeout_for(parts))  # noqa: SLF001 — one timeout table

    def send(self, files: Mapping[str, bytes], path: str) -> CommandOutcome:
        """Unpack the bundle into the residents directory, from the same tar ssh would pipe."""
        destination = Path(self.resolve(path))
        argv = ("tar", "-xf", "-", "-C", str(destination))
        try:
            destination.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(pack(files)), mode="r") as archive:
                archive.extractall(destination, filter="data")
        except OSError as exc:
            return CommandOutcome(argv=argv, exit_status=1, stderr=f"{destination}: {exc}")
        return CommandOutcome(argv=argv, exit_status=0)

    def read(self, path: str) -> str | None:
        """Read a file here, or ``None`` when there is no such file."""
        target = Path(self.resolve(path))
        try:
            return target.read_text(encoding="utf-8")
        except FileNotFoundError, NotADirectoryError, IsADirectoryError:
            return None
        except OSError as exc:
            raise TransportError(f"{self.burrow}: cannot read {path}: {exc}") from exc

    def exists(self, path: str) -> bool:
        """Report whether a path is here; a path this process may not inspect is an error."""
        # ``os.stat`` rather than ``Path.exists``, which since Python 3.13 answers False to
        # *every* OSError — a directory this process may not enter would read as empty.
        target = Path(self.resolve(path))
        try:
            target.stat()
        except FileNotFoundError, NotADirectoryError:
            return False
        except OSError as exc:
            raise TransportError(f"{self.burrow}: cannot inspect {path}: {exc}") from exc
        return True


@dataclass
class LocalTransport:
    """A directory that plays a host, for tests and rehearsals. Starts no processes.

    ``root`` stands in for ``/``: a remote path of ``~/docker/warren/residents/quill``
    lands at ``root/docker/warren/residents/quill``, which keeps the fake host's shape
    recognisable when a test prints it. Files arrive by unpacking the same tar bytes
    :class:`SshTransport` would have piped, so the archive is genuinely exercised.

    Every command is recorded rather than run, which is what makes "a dry run touched
    nothing" an assertion instead of a promise. ``unreachable`` makes every operation
    raise, which is how the unreachable-NAS test happens without a NAS.
    """

    root: Path
    kind: str = "local"
    calls: list[tuple[str, ...]] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    unreachable: bool = False
    #: Commands whose argv contains this string fail with a non-zero status, so a test can
    #: make ``docker compose up`` refuse without making the whole host disappear.
    fail_on: str | None = None

    @property
    def touched(self) -> bool:
        """True when this transport has been asked to do anything at all."""
        return bool(self.calls or self.sent)

    def describe(self) -> str:
        """Name the directory standing in for a host."""
        return f"local {self.root}"

    def plan(self, argv: Sequence[str]) -> tuple[str, ...]:
        """Return the argv a real transport would run — the same shape, unprefixed."""
        return tuple(str(part) for part in argv)

    def resolve(self, path: str) -> Path:
        """Map a remote path onto the fake host's tree."""
        stripped = path.removeprefix("~/").removeprefix("/")
        return self.root / stripped

    def _reachable(self) -> None:
        if self.unreachable:
            raise TransportError(f"no route to {self.root}")

    def run(self, argv: Sequence[str]) -> CommandOutcome:
        """Record a command and answer as though it succeeded."""
        self._reachable()
        parts = self.plan(argv)
        self.calls.append(parts)
        if self.fail_on is not None and any(self.fail_on in part for part in parts):
            return CommandOutcome(argv=parts, exit_status=1, stderr=f"{self.fail_on} refused")
        return CommandOutcome(argv=parts, exit_status=0)

    def send(self, files: Mapping[str, bytes], path: str) -> CommandOutcome:
        """Unpack the bundle's tar into the fake host, exactly as the far side would."""
        self._reachable()
        destination = self.resolve(path)
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(pack(files)), mode="r") as archive:
            archive.extractall(destination, filter="data")
        self.sent.append(path)
        return CommandOutcome(argv=("tar", "-xf", "-", "-C", path), exit_status=0)

    def read(self, path: str) -> str | None:
        """Read a file out of the fake host."""
        self._reachable()
        target = self.resolve(path)
        return target.read_text(encoding="utf-8") if target.is_file() else None

    def exists(self, path: str) -> bool:
        """Report whether the fake host has this path."""
        self._reachable()
        return self.resolve(path).exists()


def transport_for(target: DeployTarget, env: Mapping[str, str] | None = None) -> Transport:
    """Build the real transport for a resolved target.

    ssh to the target's host — unless the target's host *is* this burrow, named by
    :data:`BURROW_ENV` in ``env`` (steward's own environment when none is given), in which
    case the burrow provisions its own resident through :class:`BurrowTransport`. A laptop
    never sets the variable, so from a laptop every host is reached over ssh, the NAS
    included; the deployed control plane sets it, so from there the NAS is *here*.
    """
    source = os.environ if env is None else env
    burrow = (source.get(BURROW_ENV) or "").strip()
    if burrow and burrow == target.host:
        home = (source.get(BURROW_HOME_ENV) or "").strip()
        return BurrowTransport(burrow=burrow, user=target.user, home=home)
    return SshTransport(host=target.host, user=target.user)


def render_argv(argv: Iterable[str]) -> str:
    """Render an argv for a human reading a plan. Never re-parsed, never executed."""
    return " ".join(str(part) for part in argv)
