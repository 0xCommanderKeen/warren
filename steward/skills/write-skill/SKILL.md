---
name: write-skill
description: Write a skill into steward's library, from the interview to the commit hash. Use when someone asks for a new skill, or a skill nobody holds yet needs rewriting.
---

A steward skill is one file: frontmatter and a body, no reference files, no scripts, no
second page. Every session that holds it is handed the whole body in its prompt before it
has read a word of the actual work — so a line that does not change what that session
does is a line every run pays for and nobody reads. Write it for the resident who will be
reading it at 08:00 with nobody awake to ask.

## Interview first

Three answers, and you can draft without asking anything else:

1. **Which resident** will hold it, or which residents if it is a craft more than one
   needs. A skill written for nobody in particular reads like documentation.
2. **Which branches of its work** it covers. A branch is a case that takes a different
   path — the vault is missing, the push is refused, there is nothing worth saying today.
   Branches are what a body is made of; work with one branch is a line in a charter.
3. **What done looks like** for a turn that used it: the thing you could point at
   afterwards and say the skill worked.

Stop asking once you have those three. A fourth question buys detail you would have
drafted correctly anyway, and a draft is easier to argue with than a question.

## The craft

**The description does the triggering.** It is a pointer, and the session reads it beside
every other description and decides from that alone. Front-load the words that name the
work, say what the skill is, then say **"Use when …"** and name the branches that should
reach for it. One trigger per branch: two synonyms for one branch is one branch written
twice, in a line that costs on every turn.

**Every step ends on a checkable criterion.** "Understand the schedule" is not one —
nothing tells the session it is finished, so it finishes early. "Every routine in the
manifest named, with its cron and its timezone" is. Where the bound is irreducibly fuzzy,
name what the session must be able to say afterwards.

**State the positive.** A ban drags the banned thing into a session's attention and half
reads as an instruction to do it. Write the behaviour you want — "move it to `Archive/`"
rather than "never delete". Where a hard guardrail can only be phrased as a prohibition,
put the positive target beside it.

**One source of truth.** A rule that already lives in the resident's charter, in a default
skill, or in the `CLAUDE.md` of a repository it works in is not yours to restate: point at
it. A rule copied into two places goes stale in one and stays right in the other, and then
a session reads two authorities disagreeing inside one prompt.

**Then prune.** Read the draft once more, sentence by sentence, hunting two kinds of line:
one the session would already obey by default, which pays tokens to say nothing; and one
that is true only today — a version, a date, a resident that exists now — which will be a
lie by the time it matters. Delete the whole sentence rather than trimming words from it.

## Steward's shape

- **`GET /skills` first.** The library is shared, so improving a skill improves every
  resident holding it: work an existing skill already covers wants a rewrite, not a second
  name. When nothing covers it, read the neighbours anyway so your vocabulary matches theirs.
- **The frontmatter is `name`, `description` and `defaults`, and nothing else.** A fourth
  key is a validation error, not a comment. `name` matches the slug you post.
- **`defaults: true` is never yours to set.** It is not a property of the skill — it hands
  the body to every resident in the fleet, granted or not. Send `false`, and say in your
  reply if you think the fleet should hold it. The door refuses the flag in any case.
- **The body references no files.** Whatever the session needs is in the body or is not
  there at all: a link to `REFERENCE.md` resolves to nothing in a session's prompt.
- **Keep the body under 8,000 characters.** Every skill a resident holds is paid for out
  of one 24,000-character prompt budget, and a set that overruns it is cut mid-sentence
  with `[truncated at the injection cap]` where the rest of the instructions should be.
  `GET /skills` reports every skill's `body_chars` and its `holders`, which is how you add
  up the whole set the holder will actually be given rather than only your own line.

## Writing it

Your session holds `STEWARD_SESSION_TOKEN` and `STEWARD_URL`:

```
curl -sS -X POST "$STEWARD_URL/skills" \
  -H "Authorization: Bearer $STEWARD_SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"…","description":"…","body":"…","defaults":false}'
```

Rewriting an existing skill is `PUT "$STEWARD_URL/skills/<name>"`, carrying the `revision`
from `GET /skills/<name>` so you cannot overwrite an edit you never read. That door is open
only while no manifest grants the skill: once a resident holds it, rewriting it rewrites
instructions somebody was given, and the refusal names the holders. Take that as the
answer — knock with the new body and let a human make the swap.

End the reply on the receipt, built from the response's `commit.sha` and the two sizes:

```
🧩 read-invoices added (a1b2c3) — 3,412 of the 8,000-character body cap;
   hob's set now renders at 12,104 of the 24,000-character prompt budget.
```

Name the set only for a resident that will actually hold it; for a skill nobody holds yet,
the body count alone is the honest half. The commit hash is how the person who asked reads
what you wrote without asking you to summarise it: a skill reported with no commit hash
has, as far as anyone can check, not been written.

## The grant is somebody else's knock

Writing a skill into the library gives it to nobody: a resident holds it when its manifest
says so, and that edit is one decision of Miha's. Raise it with `grant-skill`, naming the
resident and the skill you just wrote — never by editing a declaration yourself.
