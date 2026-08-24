#!/usr/bin/env python3
"""Ingest auth tests: BURROW_TOKEN gates POST /events and nothing else.

No sockets — the handler is driven over in-memory streams, so this is the real
request-parsing/auth/append path, just without a listener. Run:

    python3 tests/test_ingest_auth.py
"""
import importlib.util
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EVENT = {
    "v": 0, "ts": "2026-08-24T14:03:22.114Z", "source": "claude-code",
    "agent_id": "claude-code:test", "project": "burrow", "cwd": "/tmp",
    "type": "idle", "payload": {},
}

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        failures.append(label)


def load(path, name, env):
    """Import a module fresh under a given environment (both modules read their
    config into globals at import time, which is what we want to exercise)."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: v for k, v in env.items() if v is not None})
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def request(serve, raw):
    """Run one raw HTTP request through the handler; return (status, body)."""
    class Fake(serve.Handler):
        def setup(self):
            self.rfile = io.BytesIO(raw)
            self.wfile = io.BytesIO()

        def finish(self):
            pass

    handler = Fake.__new__(Fake)
    handler.request = handler.client_address = handler.server = None
    handler.setup()
    handler.handle_one_request()
    out = handler.wfile.getvalue()
    status = int(out.split(b"\r\n", 1)[0].split(b" ")[1])
    body = out.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in out else b""
    return status, body


def post(serve, headers=(), body=None):
    payload = json.dumps(body if body is not None else EVENT).encode()
    lines = [b"POST /events HTTP/1.1", b"Host: x",
             b"Content-Type: application/json",
             b"Content-Length: " + str(len(payload)).encode()]
    lines += [h.encode() for h in headers]
    return request(serve, b"\r\n".join(lines) + b"\r\n\r\n" + payload)


def get(serve, path):
    return request(serve, f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())


def lines_in(path):
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return len([ln for ln in f if ln.strip()])


def main():
    tmp = tempfile.mkdtemp()
    serve_py = os.path.join(ROOT, "serve.py")
    emit_py = os.path.join(ROOT, "hooks", "emit.py")

    # --- token unset: today's open behavior ------------------------------
    print("BURROW_TOKEN unset (open ingest)")
    log = os.path.join(tmp, "open.jsonl")
    serve = load(serve_py, "serve_open", {"BURROW_EVENTS": log, "BURROW_TOKEN": None})
    check("POST no token", post(serve)[0], 204)
    check("POST bogus bearer accepted", post(serve, ["Authorization: Bearer whatever"])[0], 204)
    check("POST malformed json", post(serve, body={"nope": 1})[0], 400)
    check("GET /events", get(serve, "/events")[0], 200)
    check("GET /villagers", get(serve, "/villagers")[0], 200)
    check("events logged", lines_in(log), 2)

    # --- whitespace-only token counts as unset ---------------------------
    print("BURROW_TOKEN='   ' (treated as unset)")
    log = os.path.join(tmp, "blank.jsonl")
    serve = load(serve_py, "serve_blank", {"BURROW_EVENTS": log, "BURROW_TOKEN": "   "})
    check("POST no token", post(serve)[0], 204)

    # --- token set: ingest closed ----------------------------------------
    print("BURROW_TOKEN=s3cret (closed ingest)")
    log = os.path.join(tmp, "closed.jsonl")
    serve = load(serve_py, "serve_closed", {"BURROW_EVENTS": log, "BURROW_TOKEN": "s3cret"})
    status, body = post(serve)
    check("POST no token", status, 401)
    check("401 body", body, b"unauthorized")
    check("POST wrong bearer", post(serve, ["Authorization: Bearer nope"])[0], 401)
    check("POST wrong x-header", post(serve, ["X-Burrow-Token: nope"])[0], 401)
    check("POST token as prefix", post(serve, ["Authorization: Bearer s3cretXX"])[0], 401)
    check("POST wrong scheme", post(serve, ["Authorization: Basic s3cret"])[0], 401)
    check("POST right bearer", post(serve, ["Authorization: Bearer s3cret"])[0], 204)
    check("POST right x-header", post(serve, ["X-Burrow-Token: s3cret"])[0], 204)
    check("GET /events ungated", get(serve, "/events")[0], 200)
    check("GET /villagers ungated", get(serve, "/villagers")[0], 200)
    check("only authorized events logged", lines_in(log), 2)

    # --- emitter: sends the token, falls back to local file on rejection --
    print("emitter (hooks/emit.py)")
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    hook = json.dumps({"hook_event_name": "Stop", "session_id": "abc", "cwd": "/tmp/proj"})

    def run_emitter(token, server):
        """Emit one event, routing the emitter's POST into `server`'s handler."""
        env = {"BURROW_URL": "http://village:8737", "BURROW_TOKEN": token,
               "BURROW_AGENT_ID": None, "BURROW_PROJECT": None, "HOME": home}
        emit = load(emit_py, "emit_" + (token or "none"), env)
        emit.LOG_DIR = home
        emit.LOG = os.path.join(home, "events.jsonl")
        emit.BREAKER = os.path.join(home, ".post-failed")
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["headers"] = dict(req.headers)
            body = req.data
            raw = (b"POST /events HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                   + b"".join(f"{k}: {v}\r\n".encode() for k, v in req.headers.items()
                              if k.lower() not in ("content-type",))
                   + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            status, _ = request(server, raw)
            seen["status"] = status
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")

            class Ctx:
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return Ctx()

        # main() reads BURROW_URL/BURROW_TOKEN at call time, so the env has to be
        # in place for the call, not just for the import.
        saved_env = {k: os.environ.get(k) for k in env}
        saved_open = emit.urllib.request.urlopen
        saved_stdin = sys.stdin
        for k, v in env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        emit.urllib.request.urlopen = fake_urlopen
        sys.stdin = io.StringIO(hook)
        try:
            emit.main()
        finally:
            sys.stdin = saved_stdin
            emit.urllib.request.urlopen = saved_open
            for k, v in saved_env.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        return seen

    # against the closed server, with the right token
    log = os.path.join(tmp, "emit-ok.jsonl")
    server = load(serve_py, "serve_emit_ok", {"BURROW_EVENTS": log, "BURROW_TOKEN": "s3cret"})
    seen = run_emitter("s3cret", server)
    check("emitter sends bearer header", seen["headers"].get("Authorization"), "Bearer s3cret")
    check("server accepts emitter POST", seen["status"], 204)
    check("event landed on server", lines_in(log), 1)
    check("nothing written locally", lines_in(os.path.join(home, "events.jsonl")), 0)

    # against the closed server, with no token -> 401 -> local fallback
    os.remove(os.path.join(home, ".post-failed")) if os.path.exists(os.path.join(home, ".post-failed")) else None
    log = os.path.join(tmp, "emit-401.jsonl")
    server = load(serve_py, "serve_emit_401", {"BURROW_EVENTS": log, "BURROW_TOKEN": "s3cret"})
    seen = run_emitter(None, server)
    check("no token -> no auth header", "Authorization" in seen["headers"], False)
    check("server rejects", seen["status"], 401)
    check("nothing logged on server", lines_in(log), 0)
    check("event fell back to local file", lines_in(os.path.join(home, "events.jsonl")), 1)
    check("circuit breaker tripped", os.path.exists(os.path.join(home, ".post-failed")), True)

    print()
    if failures:
        print(f"FAILED: {len(failures)} -> {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
