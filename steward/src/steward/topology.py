"""Which burrow steward's daemons run on, and what it costs when the answer is wrong.

A warren is one control plane and many **burrows** — machines that host residents. The
control plane is deliberately singular (one ``steward.db``, because the board claim and
the approval decision are conditional ``UPDATE``s and therefore *are* the inter-resident
medium), but supervision is not a database question: :class:`~steward.watchdog.
DockerSupervisor` shells out to ``docker`` and :meth:`steward.runners._ProcessRunner.
_run_in_container` shells out to ``docker exec``, and both of those reach whatever docker
daemon *this process's environment* points at.

So the intended topology is one sentence, and steward #59 is what it costs to leave it
unsaid: **the scheduler and the watchdog run on the burrow whose containers they
supervise.** For today's fleet that is the NAS. Run them anywhere else and the watchdog
asks a docker that has never heard of ``life-agent``, gets "no such container", and
reports the resident as *unsupervised* — quietly, forever, because "docker could not
answer" is indistinguishable from "there is nothing here to answer about".

This module is the part of that sentence steward can check and enforce. ``deploy.host``
partitions the shipped resident tree: scheduler and watchdog act only for residents on
this burrow, while :command:`steward doctor` reports the same placement. It also answers
whether the docker this process reaches holds those containers, so a missing provisioned
container is a named refusal rather than an endless, ambiguous *unsupervised* reading.

Three inputs, and they are asked in order of how much they are worth:

1. **What docker says about itself.** ``docker info`` names the daemon on the other end.
   That is a *measurement* of which machine is being supervised, and it beats every guess
   below — including on a NAS whose ``hostname`` is not its tailnet name.
2. **What the operator declared.** ``STEWARD_BURROW`` names this burrow when the machine's
   own hostname is not what manifests call it.
3. **The hostname**, as the fallback nobody has to configure.

``DOCKER_HOST`` is the fourth thing, and it is deliberately *not* a way to answer the
question. Steward's docker invocations do inherit it (measured — :func:`steward.runners.
run_argv` passes the parent environment straight through), so it genuinely moves
supervision to another machine's docker. What it cannot move is the half of a
container-placed session that happens on the control plane's own filesystem: skills are
materialized into, and the journal read from, the *host side* of the resident's memory
mount (:func:`steward.deploy.memory_host_dir`), which :func:`steward.sessions.
workdir_refusal` requires to be a directory on this host. A remote ``DOCKER_HOST`` is
therefore a supported pointer for *supervision* and not for *execution*, and steward says
"unverified" rather than "fine" about it: nothing here can prove that endpoint is the
declared host's docker.
"""

import os
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from steward.deploy import BURROW_ENV, target_for
from steward.manifest import Resident, ResidentManifest
from steward.runners import CommandRun, run_argv

__all__ = [
    "BURROW_ENV",
    "DAEMON_FORMAT",
    "DOCKER_HOST_ENV",
    "LOOPBACK_NAMES",
    "NOT_ASKED",
    "NO_SERVER",
    "Daemon",
    "Note",
    "Reach",
    "Supervision",
    "Survey",
    "burrow_names",
    "docker_endpoint",
    "residents_on_this_burrow",
    "supervises",
    "survey",
    "this_burrow",
]

#: What this burrow is called, when the machine's own hostname is not the name manifests
#: use for it. A ``deploy.host`` is matched against this before anything else the operator
#: did not write down, because the NAS answers to ``dxp2800`` on the tailnet whatever
#: ``hostname`` happens to return locally. Defined in ``deploy.py``, which also reads it to
#: decide that a resident of this burrow is provisioned here rather than over ssh.

#: Docker's own pointer. Steward never sets it and never reads it to *decide* anything —
#: it is read here only so a report can say where the docker calls are going.
DOCKER_HOST_ENV = "DOCKER_HOST"

#: Names that mean "the machine this process is on" whoever wrote them.
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})

#: The one docker question this module asks, and it asks it once per survey rather than
#: once per resident: the daemon's own name and version. ``--format`` with a tab, so a
#: daemon name containing a space (docker allows one) still parses.
DAEMON_FORMAT = "{{.Name}}\t{{.ServerVersion}}"

#: What stands in for a daemon reading when nothing declared a container. Not an empty
#: :class:`Daemon`, which would read as "a daemon answered and would not say its name" —
#: steward did not ask, and the difference is the whole point of this module.
NOT_ASKED = "docker was not asked: no resident here declares a container"

