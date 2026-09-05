import json
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

    def test_the_pre_rename_burrow_spelling_configures_nothing(self):
        """warren#361: the fallback is gone, so a stale .env gets the defaults.

        Asserted rather than left unsaid, because "it is ignored" is exactly what
        an operator with a pre-rename `.env` needs to be told — and because the
        opposite of this test is what stood here until the rename finished, so it
        is a claim that can fail.
        """
        defaults = Config()
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
        self.assertEqual(config.host, defaults.host)
        self.assertEqual(config.port, 9000)  # argv, not the environment
        self.assertEqual(config.events, defaults.events)
        self.assertEqual(config.villagers_dir, defaults.villagers_dir)
        self.assertEqual((config.token, config.notify_token), ("", ""))
        self.assertIsNone(config.archive_dir)
        self.assertEqual(config.max_log_bytes, defaults.max_log_bytes)
        self.assertEqual(config.notify_timeout, defaults.notify_timeout)
        self.assertEqual(
            (config.knock_records, config.knock_bytes),
            (defaults.knock_records, defaults.knock_bytes),
        )

    def test_the_new_spelling_is_the_only_one_read(self):
        """Every setting, under its own name, with a stale twin sitting beside it."""
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

    def test_an_empty_value_is_a_value_and_not_an_absence(self):
        """``CHRONICLE_TOKEN=`` means open ingest, not "fall through to a default"."""
        config = Config.from_env({"CHRONICLE_TOKEN": ""})
        self.assertEqual(config.token, "")

    def test_import_ignores_argv_and_tolerates_invalid_environment(self):
        environment = dict(os.environ)
        environment.update(
            CHRONICLE_MAX_LOG="not-an-integer",
            CHRONICLE_KNOCK_RECORDS="not-an-integer",
            CHRONICLE_NOTIFY_TIMEOUT="not-a-float",
        )
        result = subprocess.run(
            [sys.executable, "-c", "import serve; print(serve.app.state.config.port)", "not-a-port"],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "8737")

    def test_runtime_projects_its_configured_residents_without_http_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            villagers = root / "villagers"
            villagers.mkdir()
            manifest = json.loads(
                (
                    Path(__file__).parent / "fixtures/project-agent.resident.json"
                ).read_text(encoding="utf-8")
            )
            manifest["soul"]["name"] = "Configured Resident"
            (villagers / "resident.resident.json").write_text(json.dumps(manifest))
            runtime = serve.Runtime(
                Config(events=root / "events.jsonl", villagers_dir=villagers)
            )
            snapshot = runtime.state_coordinator.evaluate()
            self.assertEqual(
                [item["meta"]["name"] for item in snapshot["residents"]],
                ["Configured Resident"],
            )

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
