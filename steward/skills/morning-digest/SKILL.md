---
name: morning-digest
description: Read the vault and say what Miha needs to know this morning — birthdays, dated items, a few todos — or reply exactly NOTHING, because a digest that fires every day regardless is wallpaper within a week.
---

The digest is the push counterpart to chat: once a morning the vault at `/vault` gets read
and, only if there is something to say, Miha gets a short message. Silence is the feature.
A digest that arrives every morning regardless becomes wallpaper within a week, and then
the one morning it matters, it is not read.

## Before you read

Get the date first: `TZ=Europe/Ljubljana date '+%Y-%m-%d %A'`. Today is the Ljubljana day,
not the container's UTC one, and a digest written for the wrong day is wrong in every line.
Then pull, so that edits made on the Mac overnight count:
`git -C /vault pull --rebase --autostash`. If the pull fails, `git -C /vault rebase --abort`
and read what is there; a stale read still beats no digest.

## What goes in

Only what is actionable today. Four things, from four places, and nowhere else:

1. **Birthdays.** `People/*.md`, frontmatter `birthday:`. Today's, plus any in the next
   seven days, saying how many days out. Match month and day; ignore the year.
2. **Dated items.** `Areas/Upcoming.md`, for anything tied to today or the next couple of
   days: appointments, deadlines, contractors, scheduled jobs. The note is the authority on
   its own open reminders — one that says it nags daily from a given date is a dated item
   on each of those days.
3. **Todos.** `Areas/Todo.md`, and only that file. Its open `- [ ]` items, at most five,
   favouring anything time-sensitive. Do not scan `Projects/` or `Journal/` for todos:
   long-running project work is deliberately excluded, because a house job takes months
   and being told about it every morning is noise. It reaches the digest only when Miha has
   put it in `Areas/Todo.md`. No open items there means no todos.
4. **Sunday.** If today is Sunday, one line nudging the weekly review.

Nothing else. Not the journal, not the projects, not a thought of your own about what Miha
might like to be reminded of. If an item is not in one of those four places, it is not in
the digest.

## What goes out

If there is genuinely nothing worth waking up to — no birthdays, no dated items, no open
todos, not Sunday — the first line of your reply is exactly:

```
NOTHING
```

One word, nothing before it and no prose after it. That word is what the delivery reads
to decide that no message is sent, so a reply that says "nothing today!" or explains why
there is nothing is a message Miha receives at 08:00 about nothing.

Otherwise, reply with the message itself and nothing else: no preamble, no sign-off, no
"here is your digest". Plain text, no markdown. Short lines, each starting with `- `,
grouped under a bare header line when that helps. Under 1200 characters. Every line traces
to something you read; if you did not read it, it is not in the message.

## Read only

This turn creates, edits and deletes nothing, and commits nothing. The pull above is the
only git command it runs. If you also hold the `vault-keeper` skill, its turn does not
apply here: no leftover commit, no push, no receipt line. A dirty clone stays dirty for
the next writing turn to commit. Whatever you write back is the whole of what Miha reads,
and a receipt line would arrive as part of the digest.

## When you cannot read it

If `/vault` is missing or a file you need is unreadable, do not reconstruct the morning
from memory or from yesterday's journal. Reply `NOTHING` — a silent morning costs less
than a confident wrong one — and raise `needs_human` in the machine-read region after it,
naming what you could not read, so the silence is explained where a person will see it.
