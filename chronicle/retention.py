"""The village's bounded memory after event-log rotation.

This module owns the pure retention policy and selectors shared conceptually
with the browser projection. Server transport and archive mechanics stay in
``serve.py``.
"""

import collections
import datetime
import functools
import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from approval_protocol import structured_approval
from journal_observations import reduce_indexed as reduce_journal_indexed
from protocol import EVENT_TYPES as PROTOCOL_EVENT_TYPES
from protocol import validate_event
from typed_json import canonical_string, decode_graph, semantic_key

_POLICY_PATH = Path(__file__).with_name("retention-policy.json")
POLICY = MappingProxyType(json.loads(_POLICY_PATH.read_text(encoding="utf-8")))

EVENT_TYPES = set(PROTOCOL_EVENT_TYPES)
KEEP_PER_AGENT = POLICY["events_per_agent"]
VIEWER_LINE_LIMIT = POLICY["viewer_line_limit"]
DROP_MS = POLICY["drop_ms"]
KEEP_TASKS = POLICY["tasks"]
KEEP_APPROVALS = POLICY["approvals"]
TASK_EVENT_TYPES = {"task_posted", "task_claimed", "task_done", "task_failed"}
PROJECTION_ACTION_TYPES = {"task_started", "tool_called", "artifact_produced"}
MOOD_TERMINAL_TYPES = {
    "tool_failed",
    "routine_failed",
    "task_failed",
    "heartbeat",
    "routine_finished",
    "task_done",
}
MOOD_FAILURE_TYPES = {"tool_failed", "routine_failed", "task_failed"}
MOOD_WORK_WEIGHTS = {
    "task_started": 3,
    "task_claimed": 3,
    "routine_started": 3,
    "artifact_produced": 2,
    "journal_written": 2,
    "tool_called": 1,
    "heartbeat": 1,
}
MOOD_ORDINARY_SUPERSEDERS = {
    "task_started",
    "tool_called",
    "tool_failed",
    "artifact_produced",
    "heartbeat",
    "needs_human",
    "idle",
    "session_ended",
}
MOOD_AUTHORITY_KIND = POLICY["mood_authority_kind"]
MOOD_AUTHORITY_ENCODING = POLICY["mood_authority_encoding"]
MOOD_AUTHORITY_LIMIT = POLICY["mood_authority_events"]
MOOD_AUTHORITY_MAX_BYTES = POLICY["mood_authority_bytes"]
MAX_MOOD_RETAINED_PER_AGENT = POLICY["mood_retained_per_agent"]
MOOD_AUTHORITY_MAX_DEPTH = POLICY["mood_authority_depth"]


@dataclass(frozen=True)
class Retention(Sequence[str]):
    """A retained log plus its durable capsule and panel selection witnesses."""

    lines: tuple[str, ...]
    capsule: dict | None
    witnesses: Mapping[str, frozenset[int]]

    def __iter__(self) -> Iterator[str]:
        return iter(self.lines)

    def __len__(self) -> int:
        return len(self.lines)

    def __getitem__(self, index):
        return self.lines[index]

    def __eq__(self, other):
        if isinstance(other, Retention):
            return (self.lines, self.capsule, self.witnesses) == (
                other.lines,
                other.capsule,
                other.witnesses,
            )
        if isinstance(other, Sequence):
            return list(self.lines) == list(other)
        return NotImplemented


def _json_domain_within(value, max_depth=math.inf):
    """Iteratively prove a bounded JSON tree, rejecting cycles and aliases."""
    seen = set()
    active = set()
    stack = [(value, 0, False)]
    try:
        while stack:
            item, parent_depth, exiting = stack.pop()
            if exiting:
                active.remove(id(item))
                continue
            if item is None or isinstance(item, (bool, int, float, str)):
                continue
            if not isinstance(item, (list, dict)):
                return False
            depth = parent_depth + 1
            identity = id(item)
            if depth > max_depth or identity in seen:
                return False
            seen.add(identity)
            active.add(identity)
            stack.append((item, depth, True))
            if isinstance(item, list):
                stack.extend((child, depth, False) for child in reversed(item))
            else:
                if not all(isinstance(key, str) for key in item):
                    return False
                stack.extend((item[key], depth, False) for key in reversed(list(item)))
    except (KeyError, RuntimeError, TypeError, ValueError, OverflowError):
        return False
    return True


def _mood_raw_index(index):
    return f"{index:016d}"


def _mood_authority_event(event):
    payload = event.get("payload") or {}
    return event.get("type") in {"needs_human", "needs_human_resolved"} or (
        event.get("type") == "task_started"
        and event.get("source") in {"claude-code", "codex"}
        and "parent_agent_id" not in payload
    )


def _mood_authority_input_event(event):
    """Raw event that must enter the compact authority fold in ordinal order."""
    payload = event.get("payload") or {}
    return (
        structured_approval(event) is not None
        or event.get("type") == "needs_human_resolved"
        or (
            event.get("type") == "task_started"
            and event.get("source") in {"claude-code", "codex"}
            and "parent_agent_id" not in payload
        )
    )


def _mood_authority_marker(record):
    return (
        isinstance(record, dict)
        and record.get("_burrow_internal") == MOOD_AUTHORITY_KIND
    )


def _encode_mood_authority(capsule):
    """Encode logical capsule data as a shallow exact-binary64 typed graph."""
    logical = dict(capsule)
    logical.pop("_burrow_internal", None)
    graph = semantic_key(logical)
    return (
        '{"_burrow_internal":'
        + canonical_string(MOOD_AUTHORITY_KIND)
        + ',"encoding":'
        + canonical_string(MOOD_AUTHORITY_ENCODING)
        + ',"graph":'
        + graph
        + "}"
    )


def _reject_json_constant(value):
    """Capsule JSON is RFC JSON; Python's NaN/Infinity extension is not."""
    raise ValueError(f"non-standard JSON constant: {value}")


