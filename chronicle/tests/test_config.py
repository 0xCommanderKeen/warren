import dataclasses
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import serve
from config import Config


EVENT = {
    "v": 0,
    "ts": "2026-08-24T14:03:22.114Z",
    "source": "test",
    "agent_id": "test:config",
    "project": "burrow",
    "cwd": "",
    "type": "idle",
    "payload": {},
}


SETTINGS = (
    "HOST",
    "EVENTS",
    "VILLAGERS",
    "TOKEN",
    "ARCHIVE",
    "MAX_LOG",
    "NOTIFY_URL",
    "NOTIFY_TOKEN",
    "NOTIFY_TIMEOUT",
    "KNOCK_RECORDS",
    "KNOCK_BYTES",
)


class ConfigTests(unittest.TestCase):
    def test_from_env_parses_all_external_inputs_into_a_frozen_value(self):
        config = Config.from_env(
            {
                "CHRONICLE_HOST": "0.0.0.0",
                "CHRONICLE_EVENTS": "/tmp/events.jsonl",
                "CHRONICLE_VILLAGERS": "/tmp/villagers",
                "CHRONICLE_TOKEN": " secret ",
                "CHRONICLE_ARCHIVE": "/tmp/archive",
                "CHRONICLE_MAX_LOG": "42",
                "CHRONICLE_NOTIFY_URL": " https://notify.invalid/topic ",
                "CHRONICLE_NOTIFY_TOKEN": " bearer ",
                "CHRONICLE_NOTIFY_TIMEOUT": "1.5",
                "CHRONICLE_KNOCK_RECORDS": "7",
                "CHRONICLE_KNOCK_BYTES": "99",
            },
            ["9000"],
        )
        self.assertEqual((config.host, config.port), ("0.0.0.0", 9000))
        self.assertEqual(config.events, Path("/tmp/events.jsonl"))
        self.assertEqual(config.villagers_dir, Path("/tmp/villagers"))
        self.assertEqual((config.token, config.notify_token), ("secret", "bearer"))
        self.assertEqual(config.archive_dir, Path("/tmp/archive"))
        self.assertEqual((config.max_log_bytes, config.notify_timeout), (42, 1.5))
        self.assertEqual(
            (
                config.knock_records,
                config.knock_bytes,
                config.ledger_records,
                config.ledger_bytes,
            ),
            (7, 99, 7, 99),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.port = 1

    def test_the_pre_rename_burrow_spelling_still_configures_every_setting(self):
        """Deployed .env files predate the rename; one release accepts both."""
        config = Config.from_env(
            {
                "BURROW_HOST": "0.0.0.0",
                "BURROW_EVENTS": "/tmp/events.jsonl",
                "BURROW_VILLAGERS": "/tmp/villagers",
                "BURROW_TOKEN": " secret ",
                "BURROW_ARCHIVE": "/tmp/archive",
                "BURROW_MAX_LOG": "42",
                "BURROW_NOTIFY_URL": " https://notify.invalid/topic ",
                "BURROW_NOTIFY_TOKEN": " bearer ",
                "BURROW_NOTIFY_TIMEOUT": "1.5",
                "BURROW_KNOCK_RECORDS": "7",
                "BURROW_KNOCK_BYTES": "99",
            },
            ["9000"],
        )
        self.assertEqual((config.host, config.port), ("0.0.0.0", 9000))
        self.assertEqual(config.events, Path("/tmp/events.jsonl"))
        self.assertEqual(config.villagers_dir, Path("/tmp/villagers"))
        self.assertEqual((config.token, config.notify_token), ("secret", "bearer"))
        self.assertEqual(config.archive_dir, Path("/tmp/archive"))
        self.assertEqual((config.max_log_bytes, config.notify_timeout), (42, 1.5))
        self.assertEqual((config.knock_records, config.knock_bytes), (7, 99))

    def test_the_new_spelling_wins_wherever_both_are_set(self):
        """A half-migrated environment must resolve one way, not by setting."""
        environ = {}
        for name in SETTINGS:
            environ["BURROW_" + name] = "old"
            environ["CHRONICLE_" + name] = "new"
        environ |= {
            "BURROW_MAX_LOG": "1",
            "CHRONICLE_MAX_LOG": "2",
            "BURROW_NOTIFY_TIMEOUT": "1",
            "CHRONICLE_NOTIFY_TIMEOUT": "2",
            "BURROW_KNOCK_RECORDS": "1",
            "CHRONICLE_KNOCK_RECORDS": "2",
            "BURROW_KNOCK_BYTES": "1",
            "CHRONICLE_KNOCK_BYTES": "2",
        }
        config = Config.from_env(environ)
        self.assertEqual(config.host, "new")
        self.assertEqual(config.events, Path("new").expanduser())
        self.assertEqual(config.villagers_dir, Path("new").expanduser())
        self.assertEqual(config.token, "new")
        self.assertEqual(config.archive_dir, Path("new").expanduser())
        self.assertEqual(config.notify_url, "new")
        self.assertEqual(config.notify_token, "new")
        self.assertEqual(config.max_log_bytes, 2)
        self.assertEqual(config.notify_timeout, 2.0)
        self.assertEqual((config.knock_records, config.knock_bytes), (2, 2))

    def test_an_empty_new_spelling_still_overrides_the_old_one(self):
        """Presence selects the spelling; "" is a value, not an absence."""
        config = Config.from_env(
            {"BURROW_TOKEN": "leftover", "CHRONICLE_TOKEN": ""}
        )
        self.assertEqual(config.token, "")

    def test_import_does_not_parse_argv_or_environment(self):
        environment = dict(os.environ)
        environment.update(
            CHRONICLE_MAX_LOG="not-an-integer",
            CHRONICLE_KNOCK_RECORDS="not-an-integer",
            CHRONICLE_NOTIFY_TIMEOUT="not-a-float",
        )
        result = subprocess.run(
            [sys.executable, "-c", "import serve; print(serve.PORT)", "not-a-port"],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "8737")

    def test_app_factory_isolates_storage_and_authentication(self):
        with tempfile.TemporaryDirectory() as temporary:
            first_path = Path(temporary) / "first.jsonl"
            second_path = Path(temporary) / "second.jsonl"
            first = serve.create_app(
                dataclasses.replace(Config(), events=first_path, token="first")
            )
            second = serve.create_app(
                dataclasses.replace(Config(), events=second_path, token="second")
            )
            with TestClient(first) as first_client, TestClient(second) as second_client:
                self.assertEqual(
                    first_client.post(
                        "/events",
                        json=EVENT,
                        headers={"Authorization": "Bearer first"},
                    ).status_code,
                    204,
                )
                self.assertEqual(
                    second_client.post(
                        "/events",
                        json=EVENT,
                        headers={"Authorization": "Bearer first"},
                    ).status_code,
                    401,
                )
                self.assertEqual(
                    second_client.post(
                        "/events",
                        json=EVENT,
                        headers={"Authorization": "Bearer second"},
                    ).status_code,
                    204,
                )
            self.assertTrue(first_path.exists())
            self.assertTrue(second_path.exists())
            self.assertEqual(len(first_path.read_text().splitlines()), 1)
            self.assertEqual(len(second_path.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