#: Why an exit status of zero is not enough to say a daemon answered.
#:
#: Measured against docker 27.3.1: with ``DOCKER_HOST`` pointing at nothing,
#: ``docker info`` prints the *client's* half of the report, writes "Cannot connect to the
#: Docker daemon at …" to stderr, and **exits 0** — while ``docker version --format
#: '{{.Server.Version}}'`` exits 1 on the same endpoint. A status-only check therefore
#: reports a client talking to itself as a healthy daemon, which is precisely the false
#: "everything is fine" this module exists to stop telling.
#:
#: ``docker version`` would be the cleaner reachability probe and does not carry
#: ``.Name`` — and the daemon's own name is the one *measured* signal this module is built
#: on, worth more than any hostname. So the probe stays ``info`` and the server fields
#: being filled in is what "answered" means.
NO_SERVER = "docker exited 0 but reported no server version, so no daemon answered there"


def _source(env: Mapping[str, str] | None) -> Mapping[str, str]:
    """Return the environment to read: the caller's, or this process's own."""
    return os.environ if env is None else env


def _declared(env: Mapping[str, str] | None = None) -> str:
    """Return the burrow name the operator wrote down, or ``""`` when they did not."""
    return (_source(env).get(BURROW_ENV) or "").strip()


def this_burrow(env: Mapping[str, str] | None = None) -> str:
    """Return what this machine is called, for a line a human reads.

    The declaration wins over the hostname, because the hostname is what the machine
    calls itself and ``deploy.host`` is what the fleet calls it, and only one of those is
    in a manifest.
    """
    return _declared(env) or socket.gethostname()


