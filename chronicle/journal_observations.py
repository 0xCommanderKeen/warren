"""Bounded authority for Steward ``journal_written`` observations.

The event log says only that Steward observed a real file.  This reducer keeps
that evidence separate from interactive-session liveness and is shared by
strict ingestion and log rotation.
"""

from __future__ import annotations

import collections
import datetime
import json
import re
from pathlib import Path

MAX_DAYS = json.loads(
    Path(__file__).with_name("retention-policy.json").read_text(encoding="utf-8")
)["journal_days"]
MAX_DIAGNOSTICS = 40
_ROUTINE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_EDGE_WHITESPACE = frozenset(
    "\u0009\u000a\u000b\u000c\u000d\u0020\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
    "\u200a\u2028\u2029\u202f\u205f\u3000"
)


def validate_journal_event(event):
    """Return a stable error for an invalid journal observation, else ``None``."""
    if event.get("source") != "steward":
        return "journal observations require source steward"
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return "payload must be an object"
    routine = payload.get("routine")
    if (
        not isinstance(routine, str)
        or not 1 <= len(routine) <= 128
        or not _ROUTINE.fullmatch(routine)
    ):
        return "invalid payload.routine"
    day = payload.get("day")
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return "invalid payload.day"
    try:
        parsed_day = datetime.date.fromisoformat(day)
    except ValueError:
        return "invalid payload.day"
    if parsed_day.isoformat() != day:
        return "invalid payload.day"
    path = payload.get("path")
    if (
        not isinstance(path, str)
        or not 1 <= len(path) <= 2048
        or path[0] in _EDGE_WHITESPACE
        or path[-1] in _EDGE_WHITESPACE
        or any(0xD800 <= ord(character) <= 0xDFFF for character in path)
        or _CONTROL.search(path)
    ):
        return "invalid payload.path"
    segment = re.split(r"[/\\]", path)[-1]
    if segment != f"{day}.md":
        return "payload.path must end with payload.day.md"
    return None


def semantic_key(event):
    return event["agent_id"], event["payload"]["day"]


def canonical_identity(event):
    payload = event["payload"]
    return event["project"], payload["routine"], payload["path"]


def retention_rank(key):
    """Stable cross-adapter capacity rank: newest day, then agent code points."""
    agent_id, day = key
    return day, agent_id


def reduce_indexed(indexed_events):
    """Fold ``(append_index, event)`` pairs into bounded canonical authority.

    The first append owns ``(agent_id, day)``.  Later equal immutable shapes do
    not refresh it; the first incompatible shape is retained only as diagnostic
    evidence.  Capacity deterministically keeps the highest day/agent ranks.
    Once full, its lowest retained rank is a monotonic frontier, so an evicted
    or below-frontier key can never re-enter without an explicit reset.
    """
    records = collections.OrderedDict()
    malformed = []
    for index, event in indexed_events:
        if not isinstance(event, dict) or event.get("type") != "journal_written":
            continue
        error = validate_journal_event(event)
        if error:
            if len(malformed) == MAX_DIAGNOSTICS:
                malformed.pop(0)
            malformed.append((index, error))
            continue
        key = semantic_key(event)
        record = records.get(key)
        if record is None:
            if len(records) == MAX_DAYS and retention_rank(key) <= retention_rank(
                next(iter(records))
            ):
                continue
            records[key] = {"canonical": (index, event), "conflict": None}
            if len(records) > MAX_DAYS:
                records.popitem(last=False)
            records = collections.OrderedDict(
                sorted(records.items(), key=lambda item: retention_rank(item[0]))
            )
            continue
        if canonical_identity(event) == canonical_identity(record["canonical"][1]):
            continue
        if record["conflict"] is None:
            record["conflict"] = (index, event)
    return records, malformed


def keep_indexes(indexed_events):
    records, _ = reduce_indexed(indexed_events)
    keep = set()
    for record in records.values():
        keep.add(record["canonical"][0])
        if record["conflict"] is not None:
            keep.add(record["conflict"][0])
    return keep
