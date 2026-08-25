"""Small lifecycle helpers shared by the real-HTTP server tests."""

import threading


class RunningServer:
    def __init__(self, serve_module):
        self._serve = serve_module
        self.server = None
        self.thread = None
        self.restart()

    def restart(self):
        self.stop()
        self.server = self._serve.BurrowHTTPServer(
            ("127.0.0.1", 0), self._serve.Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = None
        self.thread = None
