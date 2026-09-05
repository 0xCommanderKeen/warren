"""Installed bundle → real HTTP → durable snapshot and reconnect boundary."""

import http.client
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import serve
from config import Config
from tests.http_test_support import RunningServer, wait_until

ROOT = Path(__file__).resolve().parents[1]


class InstalledDeliveryTest(unittest.TestCase):
    def test_installed_worker_drains_without_another_hook_and_reconnect_has_presence(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            home = root / "home"
            home.mkdir()
            config = Config(
                events=root / "server.jsonl",
                villagers_dir=root / "villagers",
                token="synthetic-secret",
            )
            running = RunningServer(serve.create_app(config))
            self.addCleanup(running.stop)
            host, port = running.server.server_address
            url = f"http://{host}:{port}"
            environment = dict(
                os.environ,
                HOME=str(home),
                CHRONICLE_INSTALL_ROOT=str(bundle),
                CHRONICLE_URL=url,
                CHRONICLE_TOKEN="synthetic-secret",
                CHRONICLE_MIRROR="",
            )
            subprocess.run(
                ["sh", str(ROOT / "scripts/install-emitter.sh")],
                env=environment,
                check=True,
                capture_output=True,
            )
            spool = home / ".chronicle"
            spool.mkdir()
            (spool / "delivery-config.json").write_text(
                json.dumps({"URL": url, "TOKEN": "synthetic-secret"})
            )
            hook = dict(
                hook_event_name="UserPromptSubmit",
                session_id="installed-test",
                prompt="synthetic work",
            )
            subprocess.run(
                [str(bundle / "chronicle-emit"), "--runner", "codex"],
                input=json.dumps(hook),
                text=True,
                env=environment,
                timeout=2,
                check=True,
            )
            self.assertFalse(config.events.exists())
            self.assertTrue((spool / "primary-outbox.jsonl").read_text().strip())
            worker = subprocess.Popen(
                [sys.executable, str(bundle / "delivery_service.py"), "run"],
                env=environment,
            )
            try:
                wait_until(
                    lambda: (
                        config.events.exists()
                        and bool(config.events.read_text().strip())
                    ),
                    timeout=5,
                )
                wait_until(
                    lambda: not (spool / "primary-outbox.jsonl").read_text().strip(),
                    timeout=5,
                )
                connection = http.client.HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/state/stream?generation=999&cursor=old")
                response = connection.getresponse()
                lines = [response.readline().decode() for _ in range(4)]
                envelope = json.loads(lines[2].removeprefix("data: "))
                connection.close()
                self.assertEqual("reset", envelope["kind"])
                self.assertEqual(
                    "working", envelope["snapshot"]["villagers"][0]["state"]
                )
                self.assertEqual(
                    "fresh",
                    envelope["snapshot"]["villagers"][0]["presence"]["freshness"],
                )
                self.assertEqual(1, len(config.events.read_text().splitlines()))
            finally:
                worker.terminate()
                worker.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
