"""Mood retention: the durable authority capsule and the mood witnesses.

The largest and most self-contained family: parsing and encoding the private
mood-authority capsule, the per-agent mood witness selection, the compaction
fold and the overflow tracker. It imports the two approval identities and
``event_ms``; nothing imports it but ``retention`` itself.
"""

import collections
import json
import math
import re

from approval_protocol import structured_approval
from mood_policy import MOOD_TERMINAL_TYPES, MOOD_WORK_WEIGHTS
from protocol import validate_event
from typed_json import canonical_string, decode_graph, semantic_key
from retention_approvals import (
    _approval_lifecycle_identity,
    _approval_resolution_identity,
)
from retention_ledger import event_ms
from retention_policy import (
    MAX_MOOD_RETAINED_PER_AGENT,
    MOOD_AUTHORITY_ENCODING,
    MOOD_AUTHORITY_KIND,
    MOOD_AUTHORITY_LIMIT,
    MOOD_AUTHORITY_MAX_BYTES,
    MOOD_AUTHORITY_MAX_DEPTH,
    MOOD_ORDINARY_SUPERSEDERS,
)


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


# ``_burrow_internal`` is a stored member name, not a vocabulary word, and is
# deliberately left at its pre-rename spelling. It identifies the reserved
# capsule that rotation writes into the event log to carry mood authority across
# the cut, so archives and any live log written before this rename still contain
# it — and the readers below match the member set exactly, meaning a renamed key
# would not degrade, it would make those capsules unparseable and silently drop
# the mood identity they exist to preserve. Renaming it is a schema-version
# change, in the same class as the X-Burrow-* wire headers.
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


def _mood_keep_indexes(
    parsed, eligible_agents, authority_uncertain=False, overflow_state=None
):
    """Independent bounded witnesses for retained mood evidence.

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