def _mood_authority_from_line_checked(line):
    try:
        too_large = (
            not isinstance(line, str)
            or len(line.encode("utf-8")) > MOOD_AUTHORITY_MAX_BYTES
        )
    except (TypeError, UnicodeError, OverflowError):
        return None
    if too_large:
        return None

    def duplicate_free_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate capsule JSON member")
            result[key] = value
        return result

    try:
        record = json.loads(
            line,
            parse_constant=_reject_json_constant,
            object_pairs_hook=duplicate_free_object,
        )
    except (TypeError, ValueError, RecursionError, OverflowError):
        return None
    if isinstance(record, dict) and record.get("encoding") == MOOD_AUTHORITY_ENCODING:
        if set(record) != {"_burrow_internal", "encoding", "graph"}:
            return None
        try:
            logical = decode_graph(record.get("graph"))
        except (TypeError, ValueError, OverflowError):
            return None
        if not isinstance(logical, dict):
            return None
        if set(logical) != {
            "events",
            "ordinals",
            "copies",
            "raw_ordinals",
            "raw_indexes",
            "raw_count",
            "overflow",
            "observed",
        }:
            return None
        record = {"_burrow_internal": MOOD_AUTHORITY_KIND, **logical}
    elif not _json_domain_within(record, MOOD_AUTHORITY_MAX_DEPTH):
        return None
    if (
        not isinstance(record, dict)
        or not _mood_authority_marker(record)
        or set(record)
        != {
            "_burrow_internal",
            "events",
            "ordinals",
            "copies",
            "raw_ordinals",
            "raw_indexes",
            "raw_count",
            "overflow",
            "observed",
        }
        or not isinstance(record.get("events"), list)
        or not isinstance(record.get("ordinals"), list)
        or len(record["ordinals"]) != len(record["events"])
        or not isinstance(record.get("overflow"), bool)
        or isinstance(record.get("observed"), bool)
        or not isinstance(record.get("observed"), (int, float))
        or not math.isfinite(record["observed"])
        or not float(record["observed"]).is_integer()
        or not 0 <= record["observed"] <= MOOD_AUTHORITY_LIMIT + 1
        or (record["overflow"] and record["observed"] != MOOD_AUTHORITY_LIMIT + 1)
        or (not record["overflow"] and record["observed"] != len(record["events"]))
        or ("copies" in record and not isinstance(record["copies"], list))
        or ("raw_ordinals" in record and not isinstance(record["raw_ordinals"], list))
        or ("raw_indexes" in record and not isinstance(record["raw_indexes"], list))
        or (
            record["overflow"]
            and (
                record["events"]
                or record["ordinals"]
                or record.get("copies", [])
                or record.get("raw_ordinals", [])
                or record.get("raw_indexes", [])
            )
        )
    ):
        return None
    events = record["events"]
    copies = record["copies"]
    raw_ordinals = record["raw_ordinals"]
    raw_indexes = record["raw_indexes"]
    raw_count = record["raw_count"]

    def valid_ordinal(value):
        return (
            isinstance(value, str)
            and re.fullmatch(r"0|[1-9][0-9]*", value)
            and int(value) <= 9007199254740991
        )

    def increasing(values):
        return all(
            int(values[index - 1]) < int(value)
            for index, value in enumerate(values)
            if index
        )

    def valid_raw_index(value):
        return (
            isinstance(value, str)
            and re.fullmatch(r"[0-9]{16}", value)
            and int(value) <= 9007199254740991
        )

    if (
        not isinstance(copies, list)
        or not isinstance(raw_ordinals, list)
        or len(raw_indexes) != len(raw_ordinals)
        or not valid_raw_index(raw_count)
        or (record["overflow"] and int(raw_count) != 0)
        or not all(valid_raw_index(item) for item in raw_indexes)
        or any(int(item) >= int(raw_count) for item in raw_indexes)
        or not increasing(raw_indexes)
        or not all(valid_ordinal(item) for item in record["ordinals"])
        or len(set(record["ordinals"])) != len(record["ordinals"])
        or not all(valid_ordinal(item) for item in copies + raw_ordinals)
        or not increasing(record["ordinals"])
        or not increasing(copies)
        or not increasing(raw_ordinals)
        or any(
            validate_event(event) or not _mood_authority_event(event)
            for event in events
        )
    ):
        return None
    return {
        "events": events,
        "ordinals": record["ordinals"],
        "copies": copies,
        "raw_ordinals": raw_ordinals,
        "raw_indexes": raw_indexes,
        "raw_count": raw_count,
        "overflow": record["overflow"],
        "observed": record["observed"],
    }


def _mood_authority_from_line(line):
    """Parse one untrusted capsule atomically; every boundary fault is invalid."""
    try:
        return _mood_authority_from_line_checked(line)
    except (RecursionError, TypeError, OverflowError, ValueError, UnicodeError):
        return None


def _canonical_identity(value):
    """Language-neutral typed structural identity with exact binary64 values."""
    return semantic_key(value)


def _capsule_identity_equal(left, right):
    """Fail closed at the recursive capsule-canonicalization boundary."""
    if not _json_domain_within(
        left, MOOD_AUTHORITY_MAX_DEPTH
    ) or not _json_domain_within(right, MOOD_AUTHORITY_MAX_DEPTH):
        return False
    try:
        return _canonical_identity(left) == _canonical_identity(right)
    except (RecursionError, TypeError, OverflowError, ValueError):
        return False


def event_ms(event):
    """Return event time in epoch ms, or zero when missing or invalid."""
    ts = event.get("ts")
    if not isinstance(ts, str):
        return 0
    try:
        when = datetime.datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return int(when.timestamp() * 1000)


