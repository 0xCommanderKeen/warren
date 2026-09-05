"""Idempotent DDL applied on every database open."""

from collections.abc import Mapping

# The resident path segment is authoritative; collection writes (creation) carry
# detail.resident instead. Keep this expression identical in the index and query.
_REQUEST_RESIDENT = """CASE WHEN substr(path, 1, 11) = '/residents/'
    THEN substr(substr(path, 12), 1, instr(substr(path, 12) || '/', '/') - 1)
    ELSE CASE WHEN json_type(detail, '$.resident') = 'text'
        THEN json_extract(detail, '$.resident') END END"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    task_id          TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    detail           TEXT NOT NULL DEFAULT '',
    required_skills  TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'open',
    posted_by        TEXT NOT NULL,
    claimant         TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    request_id   TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    project      TEXT NOT NULL,
    action       TEXT NOT NULL,
    message      TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '{}',
    options      TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'pending',
    decision     TEXT,
    decided_by   TEXT,
    decided_at   TEXT,
    edit         TEXT,
    expires_at   TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    received_at  TEXT NOT NULL,
    method       TEXT NOT NULL,
    path         TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '{}',
    approval_id  TEXT
);

-- Ascending timestamp plus implicit rowid supports a reverse scan of both tie keys.
CREATE INDEX IF NOT EXISTS requests_received ON requests(received_at);
CREATE INDEX IF NOT EXISTS requests_routine_received
    ON requests(json_extract(detail, '$.routine'), received_at)
    WHERE json_type(detail, '$.routine') = 'text';

CREATE TABLE IF NOT EXISTS approval_announcements (
    request_id    TEXT PRIMARY KEY REFERENCES approvals(request_id),
    claimed_by    TEXT,
    claimed_until TEXT,
    announced_at  TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    effects_at    TEXT,
    effects_claimed_by TEXT,
    effects_claimed_until TEXT,
    effects_attempts INTEGER NOT NULL DEFAULT 0,
    effects_next_attempt_at TEXT
);

CREATE TABLE IF NOT EXISTS run_ledger (
    entry_id      TEXT PRIMARY KEY,
    resident      TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    trigger       TEXT NOT NULL DEFAULT '',
    run_id        TEXT NOT NULL,
    ref           TEXT NOT NULL DEFAULT '',
    origin        TEXT NOT NULL DEFAULT '',
    outcome       TEXT NOT NULL DEFAULT '',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0.0,
    duration_s    REAL NOT NULL DEFAULT 0.0,
    usage_known   INTEGER NOT NULL DEFAULT 1,
    recorded_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS run_ledger_window
    ON run_ledger (resident, recorded_at);

CREATE TABLE IF NOT EXISTS budget_pauses (
    resident    TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    budget      TEXT NOT NULL,
    spent       REAL NOT NULL,
    cap         REAL NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    request_id  TEXT,
    window_end  TEXT NOT NULL DEFAULT '',
    paused_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_allowances (
    resident   TEXT PRIMARY KEY,
    until      TEXT NOT NULL,
    granted_by TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    granted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchdog_attempts (
    resident        TEXT PRIMARY KEY,
    attempts        INTEGER NOT NULL DEFAULT 0,
    reason          TEXT NOT NULL DEFAULT '',
    last_attempt_at TEXT,
    next_attempt_at TEXT,
    gave_up_at      TEXT
);

CREATE TABLE IF NOT EXISTS watchdog_passes (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_pass_at  TEXT NOT NULL,
    passes        INTEGER NOT NULL DEFAULT 0,
    interventions INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS unbracketed_runs (
    run_id     TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL,
    routine    TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    closed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS open_runs (
    run_id     TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'routine',
    trigger    TEXT NOT NULL DEFAULT '',
    agent_id   TEXT NOT NULL,
    project    TEXT NOT NULL DEFAULT '',
    ref        TEXT NOT NULL DEFAULT '',
    timeout_s  REAL NOT NULL DEFAULT 0.0,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL DEFAULT '',
    event_log_path TEXT NOT NULL DEFAULT '',
    evidence_version INTEGER NOT NULL DEFAULT 1,
    owner_token TEXT NOT NULL DEFAULT '',
    resident_id TEXT NOT NULL DEFAULT '',
    session_credential_sha256 TEXT NOT NULL DEFAULT '',
    terminal_event TEXT,
    terminal_event_id TEXT,
    terminal_claimed_at TEXT,
    terminal_published_at TEXT,
    closed_at  TEXT
);

CREATE INDEX IF NOT EXISTS open_runs_still_open
    ON open_runs (closed_at, started_at);

CREATE TABLE IF NOT EXISTS resident_claims (
    resident_id  TEXT PRIMARY KEY,
    token        TEXT NOT NULL,
    holder       TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT '',
    ref          TEXT NOT NULL DEFAULT '',
    run_id       TEXT NOT NULL DEFAULT '',
    claimed_at   TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    released_at  TEXT
);

CREATE TABLE IF NOT EXISTS operator_credentials (
    name        TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    digest      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    issued_at   TEXT NOT NULL,
    revoked_at  TEXT
);

CREATE INDEX IF NOT EXISTS operator_credentials_live
    ON operator_credentials (digest, revoked_at);

CREATE TABLE IF NOT EXISTS chat_recipients (
    bot          TEXT NOT NULL,
    conversation TEXT NOT NULL,
    resident_uid TEXT NOT NULL,
    PRIMARY KEY (bot, conversation)
);
"""

