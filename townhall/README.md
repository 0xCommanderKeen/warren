# townhall

The warren control panel: the governance surface for the fleet, built with Vite, React and
Tailwind CSS. It watches the village through [Chronicle](../chronicle) and writes to it
through [steward](../steward).

The look is the steward operator console's (`steward/ui/`), ported onto this stack rather
than redesigned — a sidebar hosting every section, warm ink on near-black, one meaning per
colour. The read-only fleet atlas this directory used to be is now one page inside that
shell.

## Two servers, one page

| | reads | writes |
|---|---|---|
| Chronicle `/state`, `/state/stream` | the fleet page | never |
| steward | residents, skills, budgets | residents, skills, budgets |

They are kept apart on purpose. The fleet page is unauthenticated and works for anybody who
can load the page; steward gates every route on an operator token a human types at runtime,
which lives in the tab's `sessionStorage` and is never built into the bundle. A resident's
session credential will not do — steward answers `403 session_credential_forbidden` to every
write here, by design.

## Pages

- **Fleet** — read-only telemetry over Chronicle's snapshot, plus the uid-addressed agent
  record at `/agents/:uuid`.
- **Residents** — the fleet steward could validate, and each resident's declaration:
  manifest fields or the YAML byte for byte, with the soul document beside it.
- **Skills** — the library, and the editor for adding and replacing one.
- **Budgets** — daily caps per resident, with the spend steward has actually recorded
  against them.

Every write renders steward's own answer: the commit it made, or the refusal and its
per-field diagnostics. Nothing here synthesises a success.

## Develop

```sh
pnpm install
pnpm dev
```

The dev server proxies Chronicle's state endpoints to `http://127.0.0.1:8737` and steward's
routes to `http://127.0.0.1:8801` (`BURROW_URL` / `STEWARD_URL` override both), so the app
talks to both same-origin exactly as the deployed origin does. `?backend=` and `?steward=`
point either at a CORS-enabled deployment instead.

## Verify and build

```sh
pnpm test
pnpm build
```

**The deployed build needs its mount prefix**: `pnpm build --base=/observatory/`. The router
reads that prefix back out of `import.meta.env.BASE_URL`, so links and deep links work under
any mount — see [`docs/deployment.md`](docs/deployment.md).

## Contract

The fleet page consumes only Chronicle's complete version-1 snapshot envelopes from
`GET /state` and `GET /state/stream`; it never reads the event log. Its parser is tested
against Chronicle's own contract fixture, read in-tree at
`chronicle/tests/fixtures/state-contract/complete-v1.json` rather than vendored, so drift is
impossible by construction.

The write paths are steward's documented API — see [`steward/docs/api.md`](../steward/docs/api.md).
