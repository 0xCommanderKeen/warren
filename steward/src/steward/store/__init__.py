"""The durable store behind steward's API: jobs, approvals, and the request log."""

from steward.claims import ResidentClaim
from steward.runs import RUN_CHAT, RUN_DELEGATED, RUN_KINDS, RUN_ROUTINE, RUN_TASK, RUN_TRIGGERS
from steward.runs import TRIGGER_MANUAL as RUN_TRIGGER_MANUAL
from steward.runs import TRIGGER_SCHEDULE as RUN_TRIGGER_SCHEDULE
from steward.store._connection import _Connection
from steward.store._legacy import _LegacyTables
from steward.store.approvals import _ApprovalTables
from steward.store.board import _BoardTables
from steward.store.records import (
    APPROVAL_DECISIONS,
    DECIDED_BY_EXPIRY,
    DECIDED_BY_REPEAT,
    JOB_STATUSES,
    ORIGIN_UNATTRIBUTED,
    STATUS_OPEN,
    ApprovalRecord,
    JobRecord,
    LedgerEntry,
    OpenRun,
    OperatorRecord,
    OriginSpend,
    PauseRecord,
    RequestRecord,
    WatchdogAttempt,
    default_db_path,
)
from steward.store.records import STATUS_CLAIMED as STATUS_CLAIMED
from steward.store.records import STATUS_DONE as STATUS_DONE
from steward.store.records import STATUS_FAILED as STATUS_FAILED
from steward.store.records import STATUS_PENDING as STATUS_PENDING
from steward.store.records import STATUS_RESOLVED as STATUS_RESOLVED
from steward.store.records import new_id as new_id
from steward.store.requests import _RequestTables

__all__ = [
    "APPROVAL_DECISIONS",
    "DECIDED_BY_EXPIRY",
    "DECIDED_BY_REPEAT",
    "JOB_STATUSES",
    "ORIGIN_UNATTRIBUTED",
    "RUN_CHAT",
    "RUN_DELEGATED",
    "RUN_KINDS",
    "RUN_ROUTINE",
    "RUN_TASK",
    "RUN_TRIGGERS",
    "RUN_TRIGGER_MANUAL",
    "RUN_TRIGGER_SCHEDULE",
    "STATUS_OPEN",
    "ApprovalRecord",
    "JobRecord",
    "LedgerEntry",
    "OpenRun",
    "OperatorRecord",
    "OriginSpend",
    "PauseRecord",
    "RequestRecord",
    "ResidentClaim",
    "Store",
    "WatchdogAttempt",
    "default_db_path",
]


class Store(_ApprovalTables, _BoardTables, _LegacyTables, _RequestTables, _Connection):
    """The one durable memory the API writes to. Safe to share across threads."""
