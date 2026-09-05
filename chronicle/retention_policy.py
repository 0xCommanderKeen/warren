"""The loaded retention policy and every bound derived from it.

A leaf module: every selector family imports its own slice of these constants,
and nothing here imports a selector. Splitting the families out of
``retention.py`` (warren#342) is what makes that direction enforceable.
"""

import json
from pathlib import Path
from types import MappingProxyType

from protocol import EVENT_TYPES as PROTOCOL_EVENT_TYPES


_POLICY_PATH = Path(__file__).with_name("retention-policy.json")
POLICY = MappingProxyType(json.loads(_POLICY_PATH.read_text(encoding="utf-8")))

EVENT_TYPES = set(PROTOCOL_EVENT_TYPES)
KEEP_PER_AGENT = POLICY["events_per_agent"]
#: How much of an agent's budget somebody *else* is guaranteed, and all they get when it is
#: contested. A knock is an ordinary event for every bound in this file, and the one event
#: an outsider causes, so without a share of its own a knock storm ages a resident's own
#: tools, tasks and sessions out of the village early (warren#278). The split itself is
#: :func:`village_state.ambient_share`, imported rather than repeated for the reason
#: :data:`AMBIENT_TYPES` is: rotation and the reducer disagreeing here would mean discarding
#: history the snapshot would have shown.
KEEP_AMBIENT_PER_AGENT = POLICY["ambient_events_per_agent"]
VIEWER_LINE_LIMIT = POLICY["viewer_line_limit"]
DROP_MS = POLICY["drop_ms"]
KEEP_TASKS = POLICY["tasks"]
KEEP_APPROVALS = POLICY["approvals"]
#: Board facts that are nobody's visible activity. Steward emits them under the villager
#: they concern, but they animate no one and keep no one in the village, so they never
#: spend a witness slot an agent that is really working needs. ``task_delegated`` is
#: deliberately absent: a handoff is the delegator's own action and reads as a line in its
#: history (warren#276), so it stays ordinary evidence here while still opening a row in
#: the ledger — see ``TASK_LEDGER_TYPES`` for the fold it belongs to.
BOARD_ONLY_TYPES = frozenset(
    {"task_posted", "task_claimed", "task_done", "task_failed"}
)
PROJECTION_ACTION_TYPES = {"task_started", "tool_called", "artifact_produced"}
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
