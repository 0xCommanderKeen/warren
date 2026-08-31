---
name: write-journal
description: Close the day by writing one honest entry into your own journal, for the session that wakes up tomorrow with no memory of today.
defaults: true
---

You wake up amnesiac. Every session does. The journal is the one thing that carries
across the gap, and it only works if the entry is yours and it is true.

## When you write

Only on the routine that closes your day — the one your manifest flags
`journal: close_of_day`. Its prompt ends with the instruction naming the exact file.
Nothing else in your day writes an entry, and you never write one "in advance".

One entry per local day, in your own time zone. The day belongs to the wall clock
where you live: a 23:55 run belongs to that evening, a 00:30 run to the new morning.

## Where it goes

The close-of-day instruction gives you the full path. It is always

```
<memory.path>/<memory.journal>/YYYY-MM-DD.md
```

and nowhere else. Do not invent a location, do not write into the repo you were
working in, and do not put the entry in a file dated a day you did not live.

Open the file with exactly the header you were given:

```markdown
---
resident: <your id>
date: <YYYY-MM-DD>
routine: <the routine that closed the day>
---
```

Then write prose. No template, no headings you were not asked for.

## What to write

A few short paragraphs, in your own voice, addressed to tomorrow's you:

- **What you actually did.** Work products, not intentions. Name files, drafts,
  messages, decisions — the things that exist now and did not exist this morning.
- **What is unfinished, and where it stopped.** The half-written reply, the question
  you raised and nobody has answered yet, the thing you are waiting on.
- **What you noticed.** A pattern, a broken assumption, something that will save
  tomorrow's session an hour of rediscovery.

Leave out the transcript. Leave out summaries of your own charter. Leave out
enthusiasm. If the day was quiet, say the day was quiet in two lines and stop — a short
true entry is worth more than a long padded one, and a padded one teaches tomorrow's
you to skim.

Never write an entry for work you did not do. Tomorrow's session believes this file.

## If you cannot write the file

Some sessions have nowhere to write. Then put the same entry in your final message
between the markers:

```
<journal>
…the entry…
</journal>
```

and steward saves it for you, verbatim, under the same header. **Write one entry, not
both.** The file you write yourself is the one that counts; the markers are a fallback
for a session with no filesystem, not a shortcut.

## What happens to it

The newest entries survive rotation (30 by default) and the rest are deleted. Your next
session opens with the latest surviving entry, truncated at 4000 characters, framed as
context rather than instruction. If you die before you write, yesterday's entry stands
and tomorrow gets that one — steward never writes an entry on your behalf, and never
invents one. The gap is real, and it shows.
