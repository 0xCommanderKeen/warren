---
name: raise-resident
description: Draft a new resident and hand the provision to a human — interview, read the fleet, declare the skeleton with deploy false, rehearse it, and knock once with the manifest text. Use when Miha asks for a new resident, or a job arrives that no resident covers.
---

A resident is a job with a name, a boundary and a budget. Drafting one is cheap and
reversible; provisioning one reaches a machine and starts a container that spends money on
a schedule from then on. You do the first. Miha does the second. The whole shape of this
skill is keeping those two apart, so that the moment of commitment is a person's.

## Interview first

Five answers, then draft:

1. **The job**, in one sentence a stranger could read. If it takes two sentences joined by
   "and", you may be describing two residents.
2. **What it may touch** — which directories, which repositories, which of the household's
   accounts. This becomes `workspace`, `deploy.mounts` and `app_grants`, and whatever is
   not named here is not granted.
3. **Who it talks to** — Miha on a chat route, the scheduler, another resident, nobody.
4. **What it must never do.** These become the charter's `rules`: the lines the resident
   reads back when it is unsure.
5. **When it knocks** — the situations that stop the work and send it to a human. These
   become `escalation.when`.

Stop asking once you have those five, and draft. Everything else you would ask is
something the draft can propose and a person can correct in one line.

## Read the fleet first

`GET /residents` and `GET /skills` before you write anything.

- **A resident that already exists under another name is a refusal.** Two residents doing
  one job means two budgets, two sessions, and one of them silently losing the race to
  whatever they both write. When the job sits close to an existing resident's, say so and
  propose the duty be added there instead — a manifest edit, and Miha's.
- **Reuse the skills that exist**, by name, and grant the smallest set that covers the
  duties. When a duty needs a skill nothing covers, that is `write-skill`'s job; a
  paragraph of charter doing a skill's work is a skill hidden where nobody can reuse it.
- **Reuse the fleet's vocabulary.** Charters here say duties and rules; routes are `chat`,
  `cron`, `delegation`. A resident that invents its own words reads like a stranger in the
  list and ages badly.

## Draft it in Hob's shape

`steward/residents/hob/manifest.yaml` is the worked example. What you write:

- **`summary`** — one line the village can display.
- **`charter.mission`** — what this resident is for, two sentences at most.
- **`charter.duties`** — the recurring work, one line each. A duty with no route and no
  routine that could ever trigger it is a wish; say so rather than declaring it.
- **`charter.rules`** — the hard lines from the interview, phrased as the behaviour you
  want wherever they can be: "move it to `Archive/`" over "never delete".
- **`charter.escalation`** — `when` naming the situations, `how` naming the transport
  (`needs_human`), and `note` saying what the knock must contain.
- **`skills`** — the smallest set covering the duties, each with a note saying why this
  resident holds it. The default set comes free and is not granted here.
- **`tools`** — narrowed from day one wherever the job allows it. `unrestricted` is an
  honest answer for a job that runs git and a shell, but write the reason beside it and
  name the evidence that would narrow it later.
- **`budgets.daily_cost_usd`** — a number. A resident declared with no budget is a resident
  with an unbounded one.
- **`soul_body` and `voice`** — the voice the job needs. A resident writing to Miha's phone
  at 08:00 and one filing tickets do not sound alike.

## Declare the skeleton

```
curl -sS -X POST "$STEWARD_URL/residents" \
  -H "Authorization: Bearer $STEWARD_SESSION_TOKEN" \
  -H 'Content-Type: application/json' -d @resident.json
```

with `"deploy": false` in the body. That flag is the whole grant: it writes
`manifest.yaml` and `soul.md`, commits them, and reaches no machine. `true` is refused, and
asking for it spends the turn on a 403. Keep the response's `commit.sha` — it is what the
knock points at.

Send only what the form takes: `id`, `name`, `char`, `accent`, `role`, `charter`, `skills`,
`routes`, `tools`, `runner`, `soul_body`, `voice`. It has no field for `deploy.mounts`,
`routines`, `budgets`, `delegation` or `notifications`, and it refuses an unknown key
rather than ignoring it. A `workspace` on a container-placed runner is refused too, because
the mount that would make the directory reachable cannot be declared here. Everything in
that list goes into the knock instead, as manifest text.

## Rehearse it

```
curl -sS -X POST "$STEWARD_URL/residents/<id>/provision" \
  -H "Authorization: Bearer $STEWARD_SESSION_TOKEN" \
  -H 'Content-Type: application/json' -d '{"dry_run": true}'
```

The plan comes back — the compose fragment and the exact commands a real provision would
run — and nothing is sent, started or written. Read it: a plan naming the wrong image, the
wrong host, or a mount you never declared is the cheapest bug you will ever catch.
`dry_run: true` is required, and the refusal without it is the door working.

## Knock once, then stop

The knock is the `escalate` skill's four parts carrying **one decision**:

```
Provision karen?

Skeleton committed as a1b2c3. The manifest text below is complete and ready to PUT — it
adds the mounts, the routines and the daily budget the declare form cannot express. The
dry run is clean: one container, steward-resident:latest, on dxp2800.

Yes and the PUT and the provision are yours to run; no and the skeleton sits in the tree
costing nothing. Nothing happens until you say.

<the complete manifest.yaml, ready to paste>
```

One decision, not three: the PUT, the provision and the skills are one act with one
answer, and splitting them turns an approval into an interview. Put the manifest text in
the knock itself, not in a file you promise to produce later — a person deciding needs the
bytes they would be approving.

Then stop, and let the skeleton rest unprovisioned in the tree. That is the correct
finished state for this work, and the next move is a human's.
