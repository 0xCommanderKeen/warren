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
"""

import copy
import datetime
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import retention


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
        self.assertIsNotNone(retention._mood_authority_from_line(self.encode(self.base)))

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
        self.assertIn(
            6, indexes, "the owner's cross-agent canonical truth is carried"
        )

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
