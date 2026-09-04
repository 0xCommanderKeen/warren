"""Rotation vectors rescued from the test file warren#79 deleted wholesale.

These fixtures are not JavaScript debris. `tests/test_rotation.py` — a Python
test, 89 methods — read every one of them, and warren#79 removed that file in the
same commit that removed the Node suites, replacing it with ~115 lines of
`test_village_state.py`. The fixtures were left on disk with nothing reading them.

Their subject survived intact: `carry_forward`, `Retention.witnesses`,
`_mood_keep_indexes`, `_mood_authority_from_line` and `MOOD_AUTHORITY_MAX_DEPTH`
are all still live in `retention.py`, which is the largest module in the service
and has no dedicated test file. So the vectors go back to work rather than being
deleted with the suite that used to read them — the same call
`tests/test_typed_json.py::RetiredParityVectors` made for the capsule fixture.

Only the Python half is restored here. The deleted tests also shelled out to
`viewer/projection.js` and `viewer/moods.js` for cross-language parity; that half
died with the viewer (warren#219) and is not reconstructed. Every assertion below
was checked against current behavior before being written — no fixture and no
code was edited to make them agree.

`mood-authority-order.json`, `mood-future-sufficiency.json`,
`mood-grouped-unrelated.json`, `mood-lifecycle-ambiguity.json`,
`mood-rotation.json`, `mood-rotation-adversarial.json` and
`mood-rotation-regressions.json` are still unread: their deleted tests leaned on
the Node half or on rotation scaffolding that needs more than a fixture to
restore. They are kept, not wired, and named in warren#250 as follow-up.

`task-ledger.json` is the one fixture here that was not rescued: it was recorded
for warren#277, when a delegated job started opening a board row and rotation had
to learn that a handoff is one of the two events that open one.
"""

import copy
import datetime
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import retention
from village_state import project_village


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def epoch_ms(stamp):
    return int(
        datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000
    )


class ProjectionWitnessVectorTests(unittest.TestCase):
    """``retention-parity.json`` against ``carry_forward``'s witness selection."""

    def test_recorded_projection_witnesses_hold(self):
        matrix = load("retention-parity.json")
        self.assertTrue(matrix, "the parity fixture must not be empty")
        for item in matrix:
            with self.subTest(item["name"]):
                lines = [json.dumps(event) for event in item["events"]]
                result = retention.carry_forward(lines, item["now"], retention.POLICY)
                self.assertEqual(
                    item["projection_witnesses"],
                    sorted(result.witnesses["projection"]),
                )

    def test_a_departed_villager_contributes_no_projection_evidence(self):
        """The second vector is the only cover this path has.

        `test_village_state.py::test_rotation_is_observationally_invisible` only
        ever walks the live-agent branch, so without this vector a `session_ended`
        agent could start carrying witnesses forward unnoticed.
        """
        departed = next(
            item for item in load("retention-parity.json") if not item["projection_witnesses"]
        )
        self.assertEqual(
            "session_ended", departed["events"][-1]["type"], "vector still departs"
        )
        result = retention.carry_forward(
            [json.dumps(event) for event in departed["events"]],
            departed["now"],
            retention.POLICY,
        )
        self.assertEqual(frozenset(), result.witnesses["projection"])

    def test_an_ambient_knock_cannot_resurrect_a_departed_villager(self):
        """Retention's liveness must agree with the reducer's (warren#276).

        `village_state` reads a `chat_message_dropped` as somebody else's action, so it
        decides no state and cannot keep a villager. If rotation still counted one as the
        agent's latest evidence, a departed villager would spend witness budget here that
        the projection has no use for — and under pressure that budget is taken from an
        agent that really is working.
        """
        knocked = next(
            item
            for item in load("retention-parity.json")
            if item["events"][-1]["type"] == "chat_message_dropped"
        )
        result = retention.carry_forward(
            [json.dumps(event) for event in knocked["events"]],
            knocked["now"],
            retention.POLICY,
        )
        self.assertEqual(frozenset(), result.witnesses["projection"])


