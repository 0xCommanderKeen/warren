"""Opt-in Docker test of the actual pinned CLI; no credentials or paid model calls."""

import json
import os
from pathlib import Path

import pytest
import yaml

from conftest import ResidentWriter, valid_manifest
from steward.codex_usage import read_usage
from steward.deploy import bundle_for, target_for
from steward.manifest import Runner as RunnerSpec
from steward.manifest import ToolGrant, load_manifest
from steward.runners import CodexRunner, RunRequest, run_argv

# The fixture server and CLI run in one disposable container with --network none.
# Only loopback exists: an accidental real-provider request cannot leave the container.
PROBE = r"""
import json
import os
import subprocess
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# The entrypoint preserves the operator's files. The CLI may subsequently migrate
# its own config format, so check these values before starting it.
assert Path("/root/.codex/auth.json").read_text() == '{"OPENAI_API_KEY":"synthetic-test-key"}'
saved_config = tomllib.loads(Path("/root/.codex/config.toml").read_text())
assert saved_config["sandbox_mode"] == "danger-full-access"
assert saved_config["approval_policy"] == "on-request"

requests = []
api_calls = []
command = '''set -eu
printf 'persisted' > /memory/probe
curl -fsS "$STEWARD_URL/skills" -H "Authorization: Bearer $STEWARD_SESSION_TOKEN"
if touch /opt/outside-probe 2>/dev/null; then exit 9; fi
printf '\\nprobe-passed\\n'
'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        api_calls.append((self.path, self.headers.get("Authorization")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"skills": []}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def event(kind, **data):
            self.wfile.write(("data: " + json.dumps({"type": kind, **data}) + "\n\n").encode())

        event("response.created", response={"id": "resp_probe"})
        if len(requests) == 1:
            item = {
                "type": "function_call",
                "name": "exec_command",
                "arguments": json.dumps({"cmd": command, "max_output_tokens": 1000}),
                "call_id": "call_probe",
                "id": "fc_probe",
            }
        else:
            item = {
                "type": "message",
                "role": "assistant",
                "id": "msg_probe",
                "content": [{"type": "output_text", "text": "probe complete"}],
            }
        event("response.output_item.done", output_index=0, item=item)
        event(
            "response.completed",
            response={
                "id": "resp_probe",
                "output": [item],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "total_tokens": 110,
                    "input_tokens_details": {"cached_tokens": 0},
                },
            },
        )


server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"
os.environ["STEWARD_URL"] = base
os.environ["STEWARD_SESSION_TOKEN"] = "probe-session-only"
os.makedirs("/memory", exist_ok=True)
argv = json.loads(sys.argv[1])
argv[-1:-1] = [
    "-c",
    'model_provider="stub"',
    "-c",
    'model_providers.stub.name="stub"',
    "-c",
    f'model_providers.stub.base_url="{base}/v1"',
    "-c",
    'model_providers.stub.wire_api="responses"',
    "-c",
    "model_providers.stub.requires_openai_auth=false",
]
result = subprocess.run(
    argv, cwd="/memory", capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
)
assert result.returncode == 0, result.stdout + result.stderr
tool_outputs = [
    item for body in requests for item in body.get("input", [])
    if item.get("type") == "function_call_output"
]
assert Path("/memory/probe").exists(), result.stdout + result.stderr + json.dumps(tool_outputs)
assert Path("/memory/probe").read_text() == "persisted"
assert api_calls == [("/skills", "Bearer probe-session-only")]
assert len(requests) == 2
assert "probe-passed" in json.dumps(requests[-1]["input"])
# Codex may rewrite config.toml privately as container root. Inspect its resulting
# state from that account without requiring the host's unrelated UID to read it.
assert Path("/root/.codex/auth.json").read_text() == '{"OPENAI_API_KEY":"synthetic-test-key"}'
assert tomllib.loads(Path("/root/.codex/config.toml").read_text())
assert Path("/root/.codex").stat().st_mode & 0o777 == 0o700
print("CODEX_RECEIPT_START")
print(result.stdout, end="")
"""


@pytest.mark.skipif(
    not os.environ.get("CODEX_RUNTIME_IMAGE"), reason="requires built resident image"
)
def test_codex_exec_uses_memory_api_token_and_returns_usage(
    tmp_path: Path, write_resident: ResidentWriter
) -> None:
    image = os.environ["CODEX_RUNTIME_IMAGE"]
    resident = load_manifest(write_resident(valid_manifest() | {"runner": {"kind": "codex"}}))
    bundle = bundle_for(resident, target_for(resident.manifest), {})
    service = yaml.safe_load(bundle["docker-compose.yaml"])["services"][resident.id]
    (tmp_path / "codex-seccomp.json").write_bytes(bundle["codex-seccomp.json"])
    security_args = [
        item
        for option in service["security_opt"]
        for item in ("--security-opt", option.replace("seccomp=./", f"seccomp={tmp_path}/"))
    ]
    auth = tmp_path / "auth.json"
    auth.write_text('{"OPENAI_API_KEY":"synthetic-test-key"}')
    config = tmp_path / "config.toml"
    config.write_text('sandbox_mode="danger-full-access"\napproval_policy="on-request"\n')
    runner = CodexRunner(RunnerSpec(kind="codex", model="gpt-5.3-codex"))
    argv = runner.argv(
        RunRequest(
            tools=ToolGrant("unrestricted"),
            prompt="run the probe",
            workdir=Path("/memory"),
            timeout_s=60,
        )
    )
    result = run_argv(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--platform",
            "linux/amd64",
            *security_args,
            "-v",
            f"{tmp_path}:/root/.codex",
            "-e",
            "CODEX_HOME=/root/.codex",
            image,
            "python3",
            "-",
            json.dumps(argv),
        ],
        stdin=PROBE.encode(),
        timeout_s=90,
    )
    assert result.ok, result.stdout + result.stderr
    assert "CODEX_RECEIPT_START\n" in result.stdout
    usage = read_usage(result.stdout.split("CODEX_RECEIPT_START\n", 1)[1], None)
    assert usage.output == "probe complete"
    assert usage.input_tokens is not None
    assert usage.output_tokens is not None
    assert not usage.failed
