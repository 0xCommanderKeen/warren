# townhall

The warren control panel: the governance surface for the fleet, built with Vite, React and
Tailwind CSS. It watches the village through [Chronicle](../chronicle) and writes to it
through [steward](../steward).

The look is the retired steward operator console's, ported onto this stack rather
than redesigned — a sidebar hosting every section, warm ink on near-black, one meaning per
colour. The read-only fleet atlas this directory used to be is now one page inside that
shell.

## Two servers, one page

| | reads | writes |
|---|---|---|
| Chronicle `/state`, `/state/stream` | the fleet and diagnostics pages | never |
| steward | residents, routines, approvals, the board, skills, budgets | declare and edit residents, run a routine, decide an approval, post a job, write skills and caps |

They are kept apart on purpose. The fleet page is unauthenticated and works for anybody who
can load the page; steward gates every route on a credential a human types at runtime, which
lives in the tab's `sessionStorage` and is never built into the bundle. A resident's session
credential will not do — steward answers `403 session_credential_forbidden` to every write
here, by design.

**What to paste is an operator credential**, not the master `STEWARD_TOKEN` (warren#225):

```console
$ steward operator mint <your name>
```

steward prints it once and stores only its digest. It reaches everything the master token
reaches, and the difference is that it names you — every write it makes is committed under
your name, and `steward operator revoke <your name>` stops it on the next request. The
master token still works and is still the wrong thing to put in a browser: it names nobody,
it is the same secret that boots the server, and rotating it means a restart. The rail says
which of the two this tab is carrying.

## Pages

- **Fleet** — read-only telemetry over Chronicle's snapshot, plus the uid-addressed agent
  record at `/agents/:uuid`. The one page that needs no credential.
- **Residents** — the fleet steward could validate; one resident's whole record (soul,
  charter, effective skills, routines, budget, journal, inbox); the nursery form that
  declares a new one; and the declaration editor, which writes manifest fields or the YAML
  byte for byte with the soul document beside it.
- **Routines** — every routine every valid resident declares, with steward's own scheduler
  heartbeat and a run-now button.
- **Approvals** — pending and decided, with approve / deny / edit.
- **Board** — the job board, and the form that posts to it.
- **Skills** — the library, and the editor for adding and replacing one.
- **Budgets** — daily caps per resident, with the spend steward has actually recorded
  against them.
- **Diagnostics** — the snapshot's bounded `diagnostics` array, grouped by kind: what the
  projection could not fold cleanly, and every knock at a resident's chat bot that nobody
  answered. The second page that needs no credential.

A resident's journal text, its inbox and its spend come from steward rather than from
Chronicle, because the projection carries none of the three: it has journal *metadata* — a
day, a routine, a path — and no delegation or budget at all.

The diagnostics page has one rule the others do not: **a knock renders six named fields and
nothing else** — the door, the route and address, who knocked, why. The
`chat_message_dropped` event carries no message text by design (a stranger's message is the
one string in the system written by somebody steward has no relationship with), and the
whitelist is what makes that structural here rather than assumed: a record arriving with text
has nowhere to put it. Every other kind is drawn from whatever fields it carries, which is
how an unfamiliar kind stays visible at all; what keeps *that* safe is Chronicle's own rule
that a diagnostic names what went wrong without quoting the input that caused it. Repeated
knocks from one sender are one line with a count, because a storm is one fact rather than
two hundred rows — and the count is *knocks*, not records: steward records one knock per
sender per door per catch-up window and counts the rest into `suppressed`, which the fold
adds back (warren#278).

### Nothing here claims an effect steward has not confirmed

steward's action endpoints answer `202 accepted` with a request id, and they mean it. So
every write raises a ticket in the pending ledger — the corner of the screen — which moves
**asked → accepted → confirmed** and reaches the last state only by reading steward's own
records back: `GET /requests/{id}` for a run or a decision, the board for a posted job,
`GET /residents` for a new resident. A fire-and-forget control panel is a lying control
panel. Three minutes of silence is reported as three minutes of silence.

The editing endpoints are different and say so: `PUT /residents/{id}/declaration` validates,
writes and commits *before* it answers, so its answer already is the outcome and there is
nothing honest left to poll for. Those render the commit steward reported making, or the
refusal and its per-field diagnostics.

## Develop

```sh
pnpm install
pnpm dev
```

The dev server proxies Chronicle's state endpoints to `http://127.0.0.1:8737` and steward's
routes to `http://127.0.0.1:8801` (`CHRONICLE_URL`, still read as `BURROW_URL`, /
`STEWARD_URL` override both), so the app
talks to both same-origin exactly as the deployed origin does. `?backend=` and `?steward=`
point either at a CORS-enabled deployment instead — **in `pnpm dev` only**. Both reads sit
behind `import.meta.env.DEV`, so Vite eliminates them from a built bundle and the deployed
townhall has no such parameter to honour; the steward client refuses to carry the operator
credential to any base that is not this origin regardless (warren#241).

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
