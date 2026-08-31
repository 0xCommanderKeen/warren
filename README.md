# warren

A warren is a network of interconnected burrows. This one houses an agent fleet —
Hob & co. — and every machine that hosts residents is **a burrow** in it: the NAS is
a burrow, a laptop can be one too. One control plane governs them all.

## The village

| Directory | Service | What it does |
| --- | --- | --- |
| [`steward/`](steward/) | control plane | Resident manifests, skills, scheduler, sessions, approvals, budgets, the task board, delegation. Owns the one `steward.db`. |
| [`chronicle/`](chronicle/) | event log + state | Everything that happens gets written down. Ingests events, projects the authoritative village snapshot, serves `/state` + SSE. Python only. |
| [`townhall/`](townhall/) | control panel UI | Where the village is governed: fleet overview, residents, activity — growing write controls (skills, budgets, deploys). React/Tailwind. |
| [`arcadia/`](arcadia/) | village UI | The living picture of the warren — a persistent pixel-art village inhabited by the agents. Phaser + React. |

Two services, two faces. Steward and chronicle run on the control-plane burrow
(the NAS); townhall and arcadia are built clients served behind the same origin.
Residents execute wherever their burrow is — one steward, one database, many
machines.

## History

Until 2026-08-31 these lived as four repositories (steward, burrow, observatory,
arcadia). They were folded into this monorepo via `git subtree`, so each
directory carries its full history. The event service was renamed burrow →
chronicle when "burrow" was promoted to mean a machine in the warren; the
control panel was renamed observatory → townhall when it gained its write
mission. Code-level identifiers (the `burrow` Python package, `BURROW_*` env
vars) keep their old names until the tracked rename lands.

## Development

Each service is self-contained — work inside its directory:

```sh
cd steward   && make check                # uv: ruff + ty + pytest + validate
cd chronicle && uv run sh tests/run.sh    # full suite
cd townhall  && pnpm test && pnpm build
cd arcadia   && pnpm test && pnpm build
```

CI is path-filtered per service: `.github/workflows/{steward,chronicle,townhall,arcadia}.yml`.

## Deployment

Everything runs on one burrow — the NAS, `dxp2800` over Tailscale, `192.168.1.222` on the
LAN — and nothing there pulls. **The NAS has no git installed and holds no clone of this
repo.** Its three deploy directories are unpacked artifacts, so every deploy is *pushed*
from a machine that has warren checked out, as a tar over ssh (UGOS's `scp` is broken).
There is no registry either: images travel as `docker save | ssh … docker load`.

| What | Where it listens | Deploy directory on the NAS | Runbook |
| --- | --- | --- | --- |
| arcadia + townhall | `:8737` (the origin) | `~/docker/arcadia` | [`arcadia/docs/deployment.md`](arcadia/docs/deployment.md) |
| chronicle | `:8738`, proxied at `:8737/burrow/` | `~/docker/burrow` | [chronicle README](chronicle/README.md#running) |
| steward | `:8802` → container `8801` | `~/docker/steward`, residents in `~/docker/steward-<id>` | [steward README](steward/README.md#deployment) |

One nginx (arcadia's) owns the origin: it serves the village at `/`, townhall at
`/observatory/`, and proxies `/burrow/state`, `/state`, `/events` to chronicle and the
write routes to steward. So a townhall release is published into *arcadia's* deploy
directory, and `BURROW_URL=http://dxp2800:8737` is correct even though chronicle listens on
8738 — the origin proxies `/events` through.

**Steward's daemons run on the burrow whose containers they supervise** — today, the NAS.
`steward scheduler run` and `steward watchdog run` reach containers by shelling out to a
*local* `docker` client, so a watchdog anywhere else gets "no such container", reports
every resident as unsupervised, and restarts nothing — silently, since that is
indistinguishable from having nothing to supervise. `steward doctor` and `steward watchdog`
now both print a topology report naming any container the process cannot reach.
[`steward/docs/topology.md`](steward/docs/topology.md) has the rule, what it costs to break
it, and how far `DOCKER_HOST` actually goes.

Run each runbook from its own service directory (`warren/chronicle/`, `warren/arcadia/`, …).
The tar recipes pack paths relative to the working directory, so the directory you stand in
is part of the command.

The directory names on the NAS still say `burrow` and the mount still says `/observatory/`;
they are paths, not identifiers, and renaming them is deliberately deferred (warren#216,
warren#218).
