"""Bounded latest runtime observations, separate from durable semantic history.

Session IDs come from the runner, never from the delivery process. Sequence is
allocated under the stable file lock; wall time is evidence, not delivery time.
"""

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import time
import uuid

try:
    from hooks import durable
except ImportError:
    import durable

MAX_PRESENCE = 128
MAX_SESSIONS = 4096
PRESENCE_SECONDS = 60
HEALTH_SECONDS = 30
DELAY_SECONDS = 10
ACTIVE = frozenset({"task_started", "tool_called", "heartbeat", "artifact_produced"})
STATES = {
    "idle": "resting",
    "session_ended": "ended",
    "needs_human": "knocking",
    "tool_failed": "failed",
}


def read(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return {}


@contextlib.contextmanager
def transaction(path):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read(path)
        yield state
        durable.publish_staged(((durable.stage_json(path, state), path),))


def session_key(agent, session, producer=""):
    return hashlib.sha256(json.dumps([producer, agent, session]).encode()).hexdigest()


def admit(state, observation, producer=""):
    """Fence sessions independently of replaceable slots; reject when capacity is full.

    Fences never expire or get evicted implicitly. A saturated store sacrifices
    new presence, explicitly, rather than forgetting that a session has ended.
    """
    sessions = state.setdefault("sessions", {})
    key = session_key(observation["agent_id"], observation["session_id"], producer)
    prior = sessions.get(key)
    if prior and (
        prior["state"] == "ended"
        or observation["epoch"] != prior["epoch"]
        or observation["sequence"] <= prior["sequence"]
        or observation["observed_at"] < prior["observed_at"]
    ):
        return False
    if prior is None and len(sessions) >= MAX_SESSIONS:
        state["presence_overflow"] = state.get("presence_overflow", 0) + 1
        return False
    sessions[key] = {
        name: observation[name]
        for name in ("agent_id", "state", "epoch", "sequence", "observed_at")
    }
    return True


def observe(path, event, session_id):
    kind = event["type"]
    if kind not in ACTIVE and kind not in STATES:
        return
    with transaction(path) as state:
        state.setdefault("producer", uuid.uuid4().hex)
        if len(event["agent_id"]) > 256:
            state["presence_overflow"] = state.get("presence_overflow", 0) + 1
            return
        session_id = hashlib.sha256(session_id.encode()).hexdigest()
        records = state.setdefault("presence", {})
        key = json.dumps([event["agent_id"], session_id])
        previous = state.get("sessions", {}).get(
            session_key(event["agent_id"], session_id), {}
        )
        # A session cannot become active again after its terminal observation.
        if previous.get("state") == "ended":
            return
        observed = datetime.datetime.fromisoformat(
            event["ts"].replace("Z", "+00:00")
        ).timestamp()
        sequence = max(time.time_ns(), previous.get("sequence", 0) + 1)
        observation = dict(
            agent_id=event["agent_id"],
            session_id=session_id,
            sequence=sequence,
            observed_at=observed,
            epoch=previous.get("epoch", sequence),
            source=event["source"],
            project=event["project"][:256],
            state=(
                "resting"
                if kind == "heartbeat"
                and event.get("payload", {}).get("phase") == "stop"
                else "working"
                if kind in ACTIVE
                else STATES[kind]
            ),
        )
        if not admit(state, observation):
            return
        records[key] = observation
        while len(records) > MAX_PRESENCE:
            del records[min(records, key=lambda key: records[key]["sequence"])]
            state["presence_overflow"] = state.get("presence_overflow", 0) + 1


def report(path):
    with transaction(path) as state:
        state.setdefault("producer", uuid.uuid4().hex)
        return state.copy()
