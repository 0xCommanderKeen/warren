"""Approval retention: request identity, close pairing and bounded authority.

``_approval_keep_indexes`` remains the single owner of request-ID collision,
close, orphan, capacity and append-order semantics; the mood family imports the
two identity functions rather than restating them.
"""

from approval_protocol import structured_approval
from protocol import validate_event
from typed_json import semantic_key
from retention_policy import KEEP_APPROVALS

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
