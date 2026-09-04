"""The village's bounded memory after event-log rotation.

This module owns ``carry_forward`` — the one pass that runs every selector
family in order and merges the indexes they keep — plus the retained-log value
it returns and the fold that reuses it. The selectors themselves live in
sibling ``retention_*`` modules, one per family (warren#342); they are
re-exported below so ``retention.<name>`` keeps resolving for callers and tests.
Server transport and archive mechanics stay in ``serve.py``.
"""

import collections
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from approval_protocol import structured_approval
from journal_observations import reduce_indexed as reduce_journal_indexed
from protocol import validate_event

# The reducer's own sets, imported rather than mirrored: retention and the projection
# disagreeing about what counts as evidence of life — or about what opens a board row —
# is precisely the bug this prevents.
from village_state import AMBIENT_TYPES, STATE_NEUTRAL_TYPES
from retention_approvals import (
    _approval_keep_indexes,
    _journal_approval_keep_indexes,
)
from retention_ledger import _paired_transition_keep_indexes, _task_keep_indexes
from retention_mood import (
    _capsule_identity_equal,
    _compact_mood_authority,
    _encode_mood_authority,
    _exact_capsule_authority,
    _mood_authority_event,
    _mood_authority_from_line,
    _mood_authority_marker,
    _mood_authority_overflow_tracker,
    _mood_authority_tracker_add,
    _mood_keep_indexes,
    _mood_raw_index,
    _reject_json_constant,
)
from retention_policy import (
    BOARD_ONLY_TYPES,
    EVENT_TYPES,
    MOOD_AUTHORITY_KIND,
    MOOD_AUTHORITY_LIMIT,
    MOOD_AUTHORITY_MAX_BYTES,
    POLICY,
    VIEWER_LINE_LIMIT,
)
from retention_projection import _projection_keep_indexes

# Re-exports. ``event_ms`` is part of this module's public four-name interface, and
# the rest are private selectors the family test modules reach as ``retention._x``;
# both keep resolving here so the split stayed a move rather than a rename.
from retention_ledger import event_ms  # noqa: F401
from retention_approvals import _approval_lifecycle_identity  # noqa: F401
from retention_mood import _mood_authority_input_event  # noqa: F401
from retention_policy import (  # noqa: F401
    KEEP_AMBIENT_PER_AGENT,
    KEEP_APPROVALS,
    KEEP_PER_AGENT,
    KEEP_TASKS,
    MOOD_AUTHORITY_MAX_DEPTH,
)


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


class ProjectionFold:
    """Rebuildable bounded evidence for repeated authoritative projections.

    JSONL and the private mood-authority capsule remain retention details; callers
    exchange parsed protocol records only.
    """

    def __init__(self):
        self._lines = []
        self._current = []

    def replace(self, events, evaluated_at):
        current = list(events)
        lines = [json.dumps(event, ensure_ascii=False) for event in current]
        retained, _ = self._compact(lines, evaluated_at)
        self._lines = retained
        self._current = current
        return list(current)

    def extend(self, events, evaluated_at):
        events = list(events)
        if not events:
            return list(self._current)
        lines = self._lines + [
            json.dumps(event, ensure_ascii=False) for event in events
        ]
        retained, current = self._compact(lines, evaluated_at)
        self._lines = retained
        self._current = current
        return list(current)

    def _compact(self, lines, evaluated_at):
        now_ms = int(evaluated_at.timestamp() * 1000)
        retained = list(carry_forward(lines, now_ms, POLICY).lines)
        return retained, self._events(retained)

    @staticmethod
    def _events(lines):
        events = []
        for line in lines:
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                event = None
            if isinstance(event, dict) and "_burrow_internal" in event:
                continue
            events.append(event)
        return events


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
            event_type not in BOARD_ONLY_TYPES
            and event_type not in AMBIENT_TYPES
            and event_type not in STATE_NEUTRAL_TYPES
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
    # Identity is folded outside visible history. Keep its latest declaration/retirement
    # per agent independently of the ordinary per-villager tail, just like task authority.
    active_declaration = {}
    latest_identity = {}
    for index, event in full_parsed:
        if event["type"] == "resident_declared":
            active_declaration[event["agent_id"]] = (index, event)
            latest_identity[event["agent_id"]] = index
        elif event["type"] == "resident_retired":
            active = active_declaration.get(event["agent_id"])
            if active is not None and all(
                event["payload"][field] == active[1]["payload"][field]
                for field in ("resident_id", "uid")
            ):
                active_declaration.pop(event["agent_id"], None)
                latest_identity[event["agent_id"]] = index
    protected_journal_indexes.update(latest_identity.values())
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
    # Last, because every selector above can retain a handoff: it opens a board row *and*
    # is the delegator's own activity, so villager, journal and mood retention each keep
    # one on their own terms — including one whose row the ledger evicted at capacity.
    # An origin retained alone reads as an open, unclaimed job, which is a lie about work
    # that is claimed or already closed. Pair each with the newest transition on its row.
    paired_transition_keep = _paired_transition_keep_indexes(full_parsed, set(keep))
    task_keep |= paired_transition_keep
    keep.extend(paired_transition_keep)
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

