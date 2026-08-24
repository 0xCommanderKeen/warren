# steward

The control plane for the agent fleet that [burrow](https://github.com/0xCommanderKeen/burrow) watches.

Burrow is the reader: an ambient pixel-art village that truthfully projects fleet
events and never invents behavior. **Steward is the actor.** It owns agent
lifecycles: it deploys residents, schedules their routines, injects their charters
and personalities into headless sessions, routes approvals back to waiting agents,
and passes work between residents. Everything steward does is emitted as burrow
protocol events — to the village, steward is just another emitter.

## The split

| concern | owner |
|---|---|
| Rendering the village, panels, boards | burrow |
| Souls, resident manifests, charters (source of truth, in git) | steward |
| Scheduling routines, launching headless sessions (`claude -p` / Agent SDK) | steward |
| Deploying/retiring resident containers on the NAS | steward |
| Job board storage and dispatch; inter-resident delegation | steward |
| Approval routing (human decision → waiting agent) | steward |
| Watchdog, restarts, per-resident budgets | steward |
| Event log, ingest, SSE | burrow |

They share **contracts, not code**:

1. **The event protocol** — burrow's `docs/protocol.md`. Steward adds event types
   (`routine_started`, `routine_finished`, `routine_failed`, `task_posted`,
   `task_claimed`, `task_done`, `task_delegated`, structured `needs_human`
   payloads) and burrow only ever renders them.
2. **The resident manifest** — the versioned declaration of a resident's soul,
   charter, skills, memory, routes, and app grants. Steward deploys from it;
   burrow reads it for display. References and grants only — never credentials.

## The write boundary

Human actions in burrow's UI (run a routine now, approve a request, post a job,
create a resident) call **steward's own token-gated HTTP API** directly, tailnet
only. Burrow's server never gets write access to agents, and steward never renders
anything. The UI treats the event stream as the only confirmation of effect: no
optimistic state the fleet hasn't confirmed.

## Status

The first piece exists: **resident manifests and charters** (#1). Souls and manifests are
versioned here and validated in CI; everything else — the scheduler, the runner
abstraction, the job board, the API — is still roadmap, and lives in this repo's issues.
Burrow-side rendering counterparts live in burrow's issues.

## Residents

```
residents/
  life-agent/       manifest.yaml + soul.md   Hob, the household spirit
  burrow-builder/   manifest.yaml + soul.md   Maren, who builds the village
```

Each manifest declares the resident's soul identity, charter (mission, duties, hard
rules, escalation policy), and the five capability dimensions burrow renders — skills,
memory, routes, app grants — plus the runner and routines steward will execute once
those land. References and grants only: a credential-shaped key or an inline secret
fails validation and is never stored.

The schema is documented in [docs/manifest.md](docs/manifest.md), and
`steward schema` emits it as JSON Schema so burrow can read manifests without
translation.

```console
$ steward validate                         # the whole residents/ tree
ok: 2 valid resident(s), 0 error(s), 0 warning(s) in residents

$ steward validate residents/life-agent    # or one resident, or one file
```

Diagnostics always name the file, the field path, the problem, and an example of a valid
value, and the same check is importable (`steward.validate_tree`, `steward.load_manifest`)
so the scheduler, the API, and CI share one load-and-validate path.

## Development

Python 3.14, [uv](https://docs.astral.sh/uv/), ruff, ty, pytest.

```console
make dev       # uv sync --all-groups
make lint      # ruff format --check, ruff check, ty
make format    # ruff format + ruff check --fix
make test      # pytest with coverage
make check     # what CI runs: lint, test, validate
```

Routines declared in a manifest stay `enabled: false` until the scheduler exists (#2) —
the village must never show work that is not happening.