class TaskLedgerVectorTests(unittest.TestCase):
    """``task-ledger.json`` against the board half of ``carry_forward`` (warren#277).

    Rotation keeps the job board alive by keeping exactly two lines per task: the
    event that opened the row and the newest transition on it. Steward opens a row
    two ways — it posts a job, or it hands one to a named resident — so a
    delegation is an origin here. Keep the claim without it and the row vanishes
    from the village entirely, taking the delegated work with it.
    """

    def rotate(self, vector):
        lines = [json.dumps(item, separators=(",", ":")) for item in vector["events"]]
        return retention.carry_forward(lines, epoch_ms(vector["now"]), retention.POLICY)

    def events_in(self, retained):
        """The retained log as the projection reads it: the capsule is not an event."""
        records = [json.loads(line) for line in retained.lines]
        return [item for item in records if "_burrow_internal" not in item]

    def test_recorded_task_witnesses_hold(self):
        vectors = load("task-ledger.json")
        self.assertTrue(vectors, "the task fixture must not be empty")
        for vector in vectors:
            with self.subTest(vector["name"]):
                self.assertEqual(
                    vector["task_witnesses"],
                    sorted(self.rotate(vector).witnesses["tasks"]),
                )

    def test_the_rotated_log_still_projects_the_recorded_board(self):
        """The witnesses are only worth anything if the board survives them."""
        for vector in load("task-ledger.json"):
            with self.subTest(vector["name"]):
                retained = self.events_in(self.rotate(vector))
                self.assertEqual(
                    vector["board"],
                    project_village(retained, [], vector["now"])["tasks"],
                )
                self.assertEqual(
                    vector["board"],
                    project_village(vector["events"], [], vector["now"])["tasks"],
                    "the recorded board is what the complete log says too",
                )

    def test_capacity_never_leaves_a_row_reading_open_while_it_is_claimed(self):
        """The eviction path is where half a row would show up.

        A handoff is retained twice over — as the row's origin and as the
        delegator's own activity — so the ledger evicting a row at capacity does
        not remove its opening event from the log. Alone, that event reads as an
        open job nobody has taken, which is exactly the lie the village forbids.

        Generated rather than recorded: the boundary is `KEEP_TASKS` handoffs plus
        one, and a fixture spelling 50 events out would hide the one number that
        matters behind them.
        """
        events = []
        for index in range(retention.KEEP_TASKS + 1):
            stamp = f"2026-08-26T12:{index:02d}:00.000Z"
            events.append(
                {
                    "v": 0,
                    "ts": stamp,
                    "source": "steward",
                    "agent_id": "claude-code:hob",
                    "project": "life",
                    "type": "task_delegated",
                    "payload": {
                        "task_id": f"t{index}",
                        "title": f"Handoff {index}",
                        "from": "claude-code:hob",
                        "to": "codex:keeper",
                        "route": "inbox",
                        "parent_task_id": None,
                        "depth": 1,
                    },
                }
            )
            events.append(
                {
                    "v": 0,
                    "ts": stamp,
                    "source": "steward",
                    "agent_id": "codex:keeper",
                    "project": "life",
                    "type": "task_claimed",
                    "payload": {
                        "task_id": f"t{index}",
                        "title": f"Handoff {index}",
                        "claimant": "codex:keeper",
                    },
                }
            )
        ledger = retention._task_keep_indexes(list(enumerate(events)))
        self.assertEqual("t0", events[0]["payload"]["task_id"])
        self.assertNotIn(0, ledger, "the oldest row is the one evicted at capacity")
        self.assertNotIn(1, ledger, "and its claim goes with it")

        now = "2026-08-26T13:00:00.000Z"
        rotated = self.rotate({"events": events, "now": now})
        retained = self.events_in(rotated)
        board = project_village(retained, [], now)["tasks"]
        # All 25, not 24: the delegator's own history keeps every handoff, so the row
        # the ledger evicted comes back — whole, with the claim that moved it. The
        # ledger's capacity bounds its own selection, not what the board can show.
        self.assertEqual(retention.KEEP_TASKS + 1, len(board))
        for row in board:
            self.assertEqual("claimed", row["state"])
            self.assertEqual("codex:keeper", row["claimant"])
            self.assertEqual("codex:keeper", row["assignee"])
            self.assertEqual("claude-code:hob", row["posted_by"])

        # Rotating what rotation produced must be a no-op: the pairing pass runs after
        # the mood witnesses are chosen, so its lines have to leave the durable
        # capsule's manifest recomputing to the same answer.
        again = retention.carry_forward(
            list(rotated.lines), epoch_ms(now), retention.POLICY
        )
        self.assertEqual(list(rotated.lines), list(again.lines))

    def test_a_restated_handoff_cannot_reopen_a_row_the_ledger_evicted(self):
        """The pairing pass owes a retained origin its newest *transition*.

        It used to take the newest event of any kind, which is the restated handoff
        itself — pairing the origin with itself and retaining nothing. The row the
        ledger evicted at capacity then came back through the delegator's own history
        as an open job, with the claim that moved it left behind (warren#282).
        """

        def handoff(task_id, stamp, title, to):
            return {
                "v": 0,
                "ts": stamp,
                "source": "steward",
                "agent_id": "claude-code:hob",
                "project": "life",
                "type": "task_delegated",
                "payload": {
                    "task_id": task_id,
                    "title": title,
                    "from": "claude-code:hob",
                    "to": to,
                    "route": "inbox",
                    "parent_task_id": None,
                    "depth": 1,
                },
            }

        events = []
        for index in range(retention.KEEP_TASKS + 1):
            task_id, title = f"t{index}", f"Handoff {index}"
            stamp = f"2026-08-26T12:{index:02d}:%02d.000Z"
            events.append(handoff(task_id, stamp % 0, title, "codex:keeper"))
            events.append(
                {
                    "v": 0,
                    "ts": stamp % 20,
                    "source": "steward",
                    "agent_id": "codex:keeper",
                    "project": "life",
                    "type": "task_claimed",
                    "payload": {
                        "task_id": task_id,
                        "title": title,
                        "claimant": "codex:keeper",
                    },
                }
            )
            # The second origin, and the newest event on the row by a clear margin.
            events.append(handoff(task_id, stamp % 40, title, "codex:hunter"))

        now = "2026-08-26T13:00:00.000Z"
        rotated = self.rotate({"events": events, "now": now})
        board = project_village(self.events_in(rotated), [], now)["tasks"]
        self.assertEqual(
            project_village(events, [], now)["tasks"],
            board,
            "rotation must show the board the whole log shows",
        )
        self.assertEqual(retention.KEEP_TASKS + 1, len(board))
        for row in board:
            self.assertEqual("claimed", row["state"], row["id"])
            self.assertEqual("codex:keeper", row["claimant"], row["id"])
            self.assertEqual("codex:hunter", row["assignee"], row["id"])

    def test_a_replayed_post_does_not_buy_capacity_for_a_finished_row(self):
        """The ledger reads a row's standing off its newest *transition*.

        It used to read the newest event of any kind. A post replayed after the
        ``task_done`` it does not undo is newer than that close, so a finished row
        stopped sorting as finished and kept its place at capacity — pushing a
        genuinely open row off the board to hold a job nobody is doing (warren#282).
        """

        def post(minute, task_id, title):
            return {
                "v": 0,
                "ts": f"2026-08-26T12:{minute:02d}:00.000Z",
                "source": "steward",
                "agent_id": "steward:api",
                "project": "life",
                "type": "task_posted",
                "payload": {
                    "task_id": task_id,
                    "title": title,
                    "required_skills": [],
                    "posted_by": "steward:api",
                },
            }

        # t0 is closed early and posted again late, so it is the newest row on the
        # board by timestamp and the only finished one. Everything else is open.
        events = [
            post(0, "t0", "Job 0"),
            {
                "v": 0,
                "ts": "2026-08-26T12:01:00.000Z",
                "source": "steward",
                "agent_id": "codex:keeper",
                "project": "life",
                "type": "task_done",
                "payload": {
                    "task_id": "t0",
                    "title": "Job 0",
                    "claimant": "codex:keeper",
                    "artifacts": [],
                },
            },
        ]
        open_rows = retention.KEEP_TASKS - 1
        events.extend(
            post(minute, f"t{minute - 1}", f"Job {minute - 1}")
            for minute in range(2, 2 + open_rows)
        )
        events.append(post(30, "t0", "Job 0, restated"))
        # The row that tips the ledger over capacity, and the newest of them all.
        events.append(post(31, f"t{retention.KEEP_TASKS}", "The last job"))

        keep = retention._task_keep_indexes(list(enumerate(events)))
        self.assertEqual(
            set(range(2, 2 + open_rows)) | {len(events) - 1},
            keep,
            "capacity gives up the finished row, not the oldest open one",
        )


