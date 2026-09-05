"""Notification names and visible homes share one fleet allocation."""
import unittest
from unittest.mock import patch

import serve
from tests.test_village_state import NOW, RESIDENT, event
from village_state import project_village


class IdentityAllocationTests(unittest.TestCase):
    def test_one_project_resident_has_one_occupant_in_both_views(self):
        manifest = RESIDENT | {"file": "project.json", "match": {"project": "life"}, "meta": RESIDENT["meta"] | {"project": "life"}}
        events = [event("idle", agent="a"), event("idle", agent="q")]
        projected = project_village(events, [manifest], NOW)["villagers"]
        with patch.object(serve, "read_residents", return_value={"residents": [manifest]}):
            names = serve.villager_names(events)
        self.assertEqual({"a": "Burrow", "q": "Juniper"}, names)
        self.assertEqual(names, {item["id"]: item["name"] for item in projected})
        self.assertEqual([2, None], [item["home"] for item in projected])

    def assert_views(self, events, manifests, expected, now=NOW):
        projected = project_village(events, manifests, now)["villagers"]
        with patch.object(serve, "read_residents", return_value={"residents": manifests}):
            names = serve.villager_names(events, evaluated_at=now)
        self.assertEqual(expected, names)
        self.assertEqual(names, {item["id"]: item["name"] for item in projected})
        homes = [item["home"] for item in projected if item["home"] is not None]
        self.assertEqual(len(homes), len(set(homes)))
        return projected

    def test_exact_identity_reserves_home_before_lexical_project_match(self):
        manifest = RESIDENT | {"match": {"agent_id": "q", "project": "life"}}
        self.assert_views([event("idle", agent="a"), event("idle", agent="q")],
                          [manifest], {"a": "Hazel", "q": "Burrow"})

    def test_historical_child_lineage_cannot_inherit_project_identity(self):
        manifest = RESIDENT | {"match": {"project": "life"}}
        events = [event("task_started", agent="a", prompt="review", parent_agent_id="q"),
                  event("idle", agent="a"), event("idle", agent="q")]
        self.assert_views(events, [manifest], {"a": "Hazel", "q": "Burrow"})
        self.assert_views(events, [manifest | {"match": {"agent_id": "a"}}],
                          {"a": "Burrow", "q": "Juniper"})

    def test_absent_ended_and_ambient_only_agents_do_not_take_project_home(self):
        manifest = RESIDENT | {"match": {"project": "life"}}
        for previous in [event("idle", agent="a", minutes=-800),
                         event("session_ended", agent="a"),
                         event("chat_message_dropped", agent="a", route="discord", address="x",
                               **{"from": "visitor", "reason": "ignored"})]:
            with self.subTest(kind=previous["type"]):
                self.assert_views([previous, event("idle", agent="q")],
                                  [manifest], {"q": "Burrow"})

    def test_neutral_event_cannot_change_project_identity(self):
        manifest = RESIDENT | {"match": {"project": "life"}}
        neutral = event("chat_message_posted", agent="a", resident="a", route="discord",
                        channel="room", length=1) | {"project": "other"}
        self.assert_views([event("idle", agent="a"), neutral], [manifest], {"a": "Burrow"})

    def test_declaration_retirement_and_revival_agree(self):
        declaration = event("resident_declared", agent="a", source="steward", name="Pip",
                            char="Monk", accent="#123456", role="helper", summary=None,
                            resident_id="pip", uid="0198-uid", home=2)
        retirement = event("resident_retired", agent="a", source="steward",
                           resident_id="pip", uid="0198-uid")
        self.assert_views([declaration], [], {"a": "Pip"})
        self.assert_views([declaration, retirement], [], {})
        self.assert_views([declaration, retirement, declaration], [], {"a": "Pip"})

    def test_legacy_soul_does_not_rename_a_projected_visitor(self):
        legacy = {"file": "legacy.md", "meta": {"project": "life", "name": "Legacy"}}
        self.assert_views([event("idle", agent="a")], [legacy], {"a": "Hazel"})

    def test_pending_approval_keeps_ended_occupant_and_names_consistent(self):
        knock = event("needs_human", agent="a", message="help", request_id="request-1",
                      action="restart", detail={}, options=["approve", "deny"])
        self.assert_views([knock, event("session_ended", agent="a"), event("idle", agent="q")],
                          [RESIDENT | {"match": {"project": "life"}}],
                          {"a": "Burrow", "q": "Juniper"})

    def test_declaration_reserves_home_ahead_of_project_fallback(self):
        declaration = event("resident_declared", agent="q", source="steward", name="Pip",
                            char="Monk", accent="#123456", role="helper", summary=None,
                            resident_id="pip", uid="0198-uid", home=2)
        self.assert_views([event("idle", agent="a"), declaration],
                          [RESIDENT | {"match": {"project": "life"}}],
                          {"a": "Hazel", "q": "Pip"})

    def test_notification_names_are_not_lost_at_snapshot_capacity(self):
        events = [event("idle", agent=f"agent-{index:03}") for index in range(257)]
        manifest = RESIDENT | {"match": {"agent_id": "agent-000"}}
        with patch.object(serve, "read_residents", return_value={"residents": [manifest]}):
            names = serve.villager_names(events)
        self.assertEqual(257, len(names))
        self.assertEqual("Burrow", names["agent-000"])
        for villager in project_village(events, [manifest], NOW)["villagers"]:
            self.assertEqual(villager["name"], names[villager["id"]])


if __name__ == "__main__":
    unittest.main()
