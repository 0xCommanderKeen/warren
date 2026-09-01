"""Pure authoritative projection from validated evidence to wire-ready Village State.

``project_village`` is the domain seam.  It performs no I/O and returns only
JSON values, so transport and storage cannot leak reducer implementation state.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from collections import defaultdict

from protocol import validate_event


SCHEMA_VERSION = 1
NAMES = (
    "Bramble",
    "Poppy",
    "Wren",
    "Sorrel",
    "Fern",
    "Alder",
    "Maple",
    "Rowan",
    "Thistle",
    "Clover",
    "Hazel",
    "Juniper",
    "Moss",
    "Reed",
    "Tansy",
    "Willow",
)
CHARS = (
    "Villager",
    "Villager2",
    "Villager3",
    "Villager4",
    "Villager5",
    "Woman",
    "Boy",
    "OldMan",
    "Princess",
    "Hunter",
    "Noble",
    "Monk",
)
ACCENTS = ("#7d5ba6", "#4f7d5b", "#a65b5b", "#5b7da6", "#a68a4f", "#5ba69b")
#: Events filed under a villager that are somebody *else's* action, recorded against the
#: door they knocked on. They ride along in that villager's history and are never
#: evidence it is alive, present, or doing anything: they cannot create a villager, keep
#: one in the village, decide its state, refresh its clock, or age its mood. A stranger
#: messaging a resident's chat bot at three in the morning must not make the village show
#: that resident at work.
AMBIENT_TYPES = frozenset({"chat_message_dropped"})
#: How Steward opens a row on the board. It has two doors and one table: a job posted to
#: the open board, and a job handed to one named resident. Both write the same open,
#: unclaimed record in Steward's own store, so both open a row here — the delegated one
#: carrying an addressee and no required skills, because nobody else may pick it up.
TASK_ORIGIN_TYPES = frozenset({"task_posted", "task_delegated"})
#: Everything the ``tasks`` ledger folds. A transition for a task whose origin this log
#: never saw is dropped rather than inventing the job it belongs to.
TASK_LEDGER_TYPES = TASK_ORIGIN_TYPES | {"task_claimed", "task_done", "task_failed"}


@dataclasses.dataclass(frozen=True)
class ProjectionPolicy:
    stale_seconds: int = 30 * 60
    absent_seconds: int = 12 * 60 * 60
    events_per_villager: int = 40
    villagers: int = 256
    artifacts: int = 30
    tasks: int = 200
    approvals: int = 200
    journals: int = 200
    routines: int = 200
    diagnostics: int = 200
    #: How much of ``diagnostics`` an outsider may fill. A knock is the one diagnostic
    #: somebody outside the fleet causes, so without a share of its own a knock storm
    #: decides what an operator can see — the malformed events and approval collisions
    #: this channel exists for would age out behind it (warren#278).
    ambient_diagnostics: int = 40


def _instant(value):
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(dt.UTC)


def _wire_time(value):
    return _instant(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _identity(agent_id):
    number = int.from_bytes(
        hashlib.sha256(agent_id.encode("utf-8")).digest()[:8], "big"
    )
    return (
        NAMES[number % len(NAMES)],
        CHARS[number % len(CHARS)],
        ACCENTS[number % len(ACCENTS)],
    )


def _bounded_diagnostics(diagnostics, policy):
    """Keep the newest of the channel, with the outsider's share taken out of its own cap.

    Every other collection here is bounded by dropping the oldest, which is right when the
    fleet is what fills them. `chat_message_dropped` is the exception: it is the one record
    an outsider causes, and newest-wins alone hands a knock storm the power to age out the
    complaints an operator reads this channel for. So knocks are bounded first, at their
    own smaller cap, and everything else divides what is left — capped at the whole channel
    when nobody has knocked, which is the ordinary case.

    Both halves keep their newest records and the result is returned in append order, so a
    snapshot still reads oldest to newest and the same log always gives the same answer.

    Steward bounds the same storm at the other end by emitting one record per stranger per
    door per window (warren#278). This half is what holds when that one is outrun — by a
    scanner rotating sender ids, by a daemon that restarted, or by a Steward too old to
    have a limiter at all.
    """
    ambient_cap = min(policy.ambient_diagnostics, policy.diagnostics)
    ambient, ordinary = [], []
    for index, record in enumerate(diagnostics):
        bucket = ambient if record.get("kind") in AMBIENT_TYPES else ordinary
        bucket.append(index)
    kept = set(ambient[-ambient_cap:] if ambient_cap else ())
    room = policy.diagnostics - len(kept)
    kept.update(ordinary[-room:] if room else ())
    return [record for index, record in enumerate(diagnostics) if index in kept]


def _resident_indexes(manifests):
    exact, projects = {}, {}
    for manifest in manifests or ():
        if (
            not isinstance(manifest, dict)
            or not manifest.get("valid")
            or manifest.get("manifest_version") != 1
        ):
            continue
        match = manifest.get("match") or manifest.get("meta") or {}
        if match.get("agent_id"):
            exact.setdefault(match["agent_id"], manifest)
        if match.get("project"):
            project = match["project"]
            projects[project] = manifest if project not in projects else None
    return exact, projects


def _event_record(event):
    return {
        key: event[key]
        for key in ("v", "ts", "source", "agent_id", "project", "type", "payload")
        if key in event
    } | ({"cwd": event["cwd"]} if "cwd" in event else {})


def _description(event):
    payload = event["payload"]
    kind = event["type"]
    if kind == "task_started":
        return f"took up a task: “{payload.get('prompt', '…')}”"
    if kind == "tool_called":
        return f"using {payload.get('tool', 'tool')}"
    if kind == "tool_failed":
        return f"{payload.get('tool', 'tool')} failed"
    if kind == "artifact_produced":
        return f"crafted {payload.get('artifact', 'something')}"
    if kind == "heartbeat":
        tool = payload.get("tool")
        return f"finished {tool}" if tool else "working"
    if kind == "needs_human":
        return f"needs you: {payload.get('message', '(no message)')}"
    if kind == "session_ended":
        return "went home"
    if kind in {"idle", "routine_finished"}:
        return "finished, resting"
    if kind == "task_delegated":
        return (
            f"handed “{payload.get('title', '…')}” to {payload.get('to', 'somebody')}"
        )
    if kind == "task_session_finished":
        return f"reported back on “{payload.get('title', '…')}” after losing the claim"
    if kind == "resident_restarted":
        return (
            f"was restarted (attempt {payload.get('attempt', '?')}): "
            f"{payload.get('reason', 'no reason given')}"
        )
    return kind.replace("_", " ")


def _place(event):
    if event["type"] != "tool_called":
        return None
    return {
        "Read": "library",
        "Grep": "library",
        "Glob": "library",
        "WebSearch": "library",
        "WebFetch": "library",
        "Bash": "workshop",
        "Write": "workshop",
        "Edit": "workshop",
        "Email": "post-office",
        "Inbox": "post-office",
        "Agent": "delegation",
        "Task": "delegation",
    }.get(event["payload"].get("tool"))


def _mood(agent_id, indexed_history, approvals):
    events = [event for _, event in indexed_history]
    anchor_ms = int(max(_instant(event["ts"]).timestamp() for event in events) * 1000)
    outcomes = [
        event
        for event in events
        if event["type"]
        in {
            "tool_failed",
            "routine_failed",
            "task_failed",
            "heartbeat",
            "routine_finished",
            "task_done",
        }
        and anchor_ms - int(_instant(event["ts"]).timestamp() * 1000)
        < 24 * 60 * 60 * 1000
    ]
    failures = {"tool_failed", "routine_failed", "task_failed"}
    failure_count = min(3, sum(event["type"] in failures for event in outcomes))
    streak = 0
    for event in reversed(outcomes):
        if event["type"] not in failures:
            break
        streak = min(3, streak + 1)
    weights = {
        "task_started": 3,
        "task_claimed": 3,
        "routine_started": 3,
        "artifact_produced": 2,
        "journal_written": 2,
        "tool_called": 1,
        "heartbeat": 1,
    }
    buckets = {}
    for event in events:
        weight = weights.get(event["type"])
        bucket = int(_instant(event["ts"]).timestamp()) // (15 * 60)
        if weight and anchor_ms // (15 * 60 * 1000) - 7 <= bucket <= anchor_ms // (
            15 * 60 * 1000
        ):
            buckets[bucket] = max(weight, buckets.get(bucket, 0))
    density = sum(buckets.values())
    workload_level = (
        "unobserved"
        if not buckets
        else "light"
        if density <= 6
        else "active"
        if density <= 14
        else "heavy"
        if density <= 20
        else "saturated"
    )
    root_prompts = [
        event
        for event in events
        if event["type"] == "task_started"
        and event["source"] in {"claude-code", "codex"}
        and "parent_agent_id" not in event["payload"]
    ]
    latest_human = root_prompts[-1] if root_prompts else None
    human_age = (
        anchor_ms - int(_instant(latest_human["ts"]).timestamp() * 1000)
        if latest_human
        else None
    )
    pending = [
        item
        for item in approvals
        if item["agent_id"] == agent_id and item["state"] == "pending"
    ]
    oldest = min(pending, key=lambda item: item["opened_at"], default=None)
    need_age = (
        anchor_ms - int(_instant(oldest["opened_at"]).timestamp() * 1000)
        if oldest
        else None
    )
    enough = bool(oldest) or streak >= 2 or len(events) >= 6
    score = ([2, -1, -3, -5][streak] if outcomes else 0) + (
        2 if workload_level == "active" else -2 if workload_level == "saturated" else 0
    )
    score += 1 if human_age is not None and human_age <= 60 * 60 * 1000 else 0
    score += (
        0
        if need_age is None
        else -1
        if need_age <= 60 * 60 * 1000
        else -3
        if need_age <= 6 * 60 * 60 * 1000
        else -5
    )
    glyph, status = ("?", "not enough observed")
    if enough and need_age is not None and need_age > 6 * 60 * 60 * 1000:
        glyph, status = "!", "blocked"
    elif enough and streak == 3:
        glyph, status = "×", "repeated failures"
    elif enough and workload_level == "saturated":
        glyph, status = "▲", "overloaded"
    elif enough and score >= 4:
        glyph, status = "●", "steady"
    elif enough and score >= 1:
        glyph, status = "◆", "active"
    elif enough:
        glyph, status = "◇", "watchful"
    return {
        "agent_id": agent_id,
        "anchor": _wire_time(dt.datetime.fromtimestamp(anchor_ms / 1000, dt.UTC)),
        "anchorMs": anchor_ms,
        "glyph": glyph,
        "status": status,
        "enoughEvidence": enough,
        "authority": {"complete": True},
        "score": score,
        "evidence": {"count": min(6, len(events)), "spanMs": 0},
        "signals": {
            "failure": {
                "observed": bool(outcomes),
                "streak": streak if outcomes else None,
                "failures": failure_count if outcomes else None,
                "failuresLabel": str(failure_count) if outcomes else "unobserved",
            },
            "workload": {
                "observed": bool(buckets),
                "density": density if buckets else None,
                "level": workload_level,
                "buckets": [
                    {"bucket": key, "weight": buckets[key]} for key in sorted(buckets)
                ],
            },
            "interaction": {
                "observed": latest_human is not None,
                "level": "recent"
                if human_age is not None and human_age <= 60 * 60 * 1000
                else "old"
                if latest_human
                else "unobserved",
                "logAgeMs": human_age,
                "kind": "root prompt" if latest_human else None,
            },
            "unresolvedNeed": {
                "observed": oldest is not None,
                "state": "pending" if oldest else "none observed in retained authority",
                "logAgeMs": need_age,
                "kind": "structured request" if oldest else None,
                "request_id": oldest["request_id"] if oldest else None,
            },
        },
    }


def _opened_row(event):
    """The row Steward's two doors open onto, told apart by which one it came through.

    A posted job names the skills it needs and nobody in particular; a handed-over one
    names a resident and no skills, because an addressee is the stronger requirement.
    The identifiers here are the village's own: Steward's store keys a handoff by
    *resident* id, while the event carries the agent id, which is what a later claim
    can be compared against.
    """
    payload = event["payload"]
    handed_over = event["type"] == "task_delegated"
    return {
        "id": payload["task_id"],
        "title": payload["title"],
        "state": "open",
        "required_skills": [] if handed_over else list(payload["required_skills"]),
        "posted_by": payload["from"] if handed_over else payload["posted_by"],
        "assignee": payload["to"] if handed_over else None,
        "claimant": None,
        "updated_at": event["ts"],
    }


def _approval_shape(event):
    payload = event["payload"]
    request_id = payload.get("request_id")
    if (
        event["type"] != "needs_human"
        or not isinstance(request_id, str)
        or not request_id.strip()
    ):
        return None
    return {
        "request_id": request_id,
        "agent_id": event["agent_id"],
        "project": event["project"],
        "state": "pending",
        "message": payload.get("message", "(no message)"),
        "action": payload.get("action"),
        "detail": payload.get("detail"),
        "options": payload.get("options", []),
        "expires_at_present": "expires_at" in payload,
        "expires_at": payload.get("expires_at"),
        "opened_at": event["ts"],
    }


def project_village(
    events,
    resident_manifests,
    evaluated_at,
    policy=None,
    *,
    cursor="",
    generation=0,
    capabilities=None,
):
    """Project append-ordered evidence into one deterministic bounded snapshot."""
    policy = policy or ProjectionPolicy()
    now = _instant(evaluated_at)
    resident_report = (
        resident_manifests if isinstance(resident_manifests, dict) else None
    )
    resident_manifests = (
        resident_report.get("residents", []) if resident_report else resident_manifests
    )
    valid, diagnostics = [], list((resident_report or {}).get("diagnostics", []))
    for ordinal, event in enumerate(events or ()):
        error = validate_event(event)
        if error:
            diagnostics.append(
                {"kind": "malformed_event", "ordinal": ordinal, "message": error}
            )
            continue
        valid.append((ordinal, event))

    by_agent = defaultdict(list)
    for ordinal, event in valid:
        by_agent[event["agent_id"]].append((ordinal, event))
    exact, projects = _resident_indexes(resident_manifests)

    approvals, approval_by_id = [], {}
    tasks, task_by_id = [], {}
    journals, journal_by_key = [], {}
    routines, routine_by_run = [], {}
    artifacts = []
    for ordinal, event in valid:
        payload, kind = event["payload"], event["type"]
        if kind == "artifact_produced":
            artifacts.append(
                {
                    "agent_id": event["agent_id"],
                    "project": event["project"],
                    "artifact": payload["artifact"],
                    "ts": event["ts"],
                }
            )
        if kind in TASK_LEDGER_TYPES:
            task_id = payload["task_id"]
            record = task_by_id.get(task_id)
            if kind in TASK_ORIGIN_TYPES:
                record = _opened_row(event)
                task_by_id[task_id] = record
                tasks.append(record)
            elif record:
                record["claimant"] = payload["claimant"]
                record["state"] = {
                    "task_claimed": "claimed",
                    "task_done": "done",
                    "task_failed": (
                        "open" if payload.get("reason") == "lease_expired" else "failed"
                    ),
                }[kind]
                record["updated_at"] = event["ts"]
        shape = _approval_shape(event)
        if shape:
            previous = approval_by_id.get(shape["request_id"])
            if previous and any(
                previous.get(key) != shape.get(key)
                for key in (
                    "agent_id",
                    "project",
                    "message",
                    "action",
                    "detail",
                    "options",
                    "expires_at_present",
                    "expires_at",
                )
            ):
                previous["state"] = "collision"
                diagnostics.append(
                    {"kind": "approval_collision", "request_id": shape["request_id"]}
                )
            elif not previous:
                approval_by_id[shape["request_id"]] = shape
                approvals.append(shape)
        elif kind == "needs_human_resolved":
            record = approval_by_id.get(payload["request_id"])
            if (
                record
                and record["state"] != "collision"
                and record["agent_id"] == event["agent_id"]
                and record["project"] == event["project"]
                and record["action"] == payload["action"]
            ):
                record.update(
                    state="resolved",
                    decision=payload["decision"],
                    resolved_at=event["ts"],
                )
            else:
                diagnostics.append(
                    {
                        "kind": "orphan_approval_resolution",
                        "request_id": payload["request_id"],
                    }
                )
        if kind == "chat_message_dropped":
            # A knock nobody answered is only visible if the village says so, and the
            # villager's own history is not enough: a resident may have no villager at
            # all when a stranger finds its bot. Named fields only — the record itself
            # is carried into that villager's history exactly as it arrived, which is
            # why Steward keeps what the stranger said out of it.
            #
            # `suppressed` is how many other knocks this one record stands for: Steward
            # records one per stranger per door per window and counts the rest into it
            # (warren#278), so the number of knocks is one more than this. Absent from a
            # Steward older than the limiter, which emitted every knock and counted none.
            diagnostics.append(
                {
                    "kind": kind,
                    "agent_id": event["agent_id"],
                    "project": event["project"],
                    "route": payload["route"],
                    "address": payload["address"],
                    "from": payload["from"],
                    "reason": payload["reason"],
                    "suppressed": payload.get("suppressed", 0),
                    "ts": event["ts"],
                }
            )
        if kind == "journal_written":
            key = (payload["day"], event["agent_id"])
            if key not in journal_by_key:
                record = {
                    "day": payload["day"],
                    "agent_id": event["agent_id"],
                    "project": event["project"],
                    "source": event["source"],
                    "routine": payload["routine"],
                    "path": payload["path"],
                    "observed_at": event["ts"],
                }
                journal_by_key[key] = record
                journals.append(record)
            else:
                diagnostics.append(
                    {
                        "kind": "journal_collision",
                        "day": payload["day"],
                        "agent_id": event["agent_id"],
                    }
                )
        if kind.startswith("routine_"):
            run_id = payload["run_id"]
            record = routine_by_run.get(run_id)
            if kind == "routine_started":
                record = {
                    "run_id": run_id,
                    "routine": payload["routine"],
                    "agent_id": event["agent_id"],
                    "project": event["project"],
                    "source": event["source"],
                    "state": "running",
                    "trigger": payload["trigger"],
                    "started_at": event["ts"],
                    "updated_at": event["ts"],
                    "outcome": None,
                    "duration_s": None,
                    "artifacts": [],
                    "error": None,
                }
                routine_by_run[run_id] = record
                routines.append(record)
            elif record:
                record["state"] = "finished" if kind == "routine_finished" else "failed"
                record["updated_at"] = event["ts"]
                record["outcome"] = payload.get("outcome")
                record["duration_s"] = payload.get("duration_s")
                record["artifacts"] = list(payload.get("artifacts", []))
                record["error"] = payload.get("error")
            else:
                diagnostics.append(
                    {"kind": "orphan_routine_terminal", "run_id": run_id}
                )

    villagers = []
    pending_by_agent = defaultdict(list)
    for approval in approvals:
        if approval["state"] == "pending":
            pending_by_agent[approval["agent_id"]].append(approval)
    for agent_id in sorted(by_agent):
        history = by_agent[agent_id]
        # Three readings of one log: what this villager *did* decides its state and its
        # clock, what it did other than beat decides the line shown, and everything but
        # the beats — a knock at its door included — is the history worth keeping.
        evidence = [item for item in history if item[1]["type"] not in AMBIENT_TYPES]
        if not evidence:
            continue
        last = evidence[-1][1]
        visible_history = [item for item in history if item[1]["type"] != "heartbeat"]
        acted = [item for item in evidence if item[1]["type"] != "heartbeat"]
        visible_last = acted[-1][1] if acted else last
        age = (now - _instant(last["ts"])).total_seconds()
        pending = pending_by_agent[agent_id]
        if not pending and (
            last["type"] == "session_ended" or age > policy.absent_seconds
        ):
            continue
        manifest = exact.get(agent_id)
        has_parent_lineage = any(
            "parent_agent_id" in item["payload"] for _, item in history
        )
        if manifest is None and not has_parent_lineage:
            manifest = projects.get(last["project"])
        generated_name, generated_char, generated_accent = _identity(agent_id)
        meta = (manifest or {}).get("meta") or (manifest or {}).get("soul") or {}
        state = (
            "knocking"
            if pending or last["type"] == "needs_human"
            else (
                "resting"
                if last["type"] in {"idle", "routine_finished", "needs_human_resolved"}
                else "failed"
                if last["type"] in {"tool_failed", "routine_failed", "task_failed"}
                else "stale"
                if age > policy.stale_seconds
                else "working"
            )
        )
        recent = [item for _, item in visible_history[-policy.events_per_villager :]]
        mood = _mood(agent_id, evidence, approvals)
        resident = manifest is not None
        villagers.append(
            {
                "id": agent_id,
                "name": meta.get("name", generated_name),
                "char": meta.get("char", generated_char),
                "accent": meta.get("accent", generated_accent),
                "residency": "resident" if resident else "visitor",
                "home": manifest.get("home") if resident else None,
                "base": "home" if resident else "lodge",
                "resident_file": manifest.get("file") if resident else None,
                "state": state,
                "project": last["project"],
                "cwd": last.get("cwd", ""),
                "last_ts": last["ts"],
                "last_line": _description(visible_last),
                "place": _place(visible_last)
                if state in {"working", "stale"}
                else None,
                "lineage": {
                    key: last["payload"][key]
                    for key in ("parent_agent_id", "agent_type")
                    if key in last["payload"]
                },
                "history": [_event_record(item) for item in recent],
                "mood": mood,
                "pending_approval_ids": [item["request_id"] for item in pending],
            }
        )

    residents = sorted(
        (dict(item) for item in resident_manifests or () if isinstance(item, dict)),
        key=lambda item: (item.get("home", 999), str(item.get("file", ""))),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generation": int(generation),
        "cursor": str(cursor),
        "evaluated_at": _wire_time(now),
        "villagers": villagers[-policy.villagers :],
        "residents": residents,
        "diagnostic_residents": list(
            (resident_report or {}).get("diagnostic_residents", [])
        ),
        "artifacts": artifacts[-policy.artifacts :],
        "tasks": tasks[-policy.tasks :],
        "approvals": approvals[-policy.approvals :],
        "journals": journals[-policy.journals :],
        "routines": routines[-policy.routines :],
        "diagnostics": _bounded_diagnostics(diagnostics, policy),
        "capacity": {
            "villagers": policy.villagers,
            "events_per_villager": policy.events_per_villager,
            "tasks": policy.tasks,
            "approvals": policy.approvals,
            "journals": policy.journals,
            "routines": policy.routines,
            "diagnostics": policy.diagnostics,
        },
        "capabilities": dict(capabilities or {}),
    }
