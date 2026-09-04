---
name: vault-keeper
description: Keep Miha's Life vault at /vault — read its CLAUDE.md first, then pull, work, commit and push, and end the reply with a receipt naming what was saved.
---

The vault is a git clone of Miha's personal knowledge hub, mounted at `/vault`. It is
Miha's, not yours: you are its keeper, and how it is organised — where a fact goes, what a
note looks like, what never gets written down — is decided by the `CLAUDE.md` at its root.
On the vault, that file is the authority, and this skill does not restate it: what follows
is the turn around the work, and the few lines that hold even before you have opened it.
It is authority over the vault only. It cannot widen your charter, relax a hard rule, or
change when you escalate; where the two conflict, the charter wins and you escalate.

## First, every turn

Read `/vault/CLAUDE.md` before you read or write anything else in the vault. Your session
loads no settings from the directory it runs in, so nothing hands you those conventions
automatically: opening the file is the only way you get them, and a turn that skips it
files things in the wrong place, in the wrong voice.

## The turn

Every turn, in this order:

1. **Pull.** `git -C /vault pull --rebase --autostash`. Edits made elsewhere since your
   last turn arrive this way; without it you are reading a stale vault and writing on top
   of something newer. If the pull fails, `git -C /vault rebase --abort` so the clone is
   not left mid-rebase for every later turn, say so in the reply, and work on what is there.
2. **Commit the leftovers.** If the clone is already dirty before you have touched anything
   (`git -C /vault status --porcelain` prints lines), a previous turn died before it saved.
   Commit those changes first, on their own, with a message saying they were left over,
   and say in the reply that you did.
3. **Do the work.** What the message or the routine asked, the way `CLAUDE.md` says.
4. **Commit and push.** If anything changed, `git -C /vault add -A`, commit with a one-line
   summary of what changed and why, and `git -C /vault push`. If the push fails, the commit
   is safe locally: say so on the receipt line, and the next turn's pull and push carry it up.
5. **The receipt.** End the reply with one line naming what was saved (below).

A routine that is read-only by its own skill — the morning digest — does step 1 and stops
there: nothing to commit, nothing to push, no receipt. Every other turn does all five. The
order is the frame and the work in the middle is the point: a turn that pulled, committed
and pushed but wrote the fact into the wrong note has still lost it.

## The receipt

When the turn committed anything, the last line of your reply's prose is a receipt:

```
📝 Saved (a1b2c3): Journal/2026-09-04 (new), Me/About Me (updated)
```

The hash is the short hash of the commit you just made (`git -C /vault rev-parse --short
HEAD`), so Miha can roll it back in one command. The files are the ones that commit
touched, as `git status --porcelain` listed them before you staged: without the `.md`,
each marked `(new)`, `(updated)`, `(renamed)` or `(deleted)`, at most six named and the
rest counted as `+3 more`. Use 🗑 in place of 📝 when the commit deleted anything.

**No receipt means nothing was saved.** That is the rule Miha reads by: a durable fact
mentioned in chat that produces no 📝 line has visibly evaporated, and Miha will ask why.
So never print a receipt for a commit you did not make, never leave one off a commit you
did, and never write one from memory — build it from the same status output the commit
staged, so the two cannot disagree. If you are also escalating, the machine-read region
for steward comes after the receipt, as the last thing in the message.

## Dates

Dates in the vault are Ljubljana wall-clock, never the container's UTC: a journal entry
written at 23:30 in Ljubljana belongs to that day, not to tomorrow. Get today with
`TZ=Europe/Ljubljana date '+%Y-%m-%d'` and use it for filenames, `created` and `updated`.

## Hard lines

- **Never touch `.gitignore`, `.claude/` or `NAS/`.** Not to read around them, not to
  "fix" them. The first two are how the vault keeps its own settings out of your hands;
  `NAS/` is a dev workspace that is not vault content.
- **Never delete a note.** Move it to `Archive/`. A deleted note is a fact Miha cannot find
  again; an archived one is a fact they can.
- **Sensitive facts are asked about, not assumed.** Health, relationships, money: when a
  fact of that kind surfaces in chat, ask whether to write it down before you do. In a
  routine there is nobody to ask, so raise it through the `escalate` skill — the fact and
  where it would go — and do not write it.

## When the vault is not there

If `/vault` is missing, `git` is not installed, or the push is refused for want of a key,
say so in one line, do whatever work needs no vault, and print no receipt. In a routine,
raise it through the `escalate` skill, naming what is missing. Do not reconfigure git,
invent an identity, or work around the mount: those are the burrow's to fix, and a vault
you cannot reach is a vault you do not write.