#: Columns added after the first database was written. Applied with ``ALTER TABLE`` at
#: open time, because a steward that has been running since phase 3 already has a
#: ``steward.db`` full of real jobs and a migration that drops it is a migration that
#: loses work. Every one of these is nullable or defaulted, so an old row stays legible.
_ADDED_COLUMNS: Mapping[str, Mapping[str, str]] = {
    "jobs": {
        "claimed_at": "TEXT",
        "lease_expires_at": "TEXT",
        "finished_at": "TEXT",
        "outcome": "TEXT",
        "reason": "TEXT",
        "artifacts": "TEXT NOT NULL DEFAULT '[]'",
        # Delegation (steward #7). A delegated item is a job addressed to one resident:
        # same table, same lease, same three closing events. ``assignee`` is what makes
        # it a letter rather than a notice — a row with one is never on the open board.
        "assignee": "TEXT",
        "delegated_by": "TEXT",
        "route": "TEXT",
        "parent_task_id": "TEXT",
        "origin": "TEXT",
        "depth": "INTEGER NOT NULL DEFAULT 0",
        # The currently leased attempt.  These are cleared together when the attempt
        # finishes or is swept; a retry therefore cannot inherit its predecessor's run.
        "run_id": "TEXT",
        "owner_token": "TEXT",
        "lease_duration_s": "REAL",
        # A terminal delegated task is also the durable reply owed to its sender.
        "final_message": "TEXT NOT NULL DEFAULT ''",
        "reply_delivered_at": "TEXT",
    },
    "approvals": {
        "resident": "TEXT NOT NULL DEFAULT ''",
        "delivered_at": "TEXT",
        # A decision that opened a door, and the write that spent it (warren#437). One
        # approval is one edit, so the write claims the row before it touches the tree and
        # gives it back if it refuses; ``consumed_by`` is the claimant's own request id,
        # which is what makes the release safe to attribute.
        "consumed_at": "TEXT",
        "consumed_by": "TEXT NOT NULL DEFAULT ''",
    },
    "approval_announcements": {
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at": "TEXT",
        "effects_at": "TEXT",
        "effects_claimed_by": "TEXT",
        "effects_claimed_until": "TEXT",
        "effects_attempts": "INTEGER NOT NULL DEFAULT 0",
        "effects_next_attempt_at": "TEXT",
    },
    "requests": {
        "approval_id": "TEXT",
    },
    "run_ledger": {
        # Denormalized from the task the run came off (steward #45). Rolling spend up by
        # joining ``ref`` to ``jobs.task_id`` guessed: a routine whose ref happens to
        # equal some task's id would have inherited that task's bill. The row says what
        # it descends from, so the ledger is self-describing and a join cannot misread it.
        "origin": "TEXT NOT NULL DEFAULT ''",
        "trigger": "TEXT NOT NULL DEFAULT ''",
    },
    "open_runs": {
        "trigger": "TEXT NOT NULL DEFAULT ''",
        # Who the session belongs to, and what it may prove that with (steward #41). The
        # resident id rather than only ``agent_id``, because ``agent_id`` is burrow's join
        # key and two manifests may declare the same one, while the sender of a delegated
        # letter has to resolve to exactly one manifest.
        "resident_id": "TEXT NOT NULL DEFAULT ''",
        "session_credential_sha256": "TEXT NOT NULL DEFAULT ''",
        "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
        "event_log_path": "TEXT NOT NULL DEFAULT ''",
        "evidence_version": "INTEGER NOT NULL DEFAULT 0",
        "owner_token": "TEXT NOT NULL DEFAULT ''",
        "terminal_event": "TEXT",
        "terminal_event_id": "TEXT",
        "terminal_claimed_at": "TEXT",
        "terminal_published_at": "TEXT",
        # Where the final message went, for a routine that ``deliver:``s (warren#385).
        "delivery": "TEXT",
        "delivery_reason": "TEXT NOT NULL DEFAULT ''",
    },
}

#: Indexes over columns that arrived after the first schema, and so can only be created
#: once :meth:`Store._add_missing_columns` has added them. ``approvals_denials`` is what
#: keeps the repeat-deny guard (:mod:`steward.transitions.approval`) a lookup rather than a
#: table scan on every knock: the table has grown one row per ask since phase 3.
#: ``jobs_lineage`` is what :meth:`Store.lineage` walks down: without it, finding a task's
#: children is a scan of the whole board once per level of the tree.
_LATE_INDEXES = """
CREATE INDEX IF NOT EXISTS approvals_denials
    ON approvals (resident, action, decided_at);
CREATE INDEX IF NOT EXISTS requests_approval
    ON requests (approval_id, outcome);
CREATE INDEX IF NOT EXISTS jobs_lineage
    ON jobs (parent_task_id);
CREATE INDEX IF NOT EXISTS open_runs_session_credential
    ON open_runs (session_credential_sha256);
"""
