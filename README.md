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
