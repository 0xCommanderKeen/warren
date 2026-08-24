import json
import os
import sys
import tempfile
import unittest
from unittest import mock

with mock.patch.object(sys, "argv", ["serve.py"]):
    import serve


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = os.path.join(self.tmp.name, "events.jsonl")
        self.villagers = os.path.join(self.tmp.name, "villagers")
        os.mkdir(self.villagers)
        self.patches = [
            mock.patch.object(serve, "EVENTS", self.events),
            mock.patch.object(serve, "VILLAGERS_DIR", self.villagers),
            mock.patch.object(serve, "NOTIFY_URL", "https://notify.invalid/topic"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        serve._notified.clear()
        serve._notifying.clear()

    @staticmethod
    def event(agent_id="agent-a", project="burrow", message="help", ts="2026-08-24T12:00:00Z"):
        return {
            "v": 1,
            "ts": ts,
            "agent_id": agent_id,
            "project": project,
            "type": "needs_human",
            "payload": {"message": message},
        }

    def write_events(self, *events):
        with open(self.events, "w", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(event) + "\n")

    def write_soul(self, filename, **metadata):
        with open(os.path.join(self.villagers, filename), "w", encoding="utf-8") as stream:
            stream.write("---\n")
            for key, value in metadata.items():
                stream.write(f"{key}: {value}\n")
            stream.write("---\n")

    def test_failed_delivery_can_be_claimed_again_but_success_is_deduplicated(self):
        event = self.event()
        self.assertTrue(serve.claim_knock(event))
        with mock.patch.object(serve, "notify", return_value=False):
            serve.deliver_knock(event)
        self.assertTrue(serve.claim_knock(event))
        with mock.patch.object(serve, "notify", return_value=True):
            serve.deliver_knock(event)
        self.assertFalse(serve.claim_knock(event))

    def test_project_soul_is_consumed_once_across_the_fleet(self):
        self.write_soul("burrow.md", project="burrow", name="Maren")
        first = self.event(agent_id="a")
        second = self.event(agent_id="q", ts="2026-08-24T12:00:01Z")
        self.write_events(first, second)

        self.assertEqual("Maren", serve.villager_name(first))
        self.assertEqual("Poppy", serve.villager_name(second))

    def test_fallback_names_probe_hash_collisions_across_the_fleet(self):
        first = self.event(agent_id="a")
        second = self.event(agent_id="q", ts="2026-08-24T12:00:01Z")
        self.write_events(first, second)

        self.assertEqual({"a": "Poppy", "q": "Wren"},
                         serve.villager_names([first, second]))

    def test_hash_matches_javascript_for_non_bmp_agent_ids(self):
        event = self.event(agent_id="agent-U0001f407")
        self.assertEqual("Reed", serve.villager_names([event])["agent-U0001f407"])

    def test_exact_soul_is_not_reused_by_another_agent(self):
        self.write_soul("resident.md", agent_id="resident", project="burrow", name="Maren")
        ephemeral = self.event(agent_id="ephemeral")
        resident = self.event(agent_id="resident", ts="2026-08-24T12:00:01Z")
        self.write_events(ephemeral, resident)

        names = serve.villager_names([ephemeral, resident])
        self.assertEqual("Maren", names["ephemeral"])
        self.assertNotEqual("Maren", names["resident"])

    def test_unicode_title_is_ascii_safe_for_http(self):
        event = self.event(project="项目")
        self.write_soul("unicode.md", agent_id="agent-a", name="玛伦")
        self.write_events(event)
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return mock.MagicMock()

        with mock.patch.object(serve.urllib.request, "urlopen", side_effect=open_request):
            self.assertTrue(serve.notify(event))

        title = captured["request"].get_header("Title")
        title.encode("latin-1")
        self.assertIn("=?utf-8?", title.lower())

    def test_message_is_preserved_verbatim(self):
        event = self.event(message="  first line\nsecond line\n")
        self.write_events(event)
        captured = {}

        def open_request(request, timeout):
            captured["body"] = request.data
            return mock.MagicMock()

        with mock.patch.object(serve.urllib.request, "urlopen", side_effect=open_request):
            self.assertTrue(serve.notify(event))

        name = serve.villager_name(event)
        self.assertEqual(
            f"{name} · burrow\n  first line\nsecond line\n".encode(), captured["body"])

        empty = self.event(message="")
        with mock.patch.object(serve.urllib.request, "urlopen", side_effect=open_request):
            self.assertTrue(serve.notify(empty))
        self.assertEqual(f"{name} · burrow\n".encode(), captured["body"])


if __name__ == "__main__":
    unittest.main()
