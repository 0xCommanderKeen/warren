#!/usr/bin/env python3
"""burrow v0 server: serves viewer/index.html and the raw event log.

    python3 serve.py [port]     # default 8737
"""
import http.server
import os
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8737
ROOT = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.path.expanduser("~/.burrow/events.jsonl")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_file(os.path.join(ROOT, "viewer", "index.html"),
                            "text/html; charset=utf-8")
        elif self.path.split("?")[0] == "/events":
            data = b""
            if os.path.exists(EVENTS):
                with open(EVENTS, "rb") as f:
                    data = f.read()
            self._send(200, data, "application/x-ndjson")
        else:
            self._send(404, b"not found", "text/plain")

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._send(404, b"missing: " + path.encode(), "text/plain")

    def _send(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"burrow village at http://localhost:{PORT}")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