def burrow_names(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """Return every name, case-folded, that means "this burrow".

    A declared :data:`BURROW_ENV` *replaces* the hostname rather than joining it: an
    operator who has to name their burrow is telling steward the hostname is the wrong
    answer, and keeping it would leave no way to say so.
    """
    declared = _declared(env)
    if declared:
        candidates = {declared}
    else:
        hostname = socket.gethostname()
        # The first label too: a machine whose FQDN is `dxp2800.tail1234.ts.net` is the
        # `dxp2800` a manifest names, and neither spelling is more correct than the other.
        candidates = {hostname, hostname.partition(".")[0]}
    return frozenset(name.casefold() for name in candidates if name) | LOOPBACK_NAMES


def docker_endpoint(env: Mapping[str, str] | None = None) -> str | None:
    """Return the ``DOCKER_HOST`` this process's docker calls will use, or ``None``.

    ``None`` means the default: this machine's own docker socket. It is a separate value
    from the empty string on purpose — ``DOCKER_HOST=`` set to nothing is the default too,
    and reporting it as a remote pointer would be a fiction.
    """
    return (_source(env).get(DOCKER_HOST_ENV) or "").strip() or None


def supervises(manifest: ResidentManifest) -> bool:
    """Report whether this manifest names a container for steward to reach.

    Exactly the question :meth:`steward.watchdog.DockerSupervisor.owns` asks, and
    deliberately no wider: a **declared** ``deploy.container``, never the name the nursery
    would invent. ``runner.placement: container`` is not a second way in, because
    validation already refuses it without a declared container
    (:func:`steward.manifest._check_placement`) — and a manifest that somehow reached here
    without one would be reported against the *defaulted* ``steward-<id>``, a container
    the supervisor itself disowns. This report must never name something the watchdog
    would not even try.
    """
    return manifest.deploy.container is not None


def residents_on_this_burrow(
    residents: Sequence[Resident], env: Mapping[str, str] | None = None
) -> tuple[Resident, ...]:
    """Return active residents whose resolved ``deploy.host`` names this burrow.

    This is the fleet partition used by both daemons and by ``steward doctor``.  The
    deploy target is resolved through the same defaults provisioning uses, so a nursery
    manifest that omits ``deploy.host`` still lands in exactly one partition.
    """
    names = burrow_names(env)
    return tuple(
        resident
        for resident in residents
        if not resident.retired and target_for(resident.manifest).host.casefold() in names
    )


class Reach(StrEnum):
    """Which machine's docker holds a resident's container, as far as steward can tell.

    Note what this is *not*: whether anything is actually running. :attr:`HERE` says the
    docker these calls go to is the one holding this container, which is the prior
    question — :meth:`steward.watchdog.DockerSupervisor.health` still has to ask whether
    the container is up.
    """

    #: The daemon named itself as this container's declared host, or — with no
    #: ``DOCKER_HOST`` in play — this burrow answers to that name. Supervision reaches it.
    HERE = "here"
    #: The container is declared on another host and docker calls stay on this machine.
    #: Supervision does not reach it, and nothing at run time will say so.
    ELSEWHERE = "elsewhere"
    #: ``DOCKER_HOST`` sends docker calls off this machine and the daemon did not name
    #: itself as the declared host. They may still arrive at that host's docker; steward
    #: cannot prove it either way and will not guess.
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class Daemon:
    """What the docker on the other end of this endpoint said about itself."""

    name: str = ""
    version: str = ""
    #: Steward's own one-line reason the daemon did not answer, or ``None``. Never the
    #: command's own output — :meth:`steward.runners.CommandOutcome.summary` decides that,
    #: for the same reason :meth:`steward.runners._ProcessRunner._check_container` uses it.
    complaint: str | None = None

    @property
    def answered(self) -> bool:
        """True when a docker daemon actually replied."""
        return self.complaint is None

    def describe(self) -> str:
        """One phrase naming the daemon, for the line that says what is supervising."""
        if not self.answered:
            return "no answer"
        named = self.name or "an unnamed daemon"
        return f"{named} {self.version}".strip()


@dataclass(frozen=True, slots=True)
class Supervision:
    """One resident's container, where it is declared, and which docker holds it.

    Deliberately without a ``reachable`` shorthand. Whether supervision reaches this
    container is *two* facts — the right machine (:attr:`reach`) and a docker that
    answered (:attr:`Survey.daemon`) — and only :class:`Survey` holds both. A property
    here could read only the first and would have called a container on the right burrow
    "reachable" while no daemon answered for it at all.
    """

    resident_id: str
    container: str
    host: str
    reach: Reach


@dataclass(frozen=True, slots=True)
class Note:
    """One line of a topology report, and whether it is a line to worry about."""

    text: str
    ok: bool = False


@dataclass(frozen=True, slots=True)
class Survey:
    """What this process can and cannot supervise, and why. Nothing here is a guess."""

    burrow: str
    endpoint: str | None
    daemon: Daemon
    supervised: tuple[Supervision, ...] = ()

    @property
    def where(self) -> str:
        """Name the docker these calls go to, for any line that has to say it."""
        return (
            f"{DOCKER_HOST_ENV}={self.endpoint}"
            if self.endpoint is not None
            else f"{self.burrow}'s own docker"
        )

    @property
    def unreachable(self) -> tuple[Supervision, ...]:
        """Every container this process is supposed to supervise and cannot.

        A daemon that did not answer puts *all* of them here, whatever machine they are
        declared on: reach is about which docker holds a container, and no docker holding
        it means no supervision anywhere. Being on the right burrow is not supervision on
        its own.
        """
        if not self.daemon.answered:
            return self.supervised
        return tuple(item for item in self.supervised if item.reach is not Reach.HERE)

    def notes(self) -> tuple[Note, ...]:
        """Return the report, one line at a time, worst kind of line first.

        The docker line comes first because it decides how much the rest is worth: a
        daemon that did not answer makes every reachability answer below it provisional,
        and saying that after four green lines would be saying it too late.
        """
        if not self.supervised:
            return (
                Note(
                    "topology: no resident declares a container, so nothing here needs docker",
                    ok=True,
                ),
            )
        return (self._docker_note(), *(self._reach_note(item) for item in self.supervised))

    def _docker_note(self) -> Note:
        """Say whether docker answered at all, and name what is unsupervised if it did not."""
        if self.daemon.answered:
            return Note(
                f"topology: docker at {self.where} answers as {self.daemon.describe()}",
                ok=True,
            )
        orphaned = ", ".join(item.resident_id for item in self.unreachable)
        return Note(
            f"topology: docker did not answer at {self.where} ({self.daemon.complaint}); "
            f"the watchdog restarts containers by shelling out to it, so nothing is "
            f"supervising {orphaned}"
        )

    def _reach_note(self, item: Supervision) -> Note:
        """Say whether one declared container is one this process can reach."""
        if item.reach is Reach.HERE:
            if not self.daemon.answered:
                # The right machine and no docker on it is still nothing supervising this
                # container, and a green "supervised from here" under a red "docker did
                # not answer" would be the report contradicting itself in two lines.
                return Note(
                    f"{item.resident_id}: container {item.container} on {item.host} — "
                    f"this is the right burrow, but no docker here answered for it"
                )
            return Note(
                f"{item.resident_id}: container {item.container} on {item.host} — "
                f"supervised from here",
                ok=True,
            )
        if item.reach is Reach.UNVERIFIED:
            return Note(
                f"{item.resident_id}: container {item.container} is declared on {item.host}, "
                f"and {DOCKER_HOST_ENV}={self.endpoint} sends docker calls off this machine; "
                f"steward cannot verify that endpoint is {item.host}'s docker"
            )
        return Note(
            f"{item.resident_id}: container {item.container} runs on {item.host}, but this "
            f"process supervises through {self.where} — the watchdog cannot see it. Run "
            f"steward's daemons on {item.host} (the intended topology, docs/topology.md), "
            f"or point {DOCKER_HOST_ENV} at that machine's docker"
        )


def _ask_docker(command: CommandRun, binary: str = "docker") -> Daemon:
    """Ask the docker this process reaches what it is. One call, whatever else follows."""
    outcome = command([binary, "info", "--format", DAEMON_FORMAT])
    if not outcome.ok:
        return Daemon(complaint=outcome.summary())
    # Partition before stripping: a daemon that will not name itself answers with a
    # leading tab, and stripping first would slide the version into the name field.
    name, _, version = outcome.stdout.partition("\t")
    if not version.strip():
        return Daemon(complaint=NO_SERVER)
    return Daemon(name=name.strip(), version=version.strip())


def _reach(host: str, *, names: frozenset[str], endpoint: str | None, daemon: Daemon) -> Reach:
    """Decide whether the docker this process reaches is the one holding ``host``.

    Three signals, and the order is the whole point — each one is only consulted because
    the one above it could not answer:

    1. **What the daemon called itself.** A measurement of which machine is on the other
       end of these calls, and the only signal that survives a ``DOCKER_HOST``.
    2. **Whether ``DOCKER_HOST`` is set at all.** If it is and the daemon did not name
       itself as this host, the calls are leaving this machine for somewhere steward
       cannot identify — and what *this* machine is called says nothing about where they
       land. Answering from the local name here would be the module's own false "fine":
       a watchdog on the NAS with ``DOCKER_HOST`` pointed elsewhere would read as
       "supervised from here" while supervising nothing on it.
    3. **What this burrow is called.** Only now, with the calls known to be local, does
       the operator's name for the machine decide.
    """
    folded = host.casefold()
    if daemon.name and folded == daemon.name.casefold():
        return Reach.HERE
    if endpoint is not None:
        return Reach.UNVERIFIED
    return Reach.HERE if folded in names else Reach.ELSEWHERE


def survey(
    residents: Sequence[Resident],
    *,
    env: Mapping[str, str] | None = None,
    command: CommandRun = run_argv,
) -> Survey:
    """Answer, for these residents, whether this process's docker holds their containers.

    Docker is asked exactly once, and only when some resident actually declares a
    container: a fleet whose residents are all locally placed has nothing for a docker
    probe to be about, and a survey that shelled out anyway would make every
    :command:`steward doctor` on a laptop wait on a daemon it does not need.

    Retired residents are left out for the reason the watchdog leaves them out
    (:meth:`steward.watchdog.Watchdog.from_path`): ``steward retire`` stops and removes
    the container, so a retired resident's container is *supposed* to be missing and
    reporting it as unsupervised would be noise on top of a completed lifecycle.
    """
    source = os.environ if env is None else env
    names = burrow_names(source)
    endpoint = docker_endpoint(source)
    targets = [
        target_for(resident.manifest)
        for resident in residents
        if not resident.retired and supervises(resident.manifest)
    ]
    daemon = _ask_docker(command) if targets else Daemon(complaint=NOT_ASKED)
    return Survey(
        burrow=this_burrow(source),
        endpoint=endpoint,
        daemon=daemon,
        supervised=tuple(
            Supervision(
                resident_id=target.resident_id,
                container=target.container,
                host=target.host,
                reach=_reach(target.host, names=names, endpoint=endpoint, daemon=daemon),
            )
            for target in targets
        ),
    )
