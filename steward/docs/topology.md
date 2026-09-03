# Topology: where steward's daemons run (v0)

One control plane, one `steward.db`, many burrows — and **the daemons run on the burrow
whose containers they supervise.** For today's fleet that is the NAS (`dxp2800`).

That is the whole rule. The rest of this document is why it is the rule, what breaks when
it is violated, how steward now says so out loud, and the one alternative
(`DOCKER_HOST`) with an honest account of how far it actually goes.

## The rule, in three parts

**One control plane.** There is exactly one `steward.db` for the whole warren. The board
claim (`UPDATE … WHERE status = 'open'`) and the approval decision (`UPDATE … WHERE
status = 'pending'`) are conditional writes against that one database, and those writes
*are* the medium residents communicate through. A second steward with its own database is
not a bigger fleet, it is two fleets: no shared board, no cross-machine delegation, no
rolled-up budgets, no `steward task lineage` across a hop. So "run a steward per machine"
is not on the table.

**Many burrows.** A burrow is a machine that hosts residents. Provisioning has been
multi-host since the nursery landed: `deploy.host` / `user` / `path` / `container` are
per-resident, and `SshTransport` pushes the bundle as a tar over ssh. Nothing about
*declaring* a resident cares where it lives.

**The daemons live with the docker they drive.** `steward scheduler run` and `steward
watchdog run` are where the fleet stops being declarative. Both reach containers by
shelling out to a local `docker` client:

| what | where | what it runs |
|---|---|---|
| watchdog liveness | `DockerSupervisor.health` | `docker inspect --format {{.State.Running}} <container>` |
| watchdog restart | `DockerSupervisor.restart` | `docker restart <container>` |
| container-placed session | `_ProcessRunner._run_in_container` | `docker exec -w … -e … <container> sh -c …` |
| container-placed timeout kill | `_ProcessRunner._kill_in_container` | `docker exec <container> sh -c 'kill -9 -$pid'` |
| doctor / startup probe | `_ProcessRunner._check_container` | `docker inspect`, `docker exec … command -v` |

Every one of those talks to whatever docker daemon *that process's environment* points at.
None of them knows about `deploy.host`. So the daemons have to be where the containers
are.

