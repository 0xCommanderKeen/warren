---
name: grant-skill
description: Put one existing skill onto one existing resident, against Miha's approval. Use when a duty fits a resident that already exists and a skill that already exists.
---

A skill in the library is held by nobody until a manifest says so, and editing a manifest
is editing the rules that resident is held to. That is Miha's to approve and yours to make
once he has. Two moves, in two sessions: a knock, then one line.

## The knock

Raise it as one decision, carrying the two names steward will check the write against and
the note saying why that resident holds it:

```
<needs-human action="grant_skill" expires-in="24h" options="approve,deny">
{"resident": "shelf-worker", "skill": "series-detection",
 "note": "Reads the series index so it stops guessing from filenames."}
</needs-human>
```

Spell `resident` and `skill` exactly. Then stop: the answer reaches you in a later
session's `DECISIONS SINCE YOU LAST RAN`, and nothing happens meanwhile.

## The line

When it says approve, read the declaration, add the one `skills` entry, and write it back
whole with the request id and no `soul`:

```
curl -sS "$STEWARD_URL/residents/<id>/declaration" \
  -H "Authorization: Bearer $STEWARD_SESSION_TOKEN"

curl -sS -X PUT "$STEWARD_URL/residents/<id>/declaration" \
  -H "Authorization: Bearer $STEWARD_SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"text": "<the manifest, one skills entry added>", "approval_request_id": "<id>"}'
```

Send the bytes `GET` returned with that one entry added. Steward compares them against
what is on disk and refuses every other difference, down to a reordered list. One approval
is one edit: a refusal is something to knock about again, never something to retry with
different bytes. Report the commit hash.
