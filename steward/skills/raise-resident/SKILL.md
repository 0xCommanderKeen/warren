---
name: raise-resident
description: Draft a new resident, declare the skeleton, and hand the provision to Miha. Use when a job arrives that no resident covers, or Miha asks for one.
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
  propose the duty be added there instead. If that comes down to one skill, it is a knock
  rather than a new resident — see **A skill onto an existing resident** below.
- **Reuse the skills that exist**, by name, and grant the smallest set that covers the
  duties. When a duty needs a skill nothing covers, that is `write-skill`'s job; a
  paragraph of charter doing a skill's work is a skill hidden where nobody can reuse it.
- **Reuse the fleet's vocabulary.** Charters here say duties and rules; routes are `chat`,
  `cron`, `delegation`. A resident that invents its own words reads like a stranger in the
  list and ages badly.

## Draft it in Hob's shape

`GET /residents/hob/declaration` returns the worked example, and reads are always open to
you. Draft the whole manifest, not just the part the declare form takes:

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

The form takes most of the draft — down to `skills` with their notes, `routes`,
`app_grants`, `session_grants`, `workspace` and `runner`. It has no field for `deploy`,
`routines`, `budgets`, `board`, `delegation` or `notifications`, and it refuses an unknown
key rather than ignoring it, so send those nowhere: they go into the knock as manifest
text. One trap joins them — a `workspace` path is accepted only when something already
provides it, the memory directory or a declared mount, so a workspace waiting on a mount
waits in the knock beside it.

## Dry-run it

```
curl -sS -X POST "$STEWARD_URL/residents/<id>/provision" \
  -H "Authorization: Bearer $STEWARD_SESSION_TOKEN" \
  -H 'Content-Type: application/json' -d '{"dry_run": true}'
```

The plan comes back in `compose` and `commands` — the compose fragment, and the exact
argv a real provision would issue — and nothing is sent, started or written. Read it: a
plan naming the wrong image, the wrong host, or a mount you never declared is the cheapest
bug you will ever catch. `dry_run: true` is required, and the refusal without it is the
door working.

## Rehearse it

The dry run proves the plan; it says nothing about whether the charter reads right. So
send the draft one message shaped like its first real one:

```
curl -sS -X POST "$STEWARD_URL/residents/<id>/rehearse" \
  -H "Authorization: Bearer $STEWARD_SESSION_TOKEN" \
  -H 'Content-Type: application/json' -d '{"message": "Good morning — anything for me?"}'
```

One throwaway turn from the declaration: its charter, soul and skills in the prompt, and
no container, no mounts, no memory directory, no credential and no tools. `reply` is what
it said — so ask it something it can answer by talking, not by doing.
**This one costs money, and the money is yours** — charged to your budget line, because
the draft has none. Rehearse once, read the reply against the voice you were asked for,
and fix the charter rather than the reply if they disagree. It needs its own grant, `residents.rehearse`;
the dry run is not it.

## Knock once, then stop

The knock is the `escalate` skill's four parts carrying **one decision**:

```
Provision karen?

Skeleton committed as a1b2c3. The manifest text below is complete and ready to PUT — it
adds the mounts, the routines and the daily budget the declare form cannot express. The
dry run is clean: one container, steward-resident:latest, on dxp2800.

I sent her "Good morning — anything for me?" and she said:

  Morning. Nothing due yet; I will knock when the first errand lands.

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

## A skill onto an existing resident

The other answer the fleet read can give: one more skill on a resident that exists. Miha
approves, you edit — a separate knock naming the two things the write is checked against:

```
<needs-human action="grant_skill" expires-in="24h" options="approve,deny">
{"resident": "shelf-worker", "skill": "series-detection",
 "note": "Reads the series index so it stops guessing from filenames."}
</needs-human>
```

Once approved, `GET /residents/<id>/declaration`, add the one `skills` entry, and `PUT`
it back whole with `"approval_request_id": "<id>"` and no `soul`. Any other difference is
refused. One approval is one edit; report the commit hash.
