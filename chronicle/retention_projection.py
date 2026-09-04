"""Projection retention: the current routine and the bounded villager tail.

The per-agent witness budget the village projection needs after rotation, plus
the routine-ordering rules it reads the current routine with.
"""

import collections

from retention_ledger import event_ms
from retention_policy import (
    BOARD_ONLY_TYPES,
    DROP_MS,
    KEEP_AMBIENT_PER_AGENT,
    KEEP_PER_AGENT,
    PROJECTION_ACTION_TYPES,
)
from village_state import AMBIENT_TYPES, STATE_NEUTRAL_TYPES, ambient_share


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
    ignored = BOARD_ONLY_TYPES | {"needs_human_resolved", "journal_written"}
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
        # Ambient events are retained as visible history below but decide nothing here:
        # the reducer reads them as somebody else's action, so a knock at a departed
        # villager's door must not make this selector treat the agent as alive and spend
        # witness budget an actually working agent needs.
        if (
            event["type"] not in ignored
            and event["type"] not in AMBIENT_TYPES | STATE_NEUTRAL_TYPES
            and (index >= baseline_start or event["agent_id"] in routine_agents)
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
    ordinary_history = collections.defaultdict(list)
    ambient_history = collections.defaultdict(list)
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
            # Both halves of the agent's visible history are collected in full here and
            # divided afterwards. Ambient events are somebody else's action filed under
            # this villager's door, and newest-wins alone would let a knock storm spend
            # the whole of an agent's budget on itself.
            bucket = (
                ambient_history if event["type"] in AMBIENT_TYPES else ordinary_history
            )
            if len(bucket[agent_id]) < KEEP_PER_AGENT:
                bucket[agent_id].append(index)
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
    # The agent's visible history, divided: the fleet's own records are served out of
    # everything but the outsider's floor, and the outsider takes whatever is genuinely
    # left — the whole budget when nobody knocked, exactly the floor when a storm and a
    # working resident both want it (warren#278).
    history = {}
    for agent_id in admitted:
        ordinary, ambient = ordinary_history[agent_id], ambient_history[agent_id]
        take_ordinary, take_ambient = ambient_share(
            len(ordinary), len(ambient), KEEP_PER_AGENT, KEEP_AMBIENT_PER_AGENT
        )
        history[agent_id] = ordinary[:take_ordinary] + ambient[:take_ambient]
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