class MoodCapsuleDomainVectorTests(unittest.TestCase):
    """``mood-capsule-malformed.json`` against the capsule reader.

    A capsule that fails to decode is dropped silently, taking Mood authority
    history with it, so every rejection here is a silent-data-loss boundary.
    """

    def setUp(self):
        self.matrix = load("mood-capsule-malformed.json")
        self.base = {
            "_burrow_internal": retention.MOOD_AUTHORITY_KIND,
            "events": [self.matrix["authority_event"]],
            "ordinals": ["0"],
            "copies": [],
            "raw_ordinals": [],
            "raw_indexes": [],
            "raw_count": "0" * 16,
            "overflow": False,
            "observed": 1,
        }

    def encode(self, capsule):
        return json.dumps(capsule, separators=(",", ":"))

    def test_the_unmutated_capsule_is_accepted(self):
        """Anchors every rejection below: the base must decode, or they are free."""
        self.assertIsNotNone(
            retention._mood_authority_from_line(self.encode(self.base))
        )

    def test_every_recorded_invalid_field_mutation_is_rejected(self):
        self.assertTrue(self.matrix["invalid_mutations"])
        for field, value in self.matrix["invalid_mutations"]:
            with self.subTest(field=field, value=repr(value)[:40]):
                malformed = copy.deepcopy(self.base)
                malformed[field] = value
                self.assertIsNone(
                    retention._mood_authority_from_line(self.encode(malformed))
                )

    def test_nonstandard_observed_spellings_are_rejected_but_integral_is_not(self):
        encoded = self.encode(self.base)
        for token in self.matrix["nonstandard_observed_tokens"]:
            with self.subTest(token=token):
                line = encoded.replace('"observed":1', f'"observed":{token}')
                self.assertIsNone(retention._mood_authority_from_line(line))
        integral = encoded.replace('"observed":1', '"observed":1.0')
        decoded = retention._mood_authority_from_line(integral)
        self.assertIsNotNone(decoded, "1.0 is the same count as 1")
        self.assertEqual([self.matrix["authority_event"]], decoded["events"])

    def test_structural_depth_bound_matches_the_recorded_constant(self):
        """The fixture is the only thing pinning `MOOD_AUTHORITY_MAX_DEPTH`."""
        self.assertEqual(
            self.matrix["max_structural_depth"], retention.MOOD_AUTHORITY_MAX_DEPTH
        )

    def test_detail_nesting_is_accepted_and_rejected_exactly_at_the_boundary(self):
        def capsule(containers):
            value = "leaf"
            for _ in range(containers):
                value = [value]
            authority = copy.deepcopy(self.matrix["authority_event"])
            authority["type"] = "needs_human"
            authority["payload"] = {"message": "Deep request", "detail": value}
            return {**self.base, "events": [authority]}

        accepted = self.matrix["accepted_detail_containers"]
        rejected = self.matrix["rejected_detail_containers"]
        self.assertEqual(accepted + 1, rejected, "the vectors straddle one boundary")
        self.assertIsNotNone(
            retention._mood_authority_from_line(self.encode(capsule(accepted)))
        )
        self.assertIsNone(
            retention._mood_authority_from_line(self.encode(capsule(rejected)))
        )

    def test_a_parser_overflow_under_the_byte_cap_fails_closed(self):
        """Depth is refused before the byte limit can notice anything is wrong."""
        overflow = (
            '{"_burrow_internal":"mood-authority-v1","events":'
            + "[" * 1100
            + "0"
            + "]" * 1100
            + "}"
        )
        self.assertLess(
            len(overflow.encode("utf-8")), retention.MOOD_AUTHORITY_MAX_BYTES
        )
        self.assertIsNone(retention._mood_authority_from_line(overflow))


class MoodLifecycleVectorTests(unittest.TestCase):
    """``mood-lifecycle-adversarial.json`` against ``_mood_keep_indexes``.

    One `request_id` is reused with a different action and a second ID collides
    across agents, so the vector is a direct probe of the collision handling in
    the gnarliest branch of retention.
    """

    def test_adversarial_lifecycles_keep_their_canonical_evidence(self):
        fixture = load("mood-lifecycle-adversarial.json")
        events = fixture["events"]
        indexes = retention._mood_keep_indexes(
            list(enumerate(events)), {"codex:close", "codex:owner"}
        )
        self.assertIn(
            0, indexes, "the exact close independently carries its canonical knock"
        )
        self.assertIn(6, indexes, "the owner's cross-agent canonical truth is carried")

        rotated = retention.carry_forward(
            [json.dumps(item, separators=(",", ":")) for item in events],
            epoch_ms(fixture["now"]),
            retention.POLICY,
        )
        decoded = [json.loads(line) for line in rotated.lines]
        self.assertIn(events[0], decoded, "rotation keeps the displaced knock")
        self.assertIn(events[6], decoded, "rotation keeps the collision owner")


if __name__ == "__main__":
    unittest.main()
