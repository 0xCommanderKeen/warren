"""Small lifecycle helpers shared by real-socket ASGI transport tests."""

import socket
import threading
import time

import uvicorn


class RunningServer:
    def __init__(self, application):
        self._application = application
        self.server = None
        self.thread = None
        self._socket = None
        self.restart()

    def restart(self):
        self.stop()
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen()
        address = self._socket.getsockname()
        uvicorn_server = uvicorn.Server(
            uvicorn.Config(self._application, log_level="error", lifespan="on")
        )
        self.server = _ServerView(uvicorn_server, address, self._application)
        self.thread = threading.Thread(
            target=uvicorn_server.run, kwargs={"sockets": [self._socket]}, daemon=True
        )
        self.thread.start()
        deadline = time.monotonic() + 3
        while not uvicorn_server.started and self.thread.is_alive():
            if time.monotonic() >= deadline:
                raise RuntimeError("ASGI test server did not start")
            time.sleep(0.005)

    def stop(self):
        if self.server is None:
            return
        self.server._server.should_exit = True
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError("ASGI test server did not stop")
        self._socket.close()
        self.server = None
        self.thread = None
        self._socket = None


class _ServerView:
    def __init__(self, server, address, application):
        self._server = server
        self.server_address = address
        self._application = application

    @property
    def boot_id(self):
        return self._application.state.runtime.boot_id

    @property
    def state_coordinator(self):
        return self._application.state.runtime.state_coordinator

    @property
    def event_log(self):
        return self._application.state.runtime.event_log


def wait_until(condition, timeout=3.0):
    """Poll ``condition`` until it holds or ``timeout`` seconds pass.

    Returns the condition's last value, so a caller asserts on what it waited
    for rather than on the wait itself. Worker delivery in the transport tests
    is asynchronous, so a reading taken once can run ahead of the worker.
    """
    deadline = time.monotonic() + timeout
    while True:
        outcome = condition()
        if outcome or time.monotonic() >= deadline:
            return outcome
        time.sleep(0.01)
