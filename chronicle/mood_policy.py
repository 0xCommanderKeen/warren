"""Shared outcome and work evidence for mood projection and retention.

Keep this domain policy independent of both reducers: rotation must preserve
the same evidence that determines the visible mood. Retention bounds and
authority encoding remain in retention_policy.
"""

MOOD_FAILURE_TYPES = frozenset({"tool_failed", "routine_failed", "task_failed"})
MOOD_TERMINAL_TYPES = MOOD_FAILURE_TYPES | {
    "heartbeat",
    "routine_finished",
    "task_done",
}
MOOD_WORK_WEIGHTS = {
    "task_started": 3,
    "task_claimed": 3,
    "routine_started": 3,
    "artifact_produced": 2,
    "journal_written": 2,
    "tool_called": 1,
    "heartbeat": 1,
}