def _task_event_identity(event):
    """Return the stable final tie-breaker for equal-time task transitions.

    Steward identifiers and payload text are protocol strings.  Compact JSON
    keeps array ordering significant without depending on Python's whitespace.
    """
    payload = event["payload"]
    values = (
        event["type"],
        event["ts"],
        event["agent_id"],
        event["project"],
        payload["task_id"],
        payload["title"],
        payload.get("claimant", ""),
        json.dumps(
            payload.get("required_skills", []),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        json.dumps(
            payload.get("artifacts", []), ensure_ascii=False, separators=(",", ":")
        ),
        payload.get("reason", ""),
        payload.get("parent_task_id", ""),
    )
    return "\0".join(str(value) for value in values)


def _task_tie_rank(event):
    """Constant-space semantic order for Steward's equal-ms transitions."""
    if event["type"] == "task_posted":
        return 0
    if (
        event["type"] == "task_failed"
        and event["payload"].get("reason", "").strip() == "lease_expired"
    ):
        return 1
    if event["type"] == "task_claimed":
        return 2
    if event["type"] == "task_failed":
        return 3
    return 4  # task_done


def _later_task_event(candidate, current):
    candidate_ms, current_ms = event_ms(candidate), event_ms(current)
    if candidate_ms != current_ms:
        return candidate_ms > current_ms
    candidate_rank = _task_tie_rank(candidate)
    current_rank = _task_tie_rank(current)
    if candidate_rank != current_rank:
        return candidate_rank > current_rank
    return _task_event_identity(candidate) > _task_event_identity(current)


def _remember_task_event(task, slot, index, event):
    """Mirror the browser's constant-space timestamp/tie-order fold."""
    current = task[slot]
    if current is None or _later_task_event(event, current[1]):
        task[slot] = (index, event)


def _task_keep_indexes(parsed):
    """Return the bounded cross-agent task projection needed after rotation.

    A task begins under ``steward:api`` and moves to its claimant.  It therefore
    cannot share villager lifecycle retention: a claimant's ``session_ended``
    must not erase a done/failed/reopened task or resurrect its older post.
    """

    def compare(left, right):
        left_id, left_task = left
        right_id, right_task = right
        left_event, right_event = left_task["latest"][1], right_task["latest"][1]
        left_terminal = left_event["type"] == "task_done" or (
            left_event["type"] == "task_failed"
            and left_event["payload"].get("reason", "").strip() != "lease_expired"
        )
        right_terminal = right_event["type"] == "task_done" or (
            right_event["type"] == "task_failed"
            and right_event["payload"].get("reason", "").strip() != "lease_expired"
        )
        if left_terminal != right_terminal:
            return 1 if left_terminal else -1
        if event_ms(left_event) != event_ms(right_event):
            return -1 if event_ms(left_event) > event_ms(right_event) else 1
        if left_id == right_id:
            return 0
        return -1 if left_id > right_id else 1

    tasks = {}
    for index, event in parsed:
        if event.get("type") not in TASK_EVENT_TYPES or validate_event(event):
            continue
        task_id = event["payload"]["task_id"].strip()
        task = tasks.setdefault(task_id, {"posted": None, "latest": None})
        if event["type"] == "task_posted":
            _remember_task_event(task, "posted", index, event)
        _remember_task_event(task, "latest", index, event)
        if len(tasks) > KEEP_TASKS:
            # Mirror the browser after every accepted event. A post evicted at
            # capacity cannot be silently reconstructed merely because its
            # later claim appears in the same transport/reset batch.
            selected_ids = {
                key
                for key, _ in sorted(tasks.items(), key=functools.cmp_to_key(compare))[
                    :KEEP_TASKS
                ]
            }
            tasks = {key: value for key, value in tasks.items() if key in selected_ids}

    selected = sorted(tasks.items(), key=functools.cmp_to_key(compare))[:KEEP_TASKS]
    keep = set()
    for _, task in selected:
        keep.add(task["latest"][0])
        if task["posted"] is not None:
            keep.add(task["posted"][0])
    return keep


def _approval_resolution_identity(event, shape=None):
    """The exact fields a closing event shares with its immutable request."""
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    request_id = shape.request_id if shape is not None else payload.get("request_id")
    action = shape.action if shape is not None else payload.get("action")
    values = (request_id, event.get("agent_id"), event.get("project"), action)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return None
    return semantic_key(list(values))


def _approval_lifecycle_identity(event, shape=None):
    """Steward's full immutable request identity as represented on the wire.

    ``request_id`` is Steward's global primary key.  The other fields prevent a
    corrupt/combined log from rewriting the question or offered answers behind
    that ID. JSON detail equality is semantic, not Python serialization order.
    """
    if event.get("type") != "needs_human":
        return None
    payload = event.get("payload") or {}
    shape = shape or structured_approval(event)
    if not isinstance(payload, dict) or shape is None:
        return None
    return semantic_key(
        {
            "request_id": shape.request_id,
            "agent_id": event.get("agent_id"),
            "project": event.get("project"),
            "action": shape.action,
            "detail": shape.detail,
            "options": shape.options,
            "message": shape.message,
            "expires_at": {
                "present": shape.expires_at_present,
                "value": shape.expires_at,
            },
        }
    )


def _approval_keep_indexes(parsed, capacity=KEEP_APPROVALS):
    """Return append-ordered approval authority and isolated event indexes.

    The first exact request append owns an ID and the first subsequent matching
    close resolves it. Unknown closes and later replays/conflicts are isolated
    from ordinary villager retention but deliberately not carried forward.
    """
    requests = {}
    isolated = set()
    sequence = 0
    for index, event in parsed:
        shape = None if validate_event(event) else structured_approval(event)
        if shape is not None:
            isolated.add(index)
            request_id = shape.request_id
            lifecycle = _approval_lifecycle_identity(event, shape)
            resolution_identity = _approval_resolution_identity(event, shape)
            sequence += 1
            record = requests.get(request_id)
            if record is None:
                record = {
                    "knock": (index, event),
                    "resolution": None,
                    "lifecycle": lifecycle,
                    "resolution_identity": resolution_identity,
                    "collision": None,
                    "sequence": sequence,
                }
                requests[request_id] = record
            elif lifecycle != record["lifecycle"]:
                # One retained incompatible request is enough to replay the
                # collision quarantine; append order chooses which one.
                if record["collision"] is None:
                    record["collision"] = (index, event)
            if capacity is not None and len(requests) > capacity:
                ranked = sorted(
                    requests.items(),
                    key=lambda item: (
                        item[1]["resolution"] is None,
                        (item[1]["resolution"] or item[1]["knock"])[0],
                        item[1]["sequence"],
                        item[0],
                    ),
                    reverse=True,
                )
                requests = dict(ranked[:capacity])
            continue
        if event.get("type") != "needs_human_resolved":
            continue
        isolated.add(index)
        if validate_event(event):
            continue
        payload = event.get("payload") or {}
        request_id = payload["request_id"]
        record = requests.get(request_id)
        resolution_identity = _approval_resolution_identity(event)
        if record is None or resolution_identity != record["resolution_identity"]:
            continue
        if record["collision"] is None and record["resolution"] is None:
            record["resolution"] = (index, event)
    keep = set()
    for record in requests.values():
        keep.add(record["knock"][0])
        if record["collision"] is not None:
            keep.add(record["collision"][0])
        if record["resolution"] is not None:
            keep.add(record["resolution"][0])
    return keep, isolated


def _journal_approval_keep_indexes(parsed, retained_journal_indexes):
    """Approval truth required by retained journal residents.

    A journal observation can be separated from a structured knock on either
    side by arbitrary ordinary activity and by more than the viewer's raw tail.
    Select approval authority from the complete segment, then retain every
    selected lifecycle for an agent with retained canonical journal authority.
    Pending requests override both journals and later ordinary evidence, so
    their append position relative to the journal is irrelevant.  Keeping the
    selected close/collision with its request prevents reset from resurrecting
    terminal authority.  ``_approval_keep_indexes`` remains the single owner of
    request-ID collision, close, orphan, capacity, and append-order semantics.
    """
    approval_keep, _ = _approval_keep_indexes(parsed)
    if not approval_keep:
        return set()
    journal_agents = {
        agent_id for agent_id, indexes in retained_journal_indexes.items() if indexes
    }
    eligible_request_ids = set()
    for index, event in parsed:
        if index not in approval_keep:
            continue
        shape = structured_approval(event)
        if shape is None:
            continue
        if event.get("agent_id") in journal_agents:
            eligible_request_ids.add(shape.request_id)
    return {
        index
        for index, event in parsed
        if index in approval_keep
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("request_id") in eligible_request_ids
    }


def _mood_keep_indexes(
    parsed, eligible_agents, authority_uncertain=False, overflow_state=None
):
    """Independent bounded witnesses for the pure browser mood reducer.

    These indexes are evidence only. The caller intersects agent IDs with its
    ordinary presence projection, so routine/task facts cannot manufacture a
    villager during reset.
    """
    by_agent = collections.defaultdict(list)
    for index, event in parsed:
        if event.get("agent_id") in eligible_agents and not validate_event(event):
            by_agent[event["agent_id"]].append((index, event))
    if authority_uncertain:
        # Partial approval graphs are neither useful nor truthful once the
        # global gate is durable. Keep only bounded attachment evidence; do not
        # construct an arbitrarily large graph merely to discard it below.
        minimal = set()
        for entries in by_agent.values():
            minimal.add(max(entries, key=lambda item: (event_ms(item[1]), item[0]))[0])
            minimal.add(entries[-1][0])
        return minimal
    # Mood authority is independent of the approval panel's bounded,
    # one-record-per-ID presentation. Every first append of an incompatible
    # immutable request is a canonical candidate owned by that event's agent.
    # A matching decision closes a candidate only when the append prefix makes
    # the resolution identity unambiguous.
    approval_groups = {}
    approval_by_knock_index = {}
    approval_by_resolution_index = {}
    approval_resolution_dependencies = {}
    for item in parsed:
        index, event = item
        if validate_event(event):
            continue
        shape = structured_approval(event)
        if shape is not None:
            group = approval_groups.setdefault(
                shape.request_id,
                {
                    "request_id": shape.request_id,
                    "candidates": [],
                    "by_lifecycle": {},
                    "collided": False,
                },
            )
            lifecycle = _approval_lifecycle_identity(event, shape)
            if lifecycle in group["by_lifecycle"]:
                continue
            candidate = {
                "knock": item,
                "resolution": None,
                "group": group,
                "lifecycle": lifecycle,
                "resolution_identity": _approval_resolution_identity(event, shape),
            }
            group["candidates"].append(candidate)
            group["by_lifecycle"][lifecycle] = candidate
            approval_by_knock_index[index] = candidate
            if len(group["candidates"]) > 1:
                group["collided"] = True
                for member in group["candidates"]:
                    if member["resolution"] is not None:
                        approval_by_resolution_index.pop(member["resolution"][0], None)
                        approval_resolution_dependencies.pop(
                            member["resolution"][0], None
                        )
                    member["resolution"] = None
            continue
        if event["type"] != "needs_human_resolved":
            continue
        payload = event.get("payload") or {}
        group = approval_groups.get(payload.get("request_id"))
        resolution_identity = _approval_resolution_identity(event)
        if group is None or group["collided"] or resolution_identity is None:
            continue
        matches = [
            candidate
            for candidate in group["candidates"]
            if candidate["resolution"] is None
            and candidate["resolution_identity"] == resolution_identity
        ]
        if matches:
            # A retained close must carry enough append-prefix authority to
            # remain ambiguous. Otherwise rotation can drop one matching
            # lifecycle and manufacture an exact human decision.
            approval_resolution_dependencies[index] = matches[:2]
        if len(matches) == 1:
            matches[0]["resolution"] = item
            approval_by_resolution_index[index] = matches[0]
    approval_candidates = [
        candidate
        for group in approval_groups.values()
        for candidate in group["candidates"]
    ]
    # Human interaction follows the final approval-panel authority, not Mood's
    # independent per-collision candidates. A close whose request ID collided
    # later is quarantined and cannot displace an earlier truthful decision.
    authoritative_close_indexes = {
        group["candidates"][0]["resolution"][0]
        for group in approval_groups.values()
        if len(group["candidates"]) == 1
        and group["candidates"][0]["resolution"] is not None
    }

    def complete_candidate(candidate):
        """Bounded facts that reconstruct this canonical collision member."""
        canonical = candidate["group"]["candidates"][0]
        members = [canonical, candidate]
        if candidate is canonical and len(candidate["group"]["candidates"]) > 1:
            members.append(candidate["group"]["candidates"][1])
        return {member["knock"][0]: member for member in members}.values()

    mood_approval_indexes = set()
    unresolved_by_agent = collections.defaultdict(list)
    for record in approval_candidates:
        agent_id = record["knock"][1]["agent_id"]
        if record["resolution"] is None:
            unresolved_by_agent[agent_id].append(record)
    for agent_id, entries in by_agent.items():
        anchor = max(event_ms(item[1]) for item in entries)
        oldest = min(
            unresolved_by_agent[agent_id],
            key=lambda record: (
                -max(0, anchor - event_ms(record["knock"][1])),
                record["knock"][0],
            ),
            default=None,
        )
        # Resolved predecessors live in the internal authority capsule rather
        # than an arbitrary finite raw-event stack. Raw retention needs only
        # today's oldest open lifecycle.
        for record in [oldest] if oldest is not None else []:
            if record is None:
                continue
            # Complete the whole canonical/collision group. This includes a
            # cross-agent canonical request even when only the colliding owner
            # is independently eligible for Mood projection.
            for candidate in complete_candidate(record):
                mood_approval_indexes.add(candidate["knock"][0])
                if candidate["resolution"] is not None:
                    mood_approval_indexes.add(candidate["resolution"][0])
    keep = set()
    minimal_keep = set()
    witness_overflow = False
    quarter = 15 * 60 * 1000
    day = 24 * 60 * 60 * 1000
    for agent_id, entries in by_agent.items():
        # One reverse pass provides both the unresolved plain/fallback set and
        # the append-later ordinary boundary needed by retained anchor knocks.
        open_plain = []
        boundary_after = {}
        latest_ordinary = None
        for item in reversed(entries):
            event = item[1]
            if event["type"] == "needs_human" and structured_approval(event) is None:
                if latest_ordinary is None:
                    open_plain.append(item)
                else:
                    boundary_after[item[0]] = latest_ordinary
            if event["type"] in MOOD_ORDINARY_SUPERSEDERS:
                latest_ordinary = item
        open_plain.reverse()
        anchor_item = max(entries, key=lambda item: (event_ms(item[1]), item[0]))
        minimal_keep.add(anchor_item[0])
        minimal_keep.add(entries[-1][0])
        anchor = event_ms(anchor_item[1])
        candidates = []
        # Timestamps need not follow append order. Retain the complete current
        # lower-open terminal frontier so a later anchor can expire an
        # append-later success without losing the earlier outcome it exposed.
        terminal_ring = collections.deque(maxlen=MAX_MOOD_RETAINED_PER_AGENT)
        terminal_observed = 0
        for item in entries:
            if (
                item[1]["type"] not in MOOD_TERMINAL_TYPES
                or max(0, anchor - event_ms(item[1])) >= day
            ):
                continue
            terminal_observed = min(
                MAX_MOOD_RETAINED_PER_AGENT + 1, terminal_observed + 1
            )
            terminal_ring.append(item)
        terminal = list(terminal_ring)
        if terminal_observed > MAX_MOOD_RETAINED_PER_AGENT:
            witness_overflow = True
            if overflow_state is not None:
                overflow_state["overflow"] = True
        candidates.extend(terminal)
        base_bucket = anchor // quarter
        bucket_best = {}
        for item in entries:
            weight = MOOD_WORK_WEIGHTS.get(item[1]["type"])
            bucket = event_ms(item[1]) // quarter
            if not weight or bucket < base_bucket - 7 or bucket > base_bucket:
                continue
            current = bucket_best.get(bucket)
            if current is None or weight >= current[0]:
                bucket_best[bucket] = (weight, item)
        candidates.extend(value[1] for value in bucket_best.values())
        human = []
        for item in entries:
            event = item[1]
            payload = event.get("payload") or {}
            root_prompt = (
                event["type"] == "task_started"
                and event.get("source") in {"claude-code", "codex"}
                and "parent_agent_id" not in payload
            )
            exact_close = (
                event["type"] == "needs_human_resolved"
                and item[0] in authoritative_close_indexes
            )
            if root_prompt or exact_close:
                human.append(item)
        if human:
            candidates.append(human[-1])
        # Complete only the lifecycle witnesses selected above. Selection uses
        # the canonical knock's agent, while retaining a cross-agent collision
        # at its own append position.
        candidates.extend(item for item in entries if item[0] in mood_approval_indexes)
        # A plain/fallback knock is open only if no later ordinary event
        # supersedes it. Retaining the latest such boundary is sufficient.
        if open_plain:
            candidates.append(open_plain[-1])
        # Threshold witnesses must be the exact deduplicated contributors used
        # by the reducer, rather than merely events whose types could contribute.
        contributing = {item[0]: item for item in terminal}
        contributing.update((item[0], item) for _, item in bucket_best.values())
        contributing.update((item[0], item) for item in human[-1:])
        unresolved_indexes = set()
        unresolved_indexes.update(
            record["knock"][0] for record in unresolved_by_agent[agent_id]
        )
        unresolved_indexes.update(item[0] for item in open_plain)
        contributing.update(
            (item[0], item) for item in entries if item[0] in unresolved_indexes
        )
        signal_candidates = sorted(contributing.values())
        if signal_candidates:
            if len(signal_candidates) <= 6:
                threshold = signal_candidates
            else:
                by_time = sorted(
                    signal_candidates, key=lambda item: (event_ms(item[1]), item[0])
                )
                selected = {}
                for item in [by_time[0], by_time[-1], *reversed(signal_candidates)]:
                    selected[item[0]] = item
                    if len(selected) == 6:
                        break
                threshold = sorted(selected.values())
            candidates.extend(threshold)
        # Preserve a second threshold proof without unresolved needs. A later
        # ordinary superseder or exact close can remove those contributors;
        # the backup keeps evidence sufficient across the future append.
        stable_contributing = {item[0]: item for item in terminal}
        stable_contributing.update((item[0], item) for _, item in bucket_best.values())
        stable_contributing.update((item[0], item) for item in human[-1:])
        stable_candidates = sorted(stable_contributing.values())
        if stable_candidates:
            if len(stable_candidates) <= 6:
                stable_threshold = stable_candidates
            else:
                stable_by_time = sorted(
                    stable_candidates, key=lambda item: (event_ms(item[1]), item[0])
                )
                stable_selected = {}
                for item in [
                    stable_by_time[0],
                    stable_by_time[-1],
                    *reversed(stable_candidates),
                ]:
                    stable_selected[item[0]] = item
                    if len(stable_selected) == 6:
                        break
                stable_threshold = sorted(stable_selected.values())
            candidates.extend(stable_threshold)
        candidates.append(anchor_item)
        # Keep a strict per-agent ceiling. Only Mood's selected oldest-open and
        # latest-close lifecycles enter this set; presentation capacity is not
        # part of their authority.
        selected = sorted({item[0]: item for item in candidates}.values())
        # A timestamp-anchor plain/fallback knock can already be superseded in
        # append order. Carry one later ordinary boundary for every such knock
        # retained by another Mood rule, or reset would reopen it.
        for item in list(selected):
            if (
                item[1]["type"] != "needs_human"
                or structured_approval(item[1]) is not None
            ):
                continue
            boundary = boundary_after.get(item[0])
            if boundary is not None:
                selected.append(boundary)
        selected = sorted({item[0]: item for item in selected}.values())
        if len(selected) > MAX_MOOD_RETAINED_PER_AGENT:
            witness_overflow = True
            if overflow_state is not None:
                overflow_state["overflow"] = True
        selected = selected[-MAX_MOOD_RETAINED_PER_AGENT:]
        if anchor_item not in selected:
            selected = sorted([*selected[1:], anchor_item])
        for index, _ in selected:
            keep.add(index)
    if authority_uncertain or witness_overflow:
        return minimal_keep
    # Cross-agent lifecycle members cannot be added from the projected owner's
    # per-agent list, but remain required authority for that owner's collision.
    if not authority_uncertain and not witness_overflow:
        keep.update(mood_approval_indexes)
    # Structured knocks selected for another reason (especially the greatest
    # timestamp anchor) also carry their exact close/collision lifecycle.
    if not authority_uncertain and not witness_overflow:
        for index in list(keep):
            selected = approval_by_knock_index.get(
                index
            ) or approval_by_resolution_index.get(index)
            dependencies = approval_resolution_dependencies.get(index, [])
            for record in ([selected] if selected is not None else []) + dependencies:
                for candidate in complete_candidate(record):
                    keep.add(candidate["knock"][0])
                    if candidate["resolution"] is not None:
                        keep.add(candidate["resolution"][0])
        counts = collections.Counter(
            event["agent_id"] for index, event in parsed if index in keep
        )
        if any(count > MAX_MOOD_RETAINED_PER_AGENT for count in counts.values()):
            keep = minimal_keep
            if overflow_state is not None:
                overflow_state["overflow"] = True
    return keep


def _compact_mood_authority(events):
    """Exact append-prefix approval/root authority; orphan closes are final."""
    groups = {}
    selected = set()
    latest_roots = {}
    for position, (ordinal, event) in enumerate(events):
        payload = event.get("payload") or {}
        root_prompt = (
            event.get("type") == "task_started"
            and event.get("source") in {"claude-code", "codex"}
            and "parent_agent_id" not in payload
        )
        if root_prompt:
            latest_roots[event["agent_id"]] = position
            continue
        shape = structured_approval(event)
        if shape is not None:
            group = groups.setdefault(
                shape.request_id,
                {"candidates": [], "lifecycles": set(), "collided": False},
            )
            lifecycle = _approval_lifecycle_identity(event, shape)
            if lifecycle in group["lifecycles"]:
                continue
            group["lifecycles"].add(lifecycle)
            group["candidates"].append(
                {
                    "ordinal": position,
                    "resolved": False,
                    "resolution": None,
                    "resolution_identity": _approval_resolution_identity(event, shape),
                }
            )
            selected.add(position)
            if len(group["candidates"]) > 1:
                group["collided"] = True
                for candidate in group["candidates"]:
                    if candidate["resolution"] is not None:
                        selected.discard(candidate["resolution"])
                    candidate["resolved"] = False
                    candidate["resolution"] = None
            continue
        if event.get("type") != "needs_human_resolved":
            continue
        identity = _approval_resolution_identity(event)
        group = groups.get(payload.get("request_id"))
        if identity is None or group is None or group["collided"]:
            continue
        matches = [
            candidate
            for candidate in group["candidates"]
            if not candidate["resolved"]
            and candidate["resolution_identity"] == identity
        ]
        if matches:
            # One match closes exactly; two or more retain the same ambiguity.
            selected.add(position)
        if len(matches) == 1:
            matches[0]["resolved"] = True
            matches[0]["resolution"] = position
    selected.update(latest_roots.values())
    return [item for position, item in enumerate(events) if position in selected]


def _mood_authority_overflow_tracker():
    """Bounded monotonic lower bound for irreducible Mood authority."""
    return {"roots": set(), "lifecycles": {}, "observed": 0}


def _mood_authority_tracker_add(tracker, event):
    """Return true as soon as exact authority is guaranteed to exceed 256.

    A later root replaces an earlier root for the same owner, while a distinct
    owner remains one irreducible fact. Structured lifecycle candidates are
    never removed by closes or collisions. Thus this bounded lower bound may
    delay overflow caused by close witnesses, but can never declare it early.
    """
    payload = event.get("payload") or {}
    root = (
        event.get("type") == "task_started"
        and event.get("source") in {"claude-code", "codex"}
        and "parent_agent_id" not in payload
    )
    if root:
        agent_id = event["agent_id"]
        if agent_id not in tracker["roots"]:
            tracker["roots"].add(agent_id)
            tracker["observed"] += 1
    else:
        shape = structured_approval(event)
        if shape is not None:
            lifecycles = tracker["lifecycles"].setdefault(shape.request_id, set())
            lifecycle = _approval_lifecycle_identity(event, shape)
            if lifecycle not in lifecycles:
                lifecycles.add(lifecycle)
                tracker["observed"] += 1
    return tracker["observed"] > MOOD_AUTHORITY_LIMIT


def _exact_capsule_authority(capsule, raw_events):
    """Prove a capsule is the canonical irreducible fold of its source epoch."""
    copies = set(map(int, capsule["copies"]))
    ordered = [
        (int(ordinal), event)
        for ordinal, event in zip(capsule["ordinals"], capsule["events"])
        if int(ordinal) not in copies
    ]
    for raw_index, ordinal in zip(capsule["raw_indexes"], capsule["raw_ordinals"]):
        index = int(raw_index)
        if index < len(raw_events) and _mood_authority_input_event(raw_events[index]):
            ordered.append((int(ordinal), raw_events[index]))
    ordered.sort(key=lambda item: item[0])
    if any(
        ordered[index - 1][0] == item[0] for index, item in enumerate(ordered) if index
    ):
        return False
    folded = _compact_mood_authority(ordered)
    expected = list(zip(map(int, capsule["ordinals"]), capsule["events"]))
    return len(folded) == len(expected) and all(
        left_ordinal == right_ordinal
        and _capsule_identity_equal(left_event, right_event)
        for (left_ordinal, left_event), (right_ordinal, right_event) in zip(
            folded, expected
        )
    )


def _routine_start_tie(event):
    payload = event["payload"]
    # Python strings compare by Unicode scalar value. Compare this fieldwise
    # tuple directly; it is never delimiter encoded.
    return (
        event["source"],
        event["agent_id"],
        event["project"],
        payload["routine"],
        payload["run_id"],
        payload["trigger"],
    )


def _routine_terminal_tie(event):
    payload = event["payload"]
    if event["type"] == "routine_failed":
        present = "duration_s" in payload
        return (1, payload["error"], present, payload.get("duration_s", 0))
    return (0, payload["outcome"], payload["duration_s"], tuple(payload["artifacts"]))


def _current_routine(indexed_events):
    """Select the canonical current-run authority."""
    runs = collections.defaultdict(list)
    for index, event in indexed_events:
        if event["type"].startswith("routine_"):
            payload = event["payload"]
            runs[(payload["routine"], payload["run_id"])].append((index, event))
    selected = None
    for key, facts in runs.items():
        starts = [
            (index, event)
            for index, event in facts
            if event["type"] == "routine_started"
        ]
        if not starts:
            continue
        start = min(
            starts, key=lambda item: (event_ms(item[1]), _routine_start_tie(item[1]))
        )
        terminals = [
            (index, event)
            for index, event in facts
            if event["type"] in {"routine_finished", "routine_failed"}
        ]
        terminal = (
            max(
                terminals,
                key=lambda item: (event_ms(item[1]), _routine_terminal_tie(item[1])),
            )
            if terminals
            else None
        )
        if terminal and event_ms(terminal[1]) < event_ms(start[1]):
            terminal = None
        candidate = (event_ms(start[1]), key, start, terminal)
        if selected is None or candidate[:2] > selected[:2]:
            selected = candidate
    return (
        {
            "start": selected[2],
            "terminal": selected[3],
            "event": selected[3] or selected[2],
        }
        if selected
        else None
    )


def _projection_keep_indexes(
    parsed, now_ms, limit, live_agents=None, baseline_start=None, preselected=None
):
    """Bounded append-ordered authority for the ordinary village reducer.

    Terminal/expired agents need no reset witness. Routine identity overflow is
    first bounded by newest routine append; newest live candidates are then
    admitted with indivisible state, lineage, and heartbeat-action support.
    Remaining capacity retains newest visible history up to the reducer's
    per-agent cap. This mirrors projectionWitnesses in JavaScript.
    """
    if limit <= 0:
        return set()
    preselected = set(preselected or ())
    if len(preselected) > limit:
        raise ValueError("preselected projection witnesses exceed limit")
    ignored = TASK_EVENT_TYPES | {"needs_human_resolved", "journal_written"}
    routine_agents = set()
    for _, event in reversed(parsed):
        if event["type"].startswith("routine_"):
            routine_agents.add(event["agent_id"])
            if len(routine_agents) == limit:
                break
    if baseline_start is None:
        baseline_start = -1
    latest_raw = {}
    last_nonroutine = {}
    all_routine_facts = collections.defaultdict(list)
    for index, event in parsed:
        if event["type"] not in ignored and (
            index >= baseline_start or event["agent_id"] in routine_agents
        ):
            agent_id = event["agent_id"]
            latest_raw[agent_id] = (index, event)
            if event["type"].startswith("routine_"):
                all_routine_facts[agent_id].append((index, event))
            else:
                last_nonroutine[agent_id] = (index, event)
    latest = {}
    orphan_routine_agents = set()
    for agent_id, raw in latest_raw.items():
        if not raw[1]["type"].startswith("routine_"):
            latest[agent_id] = (*raw, raw[0])
            continue
        current = _current_routine(all_routine_facts[agent_id])
        if current:
            latest[agent_id] = (*current["event"], raw[0])
        else:
            orphan_routine_agents.add(agent_id)
            if agent_id in last_nonroutine:
                latest[agent_id] = (*last_nonroutine[agent_id], raw[0])
    live = [
        (agent_id, item)
        for agent_id, item in latest.items()
        if item[1]["type"] != "session_ended" and now_ms - event_ms(item[1]) <= DROP_MS
    ]
    live.sort(key=lambda item: item[1][2], reverse=True)
    candidates = {agent_id for agent_id, _ in live}
    history = collections.defaultdict(list)
    lineage = {}
    previous_action = {}
    previous_ordinary_seen = set()
    for index, event in reversed(parsed):
        agent_id = event["agent_id"]
        if (
            agent_id not in candidates
            or event["type"] in ignored
            or (index < baseline_start and agent_id not in routine_agents)
        ):
            continue
        payload = event.get("payload") or {}
        if agent_id not in lineage and payload.get("parent_agent_id"):
            lineage[agent_id] = index
        if event["type"] != "heartbeat":
            if len(history[agent_id]) < KEEP_PER_AGENT:
                history[agent_id].append(index)
            if agent_id not in previous_ordinary_seen:
                previous_ordinary_seen.add(agent_id)
                if event["type"] in PROJECTION_ACTION_TYPES:
                    previous_action[agent_id] = index
    event_by_index = dict(parsed)
    keep = set(preselected)
    kept_per_agent = collections.Counter(
        event["agent_id"] for index, event in parsed if index in keep
    )
    admitted = []
    for agent_id, (index, event, _rank_index) in live:
        support = {index}
        if agent_id in lineage:
            support.add(lineage[agent_id])
        if event["type"] == "heartbeat" and agent_id in previous_action:
            support.add(previous_action[agent_id])
        if event["type"].startswith("routine_"):
            current = _current_routine(all_routine_facts[agent_id])
            if current:
                support.add(current["start"][0])
                if current["terminal"]:
                    support.add(current["terminal"][0])
        added = support - keep
        if len(keep) + len(added) > limit or (
            agent_id in all_routine_facts
            and kept_per_agent[agent_id] + len(added) > KEEP_PER_AGENT
        ):
            continue
        keep.update(added)
        kept_per_agent[agent_id] += len(added)
        admitted.append(agent_id)
        if live_agents is not None:
            live_agents.add(agent_id)
    optional = sorted(
        (
            index
            for agent_id in admitted
            for index in history[agent_id]
            if index not in keep
        ),
        reverse=True,
    )
    for index in optional:
        if len(keep) == limit:
            break
        event = event_by_index[index]
        if (
            event["agent_id"] in all_routine_facts
            and kept_per_agent[event["agent_id"]] >= KEEP_PER_AGENT
        ):
            continue
        keep.add(index)
        kept_per_agent[event["agent_id"]] += 1
    for index, event in reversed(parsed):
        if len(keep) == limit:
            break
        agent_id = event["agent_id"]
        if (
            agent_id not in orphan_routine_agents
            or event["type"] == "routine_started"
            or index in keep
            or kept_per_agent[agent_id] >= KEEP_PER_AGENT
        ):
            continue
        keep.add(index)
        kept_per_agent[agent_id] += 1
    return keep


def carry_forward(lines, now_ms, policy):
    """The bounded tail that preserves both village and job-board projections.

    Villager history is retained per agent.  Task lifecycle is retained per
    task ID because Steward posts centrally and emits later transitions under
    the claimant; the two lifecycles intentionally have different owners.
    """
    if policy != POLICY:
        raise ValueError("carry_forward requires the loaded retention policy")
    # Journal authority is independent from the ordinary viewer tail. Derive
    # its bounded canonical/collision selection from the complete segment
    # before clipping ordinary history. Absolute line indexes let the final
    # merge preserve append order without duplicating specialized records.
    capsule_attempts = []
    full_parsed = []
    for i, line in enumerate(lines):
        try:
            event = json.loads(line, parse_constant=_reject_json_constant)
        except (TypeError, ValueError, RecursionError, OverflowError):
            continue
        if _mood_authority_marker(event):
            capsule_attempts.append((i, _mood_authority_from_line(line)))
            continue
        if (
            not isinstance(event, dict)
            or not event.get("agent_id")
            or event.get("type") not in EVENT_TYPES
            or validate_event(event)
        ):
            continue
        full_parsed.append((i, event))
    # A capsule is all-or-nothing metadata. Copies must be a structural
    # multiset subset of both capsule authority and surrounding raw records;
    # order entries must likewise be a raw subset. Multiple capsules are
    # ambiguous and therefore ignored together.
    capsule = (
        capsule_attempts[0][1]
        if len(capsule_attempts) == 1 and capsule_attempts[0][0] == 0
        else None
    )
    if capsule is not None:
        authority_by_ordinal = dict(zip(capsule["ordinals"], capsule["events"]))
        raw_by_ordinal = {
            ordinal: full_parsed[int(raw_index)][1]
            for ordinal, raw_index in zip(
                capsule["raw_ordinals"], capsule["raw_indexes"]
            )
            if int(raw_index) < len(full_parsed)
        }
        safe = (
            int(capsule["raw_count"]) <= len(full_parsed)
            and all(int(index) < len(full_parsed) for index in capsule["raw_indexes"])
            and len(capsule["copies"]) == len(set(capsule["copies"]))
            and all(
                ordinal in authority_by_ordinal
                and ordinal in raw_by_ordinal
                and _capsule_identity_equal(
                    authority_by_ordinal[ordinal], raw_by_ordinal[ordinal]
                )
                for ordinal in capsule["copies"]
            )
        )
        intersections = set(capsule["ordinals"]) & set(capsule["raw_ordinals"])
        safe = safe and intersections == set(capsule["copies"])
        if safe and not capsule["overflow"]:
            raw_count = int(capsule["raw_count"])
            raw_events = [event for _, event in full_parsed[:raw_count]]
            safe = _exact_capsule_authority(capsule, raw_events)
            required_raw = set()
            for prefix in ([], capsule["events"]):
                proof_events = [*prefix, *raw_events]
                proof = list(enumerate(proof_events))
                proof_agents = {event["agent_id"] for event in proof_events}
                proof_overflow = {"overflow": False}
                selected = _mood_keep_indexes(
                    proof, proof_agents, overflow_state=proof_overflow
                )
                if proof_overflow["overflow"]:
                    safe = False
                    break
                offset = len(prefix)
                required_raw.update(
                    position - offset
                    for position in selected
                    if offset <= position < offset + raw_count
                )
            # Co-retained board/journal/presence records stay outside the Mood
            # list. The manifest must equal—not merely cover—the exact Mood
            # witness union, so a surplus entry cannot hide its superseder.
            safe = safe and required_raw == set(map(int, capsule["raw_indexes"]))
        if not safe:
            capsule = None
    authority_events = []
    authority_overflow = bool(capsule and capsule["overflow"])
    authority_observed = capsule["observed"] if capsule else 0
    authority_tracker = _mood_authority_overflow_tracker()
    raw_ordinals = {}
    copy_ordinals = set()
    if capsule:
        if not authority_overflow:
            authority_events.extend(
                (int(ordinal), event)
                for ordinal, event in zip(capsule["ordinals"], capsule["events"])
            )
            for _, event in authority_events:
                _mood_authority_tracker_add(authority_tracker, event)
        copy_ordinals.update(map(int, capsule["copies"]))
    maximum_ordinal = max(
        [-1]
        + [ordinal for ordinal, _ in authority_events]
        + (list(map(int, capsule["raw_ordinals"])) if capsule else [])
    )
    required_allocations = (
        max(0, len(full_parsed) - int(capsule["raw_count"]))
        if capsule and not authority_overflow
        else len(full_parsed)
    )
    ordinal_exhausted = maximum_ordinal > 9007199254740991 - required_allocations
    if ordinal_exhausted:
        # Do not assign even one inexact coordinate. Preserve raw transports,
        # but replace all partial authority/manifests with canonical durable
        # overflow. A capsule-only rotation allocates zero and stays exact.
        authority_overflow = True
        authority_observed = MOOD_AUTHORITY_LIMIT + 1
        authority_events = []
        copy_ordinals.clear()
        capsule = None
        maximum_ordinal = -1
    next_ordinal = maximum_ordinal + 1 if required_allocations else 0
    retained_ordinal_by_position = (
        {
            int(raw_index): int(ordinal)
            for raw_index, ordinal in zip(
                capsule["raw_indexes"], capsule["raw_ordinals"]
            )
        }
        if capsule
        else {}
    )
    capsule_raw_count = int(capsule["raw_count"]) if capsule else 0
    for raw_position, (index, event) in enumerate(full_parsed):
        if ordinal_exhausted:
            continue
        had_ordinal = raw_position in retained_ordinal_by_position
        ordinal = (
            retained_ordinal_by_position[raw_position] if had_ordinal else next_ordinal
        )
        if not had_ordinal:
            next_ordinal += 1
        raw_ordinals[index] = ordinal
        source_epoch_excluded = (
            capsule is not None
            and not authority_overflow
            and raw_position < capsule_raw_count
            and raw_position not in retained_ordinal_by_position
        )
        if (
            not authority_overflow
            and _mood_authority_event(event)
            and not source_epoch_excluded
        ):
            if ordinal in copy_ordinals:
                copy_ordinals.remove(ordinal)
            else:
                authority_events.append((ordinal, event))
                if _mood_authority_tracker_add(authority_tracker, event):
                    # The lower bound is monotonic, so no later close/replay can
                    # recover exact authority. Discard the partial graph now and
                    # keep subsequent work independent of authority cardinality.
                    authority_overflow = True
                    authority_observed = MOOD_AUTHORITY_LIMIT + 1
                    authority_events = []
                    copy_ordinals.clear()
    authority_events.sort(key=lambda item: item[0])
    journal_parsed = [
        (i, event) for i, event in full_parsed if event.get("type") == "journal_written"
    ]
    journal_records, _ = reduce_journal_indexed(journal_parsed)
    journal_canonical_keep = {
        record["canonical"][0] for record in journal_records.values()
    }
    journal_keep = journal_canonical_keep | {
        record["conflict"][0]
        for record in journal_records.values()
        if record["conflict"] is not None
    }
    retained_journal_indexes = collections.defaultdict(set)
    for i, event in journal_parsed:
        if i in journal_canonical_keep:
            retained_journal_indexes[event["agent_id"]].add(i)

    # Every retained journal needs the ordinary facts on both sides of the
    # short-lived overlay. Select them per canonical journal segment rather
    # than merely per agent: a resident may retain several days separated by
    # ordinary activity, and the newest event after each observation is just as
    # authoritative as its predecessor. Keeping only session_ended here would
    # let rotation resurrect journal animation over a later tool, idle, knock,
    # heartbeat, or other ordinary state.
    def projection_ordinary(event):
        event_type = event.get("type", "")
        return (
            event_type not in TASK_EVENT_TYPES
            and event_type != "journal_written"
            and event_type != "needs_human_resolved"
        )

    latest_ordinary = {}
    journal_predecessor_keep = set()
    journal_successors = {}
    active_journal = {}
    for i, event in full_parsed:
        agent_id = event.get("agent_id")
        if agent_id not in retained_journal_indexes:
            continue
        if i in retained_journal_indexes[agent_id]:
            predecessor = latest_ordinary.get(agent_id)
            if predecessor is not None:
                journal_predecessor_keep.add(predecessor[0])
            active_journal[agent_id] = i
            continue
        if not projection_ordinary(event):
            continue
        latest_ordinary[agent_id] = (i, event)
        if agent_id in active_journal:
            journal_successors[active_journal[agent_id]] = (i, event)
    journal_successor_keep = {item[0] for item in journal_successors.values()}

    # Heartbeats refresh liveness without entering the visible event history,
    # and lineage may have been declared on an older ordinary event. Preserve
    # those tiny dependencies for every selected neighbour so reset reconstructs
    # the same displayed action and resident/visitor ownership as incremental
    # folding, rather than merely the same top-level event type.
    journal_neighbour_keep = journal_predecessor_keep | journal_successor_keep
    journal_support_keep = set()
    latest_nonheartbeat = {}
    latest_lineage = {}
    for i, event in full_parsed:
        agent_id = event.get("agent_id")
        if i in journal_neighbour_keep:
            if event["type"] == "heartbeat" and agent_id in latest_nonheartbeat:
                journal_support_keep.add(latest_nonheartbeat[agent_id][0])
            if agent_id in latest_lineage:
                journal_support_keep.add(latest_lineage[agent_id][0])
        if not projection_ordinary(event):
            continue
        if event["type"] != "heartbeat":
            latest_nonheartbeat[agent_id] = (i, event)
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("parent_agent_id"):
            latest_lineage[agent_id] = (i, event)

    # A structured knock's reducer owns whether it is pending, collided, or
    # exactly resolved. Select that authority from the complete segment for
    # every retained journal resident: intervening ordinary activity must not
    # hide a request that predates the journal, even when both are outside the
    # raw tail. Resolved/collided lifecycles are retained whole so replay cannot
    # resurrect them, while orphan decisions remain excluded by the reducer.
    journal_approval_keep = _journal_approval_keep_indexes(
        full_parsed, retained_journal_indexes
    )

    # The browser's transport window remains globally bounded even when older
    # journal authority is merged into it. Reserve only protected indexes that
    # fall before the candidate tail; moving the boundary can expose another
    # protected index, so converge the tiny (at most journal-bound) frontier.
    protected_journal_indexes = (
        journal_keep
        | journal_predecessor_keep
        | journal_successor_keep
        | journal_support_keep
        | journal_approval_keep
    )
    special_agents = {
        event["agent_id"]
        for index, event in full_parsed
        if index in protected_journal_indexes
    }
    latest_special_ordinary = {}
    for index, event in full_parsed:
        if event["agent_id"] in special_agents and projection_ordinary(event):
            latest_special_ordinary[event["agent_id"]] = (index, event)
    protected_journal_indexes.update(
        index
        for index, event in latest_special_ordinary.values()
        if event["type"] == "session_ended"
    )
    projection_live_agents = set()
    raw_tail_start = max(0, len(lines) - VIEWER_LINE_LIMIT)
    # Without routine authority before the transport tail, every ordinary
    # villager fact is already carried by that tail. Avoid several complete-log
    # selector passes on the common (and approval-heavy) rotation path.
    has_pre_tail_routine = any(
        index < raw_tail_start and event["type"].startswith("routine_")
        for index, event in full_parsed
    )
    projection_source = (
        full_parsed
        if has_pre_tail_routine
        else [(index, event) for index, event in full_parsed if index >= raw_tail_start]
    )
    projection_keep = _projection_keep_indexes(
        projection_source,
        now_ms,
        VIEWER_LINE_LIMIT,
        projection_live_agents,
        raw_tail_start,
        protected_journal_indexes,
    )
    protected_projection_indexes = protected_journal_indexes | projection_keep
    tail_start = max(0, len(lines) - VIEWER_LINE_LIMIT)
    while True:
        protected_before_tail = sum(
            index < tail_start for index in protected_projection_indexes
        )
        adjusted = max(0, len(lines) - (VIEWER_LINE_LIMIT - protected_before_tail))
        if adjusted == tail_start:
            break
        tail_start = adjusted
    parsed = []
    for i in range(tail_start, len(lines)):
        line = lines[i]
        try:
            event = json.loads(line, parse_constant=_reject_json_constant)
        except (TypeError, ValueError, RecursionError, OverflowError):
            continue
        if _mood_authority_marker(event):
            continue
        if not isinstance(event, dict) or not event.get("agent_id"):
            continue
        if event.get("type") not in EVENT_TYPES or validate_event(event):
            continue
        parsed.append((i, event))
    approval_keep, _ = _approval_keep_indexes(parsed)
    retained_approval_knocks = collections.defaultdict(list)
    for i, event in parsed:
        if i in approval_keep and structured_approval(event) is not None:
            retained_approval_knocks[event["agent_id"]].append(i)
    latest_projection = {}
    for i, event in full_parsed:
        if projection_ordinary(event):
            latest_projection[event["agent_id"]] = (i, event)
    approval_terminal_keep = {
        index
        for agent_id, (index, event) in latest_projection.items()
        if event["type"] == "session_ended"
        and any(knock_i < index for knock_i in retained_approval_knocks[agent_id])
    }
    task_keep = _task_keep_indexes(parsed)
    keep = list(
        task_keep
        | approval_keep
        | journal_keep
        | journal_predecessor_keep
        | journal_successor_keep
        | journal_support_keep
        | journal_approval_keep
        | projection_keep
        | approval_terminal_keep
    )
    # Mood authority comes from the complete pre-rotation segment, but only
    # for agents whose villager projection above remains live. Task evidence
    # alone still cannot manufacture liveness; routine evidence can because it
    # is now a first-class Steward-authored villager lifecycle.
    mood_agents = set(projection_live_agents)
    mood_agents.update(
        event["agent_id"]
        for index, event in full_parsed
        if index in approval_keep
        and structured_approval(event) is not None
        and (
            event["agent_id"] not in latest_projection
            or latest_projection[event["agent_id"]][1]["type"] != "session_ended"
        )
    )
    compacted_authority = (
        [] if authority_overflow else _compact_mood_authority(authority_events)
    )
    authority_overflow = (
        authority_overflow or len(compacted_authority) > MOOD_AUTHORITY_LIMIT
    )
    dependency_state = {"overflow": False}
    mood_keep_indexes = _mood_keep_indexes(
        full_parsed, mood_agents, authority_overflow, dependency_state
    )
    authority_overflow = authority_overflow or dependency_state["overflow"]
    keep.extend(mood_keep_indexes)
    kept_indexes = sorted(set(keep))
    full_by_index = dict(full_parsed)
    retained_parsed = [
        (position, full_by_index[index])
        for position, index in enumerate(kept_indexes)
        if index in full_by_index
    ]
    retained_agents = {event["agent_id"] for _, event in retained_parsed}
    proof_positions = _mood_keep_indexes(
        retained_parsed, retained_agents, authority_overflow
    )
    combined_events = [event for _, event in compacted_authority]
    combined_events.extend(event for _, event in retained_parsed)
    combined_parsed = list(enumerate(combined_events))
    combined_agents = {event["agent_id"] for event in combined_events}
    combined_keep = _mood_keep_indexes(
        combined_parsed, combined_agents, authority_overflow
    )
    authority_prefix = len(compacted_authority)
    proof_positions.update(
        position - authority_prefix
        for position in combined_keep
        if authority_prefix <= position < len(combined_events)
    )
    retained_original = [index for index in kept_indexes if index in full_by_index]
    # The sparse transport manifest is canonical, not merely sufficient: it is
    # exactly the union selected by the raw-only and authority+all-raw folds
    # that the consumer independently recomputes over this retained epoch.
    manifest_mood_indexes = {
        retained_original[position] for position in proof_positions
    }
    authority_events = [] if authority_overflow else compacted_authority
    authority_observed = (
        MOOD_AUTHORITY_LIMIT + 1 if authority_overflow else len(authority_events)
    )
    authority_signatures = {ordinal: event for ordinal, event in authority_events}
    copies = []
    for index in [] if authority_overflow else sorted(manifest_mood_indexes):
        try:
            event = json.loads(lines[index], parse_constant=_reject_json_constant)
        except (TypeError, ValueError):
            continue
        if (
            isinstance(event, dict)
            and _mood_authority_event(event)
            and not validate_event(event)
        ):
            ordinal = raw_ordinals[index]
            if ordinal in authority_signatures and _capsule_identity_equal(
                authority_signatures[ordinal], event
            ):
                copies.append(str(ordinal))
    retained = [lines[i] for i in kept_indexes]
    retained_position = {index: position for position, index in enumerate(kept_indexes)}
    if authority_events or authority_overflow:
        capsule = (
            {
                "_burrow_internal": MOOD_AUTHORITY_KIND,
                "events": [],
                "ordinals": [],
                "copies": [],
                "raw_ordinals": [],
                "raw_indexes": [],
                "raw_count": _mood_raw_index(0),
                "overflow": True,
                "observed": MOOD_AUTHORITY_LIMIT + 1,
            }
            if authority_overflow
            else {
                "_burrow_internal": MOOD_AUTHORITY_KIND,
                "events": [event for _, event in authority_events],
                "ordinals": [str(ordinal) for ordinal, _ in authority_events],
                "copies": copies,
                "raw_ordinals": [
                    str(raw_ordinals[index])
                    for index in sorted(manifest_mood_indexes)
                    if index in full_by_index
                ],
                "raw_indexes": [
                    _mood_raw_index(retained_position[index])
                    for index in sorted(manifest_mood_indexes)
                    if index in full_by_index
                ],
                "raw_count": _mood_raw_index(len(kept_indexes)),
                "overflow": False,
                "observed": authority_observed,
            }
        )
        try:
            encoded_capsule = _encode_mood_authority(capsule)
        except (RecursionError, TypeError, OverflowError, ValueError):
            encoded_capsule = None
        if (
            encoded_capsule is None
            or len(encoded_capsule.encode("utf-8")) > MOOD_AUTHORITY_MAX_BYTES
        ):
            authority_overflow = True
            capsule = {
                "_burrow_internal": MOOD_AUTHORITY_KIND,
                "events": [],
                "ordinals": [],
                "copies": [],
                "raw_ordinals": [],
                "raw_indexes": [],
                "raw_count": _mood_raw_index(0),
                "overflow": True,
                "observed": MOOD_AUTHORITY_LIMIT + 1,
            }
            encoded_capsule = _encode_mood_authority(capsule)
        retained.insert(0, encoded_capsule)
    witnesses = {
        "tasks": frozenset(task_keep),
        "approvals": frozenset(approval_keep | journal_approval_keep),
        "journal": frozenset(
            journal_keep
            | journal_predecessor_keep
            | journal_successor_keep
            | journal_support_keep
        ),
        "projection": frozenset(projection_keep),
        "moods": frozenset(manifest_mood_indexes),
    }
    return Retention(tuple(retained), capsule, witnesses)