**A third daemon, on the same rule for a narrower reason.** `steward chat run`
([docs/chat.md](chat.md)) is a separate process sharing the same state directory and the
same `steward.db` — that sharing is what the cross-process session claim (warren#111) is
for. It supervises nothing and touches no docker socket of its own, so a fleet of
locally-placed residents can run it anywhere the database is. But it *fires sessions*, and
a session placed in a container is launched by `docker exec` like any other: a chat route on
a container-placed resident puts this daemon under the same rule as the other two, needing a
`docker` binary and access to that machine's docker. Pip, the first chat resident, is
locally placed, so today the rule binds it only by way of the database it shares.

## What breaks when it is violated

Run the watchdog somewhere else and nothing errors. That is the problem.

`docker inspect life-agent` on a machine that has never held that container exits
non-zero, and `DockerSupervisor.health` correctly refuses to guess: it returns
`known=False` with `docker could not answer`. The watchdog then reports the resident as
**unsupervised** and does not restart it — which is right, given what it was able to see,
and indistinguishable from the resident having no container at all. A dead `life-agent`
stays dead, and every pass says the same calm thing.

The same silence has a second half. `steward watchdog` on the wrong machine still buries
stale runs, still sweeps expired leases and approvals, still trips budgets — all of which
work perfectly, because none of them needs docker. So the daemon looks healthy, its output
looks normal, and the one job that needed a container is the one job nobody is doing.

The scheduler's half of the rule was already loud, which is worth being precise about,
because it is why only one daemon needed a new report. Container placement
(`runner.placement: container`) fails at a moment somebody is watching: `steward doctor`
and the scheduler's own startup check both run `check_runner`, which for a container
placement probes the container instead of the local `PATH` (`_check_container`), and a
container the local docker cannot see is a **refusal before anything fires**. Supervision
had no equivalent — no refusal, no complaint, just a calm `unsupervised` forever — which is
what steward#59 was filed about.

## "On the burrow" is about docker, not about the machine

Steward is itself deployed as a container on the NAS (`~/docker/warren/steward`, `:8802` →
container `8801`). Being on the right *machine* is therefore necessary and not sufficient:
a process inside a container has no docker client and no docker socket unless it was given
one. A daemon in that position reaches nothing, and reaches it silently, in exactly the way
this document is about.

So a burrow's daemons need, in whatever process they run:

- a `docker` binary on `PATH`, and
- access to that machine's docker — `/var/run/docker.sock` bind-mounted, or a `DOCKER_HOST`
  pointing at it.

The topology report answers this without any of it having to be remembered: a daemon that
cannot reach a docker at all gets `topology: docker did not answer …`, naming every
resident left unsupervised. Which is the same line a missing socket produces as a missing
daemon — because from steward's side they are the same failure.

## What steward now says

`steward doctor` and `steward watchdog` both print a topology report
(`steward/src/steward/topology.py`) — the watchdog at startup, *before* its first pass, so
the "unsupervised" lines that follow are already explained; doctor down with its other
fleet-wide lines (`watchdog:`, `scheduler:`), because it is a fleet-wide fact and not a
per-resident one. It asks `docker info` **once**, and only when some resident actually
declares a `deploy.container` — a fleet of locally-placed residents never waits on a daemon
it does not need:

```console
$ steward watchdog tick              # on the NAS
topology: docker at dxp2800's own docker answers as dxp2800 27.3.1
life-agent: container life-agent on dxp2800 — supervised from here

$ steward watchdog tick              # on a laptop (real output)
topology: docker at Mihas-MacBook-Pro.local's own docker answers as docker-desktop 27.3.1
life-agent: container life-agent runs on dxp2800, but this process supervises through
  Mihas-MacBook-Pro.local's own docker — the watchdog cannot see it. Run steward's daemons
  on dxp2800 (the intended topology, docs/topology.md), or point DOCKER_HOST at that
  machine's docker
life-agent: unsupervised — steward's own state shows nothing stuck, which is not the same
  as up; docker could not answer for 'life-agent': exit status 1
nothing to intervene in
```

The third line is what the watchdog said before this change, on its own. The two above it
are why.

Three things decide "is this container's host the machine I am on", and each is consulted
only because the one above it could not answer:

1. **What docker says about itself.** `docker info --format {{.Name}}` names the daemon on
   the other end. That is a measurement rather than a guess, and it settles the case a
   hostname cannot: a NAS whose `hostname` is not its tailnet name would otherwise report
   its own containers as unreachable, which is the more damaging direction to be wrong in.
   It is also the only one of the three that survives a `DOCKER_HOST`.
2. **Whether `DOCKER_HOST` is set at all.** If it is, and the daemon did not name itself as
   this container's host, the calls are leaving this machine for somewhere steward cannot
   identify — and what *this* machine is called says nothing about where they land. Falling
   through to the local name here would be this report's own false "fine": a watchdog **on
   the NAS** with `DOCKER_HOST` pointed elsewhere would read as "supervised from here"
   while supervising nothing on it.
3. **What this burrow is called** — `STEWARD_BURROW`, else the hostname and its first
   label. Only now, with the calls known to be local, does the operator's name for the
   machine decide. A `STEWARD_BURROW` *replaces* the hostname rather than joining it: an
   operator who has to name their burrow is saying the hostname is the wrong answer.

The report also refuses to contradict itself. Reach answers *which machine's docker holds
this container*; whether any docker answered is a separate fact, and a container on the
right burrow with no daemon behind it is reported as exactly that — "the right burrow, but
no docker here answered for it" — never as supervised.

One measured detail about that probe, because it caught this implementation out: **`docker
info` exits 0 even when no daemon answers.** Against a `DOCKER_HOST` pointing at nothing,
docker 27.3.1 prints the *client's* half of the report, writes "Cannot connect to the
Docker daemon" to stderr, and still exits zero — while `docker version --format
'{{.Server.Version}}'` exits 1 on the same endpoint. A status-only check therefore reports
a client talking to itself as a healthy daemon, which is the exact false "everything is
fine" this report exists to stop telling. So "answered" means *the server fields came back
filled in*, and the real client's behaviour is pinned by a test
(`test_docker_info_exits_zero_at_an_endpoint_with_no_daemon`) that will fail if a future
docker fixes it.

The severities differ on purpose:

- **`steward doctor` warns, and still exits 0.** Doctor is routinely run on a laptop while
  the daemons live on the NAS, and a container this host cannot see is not a broken fleet —
  it is a report being run from somewhere other than the burrow. Same judgement
  `_report_scheduler` already makes about a state file this host cannot see.
- **`steward watchdog` says it in red, at startup, before its first pass.** That process
  *is* the supervisor: a container it cannot reach is not a diagnostic, it is its own
  defect. It does not refuse to start — two thirds of a pass need no docker, and stopping
  those would turn one gap into three — but the operator who typed the command is told,
  first, in the terminal they are looking at.

Neither of them fixes anything, and neither should: which machine a daemon runs on is an
operator's decision, not a control plane's.

## `DOCKER_HOST`: what it really buys

Docker's own pointer works, and steward does not get in its way. This was measured rather
than assumed, because the interesting question is whether steward's environment scrubbing
eats it:

- **A session's environment is an allowlist** (steward#41): a locally placed session gets
  `SESSION_ENV_BASE` and nothing else, and `DOCKER_HOST` is not on it.
- **A control-plane command's is not.** `run_argv` calls `subprocess.Popen` with no `env=`,
  so the child inherits the daemon's whole environment — and `_run_in_container` builds
  `{**os.environ, **request.env}` for the docker *client*, which is a control-plane tool
  like the nursery's `ssh`. Both are pinned by tests
  (`test_a_control_plane_command_inherits_the_daemons_docker_host`,
  `test_a_container_launch_hands_docker_the_daemons_docker_host`), and the consequence is
  pinned against a real client in `test_a_bogus_docker_host_reaches_the_real_docker_client`:
  with `DOCKER_HOST=tcp://127.0.0.1:1`, `docker ps` fails with *Cannot connect to the
  Docker daemon at tcp://127.0.0.1:1* rather than answering from this host.

So **steward does not eat `DOCKER_HOST`**, and docker's own remote-endpoint support
therefore applies to supervision: `docker inspect` and `docker restart` are ordinary docker
calls, and a client pointed at another machine runs them there.

Be exact about the edge of that. What is measured is the *passthrough* — steward hands the
variable to the real client, and the real client honours it. What is **not** measured here
is an end-to-end restart of a real container over a real remote endpoint: that needs a
second machine running docker, and neither the test suite nor this repo's CI has one.
Proving it would mean an integration test against a reachable remote daemon
(`ssh://…` or a TCP endpoint) asserting that `DockerSupervisor.restart` actually bounces
the container there — worth writing the day a second burrow exists, and dishonest to claim
before it. Until then this document claims the passthrough, which is measured, and not the
round trip, which is inferred from docker's own documented behaviour.

`DOCKER_HOST` does **not** relocate execution, and that reason is not docker's:

- A container-placed session's *own* half runs fine remotely — `docker exec` is just
  another docker call, and `-e NAME` reads its value from the client's environment.
- The control plane's half does not. Skills are materialized into, and the journal read
  from, the **host side** of the resident's memory mount (`deploy.memory_host_dir`), which
  is a path on the machine holding the container. `sessions.workdir_refusal` requires that
  directory to exist *on this host* and refuses the session otherwise
  (`unprovisioned_reason`). A remote `DOCKER_HOST` leaves that directory on the far machine,
  so the refusal fires — correctly, because materializing skills into a local path that
  merely resembles the remote one would be steward writing a resident's capabilities into
  the wrong filesystem, and `materialize` prunes whatever it does not own.

That asymmetry is why steward reports a set `DOCKER_HOST` as **unverified** rather than
fine: even where the endpoint does reach the declared host's docker, only half the fleet's
work would follow it there. The one exception is not a guess — when the daemon at the far
end *names itself* as the declared host, that is measured, and the report says reached.
Everywhere else the honest word is unverified. Use it as a break-glass for supervision; do
not use it to run residents on a machine the control plane cannot see the disk of.

## What we did not do, and when to revisit

**Teaching `DockerSupervisor` ssh** was the other half of steward#59. It is the right shape
eventually — a `Transport` for supervision, mirroring the one provisioning already has —
and it is not right yet, for three reasons:

1. It buys nothing this fleet needs. Every burrow today is the NAS, and the daemons belong
   on it anyway for the execution reason above.
2. It would fix only the supervision half, leaving container-placed execution still local —
   so the fleet would gain a *second* topology rule that differs by subsystem, which is
   worse than one rule that is true everywhere.
3. Restarting a container over ssh means holding an ssh credential in the watchdog's
   process, on a timer, unattended. Provisioning holds one for a human-initiated command;
   a daemon holding one forever is a different security question and deserves its own
   issue rather than a paragraph in this one.

Revisit when a second burrow genuinely exists — a second machine hosting residents that
the control plane is not on. At that point the honest design is one supervision transport
with local and ssh implementations, `deploy.host` selecting between them, and the
execution half (`#58`'s v1 remote placement) moving with it rather than after it. The
report this document describes is what will tell you the day has come: it already names
every container the daemons cannot reach.
