"""The two approval vector files, wired to the Python code that owns them.

``approval-identity.json`` and ``approval-lifecycle.json`` were written as shared
vectors: a JavaScript projection and Python rotation had to agree on them. The
JavaScript was deleted (warren#219) and for a while nothing loaded either file, so
docs/protocol.md honestly described them as recorded but unenforced (warren#249).

They are enforced here. Both encode rules that survived the deletion because they
were never really about the browser — they are rotation's rules for which approval
records a rotated log must carry forward:

* identity — which two `needs_human` appends are *the same request*, and which
  are a collision that must quarantine the ID.
* lifecycle — which single close a request is rendered with, given replays,
  conflicting decisions, and producer timestamps that disagree with append order.

Neither file was edited to fit the code. Every vector already matched
``retention``'s behavior at the time it was wired.
"""

import json
import pathlib
import unittest

import retention


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def knock(payload):
    """A valid structured knock envelope around a fixture's bare payload.

    The identity vectors vary only the payload, so the envelope fields that also
    feed the identity — ``agent_id`` and ``project`` — are held equal on both
    sides. A vector's verdict is therefore attributable to the payload alone.
    """
    return {
        "v": 0,
        "ts": "2026-08-25T10:00:00.000Z",
        "source": "codex",
        "agent_id": "codex:keeper",
        "project": "life",
        "type": "needs_human",
        "payload": payload,
    }


class ApprovalIdentityVectorTests(unittest.TestCase):
    """``approval-identity.json`` against ``_approval_lifecycle_identity``."""

    def test_every_recorded_identity_vector_holds(self):
        vectors = json.loads((FIXTURES / "approval-identity.json").read_text())
        self.assertTrue(vectors, "the identity fixture must not be empty")
        for vector in vectors:
            with self.subTest(vector["name"]):
                left = retention._approval_lifecycle_identity(knock(vector["left"]))
                right = retention._approval_lifecycle_identity(knock(vector["right"]))
                # A vector whose payload stopped classifying as structured would
                # otherwise pass every `compatible: false` case for free.
                self.assertIsNotNone(left, "left side is not a structured knock")
                self.assertIsNotNone(right, "right side is not a structured knock")
                self.assertEqual(vector["compatible"], left == right)

    def test_identity_is_not_trivially_equal_across_vectors(self):
        """Guards against an identity that collapses everything to one value."""
        vectors = json.loads((FIXTURES / "approval-identity.json").read_text())
        identities = {
            retention._approval_lifecycle_identity(knock(vector["left"]))
            for vector in vectors
        }
        self.assertEqual(len(vectors), len(identities))


class ApprovalLifecycleVectorTests(unittest.TestCase):
    """``approval-lifecycle.json`` against ``_approval_keep_indexes``.

    Rotation is where the rendered decision is decided: the selection here is
    exactly what a rotated log carries forward, so a close it drops can never be
    projected again after rotation.
    """

    def test_every_recorded_lifecycle_vector_holds(self):
        vectors = json.loads((FIXTURES / "approval-lifecycle.json").read_text())
        self.assertTrue(vectors, "the lifecycle fixture must not be empty")
        for vector in vectors:
            with self.subTest(vector["name"]):
                events = vector["events"]
                keep, isolated = retention._approval_keep_indexes(
                    list(enumerate(events))
                )
                kept = [events[index] for index in sorted(keep)]
                knocks = [item for item in kept if item["type"] == "needs_human"]
                closes = [
                    item for item in kept if item["type"] == "needs_human_resolved"
                ]
                self.assertEqual(1, len(knocks), "one request survives per vector")
                self.assertEqual(vector["expected_request_ts"], knocks[0]["ts"])
                self.assertLessEqual(len(closes), 1, "at most one close is rendered")
                decision = closes[0]["payload"]["decision"] if closes else None
                self.assertEqual(vector["expected_decision"], decision)
                # Every approval record stays isolated from ordinary villager
                # retention whether or not it is carried forward.
                self.assertEqual(set(range(len(events))), isolated)


if __name__ == "__main__":
    unittest.main()
