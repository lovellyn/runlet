import socket
import socketserver
import struct
import threading
from types import SimpleNamespace

import pytest

from scripts import smoke_container


@pytest.mark.parametrize("reset", [True, False], ids=["tcp-reset", "http-disconnect"])
def test_readiness_recovers_after_connections_close_during_startup(reset):
    attempts = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                request += chunk
            attempts.append(request)
            if len(attempts) <= 2:
                if reset:
                    self.request.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )
                return
            self.request.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 16\r\nConnection: close\r\n\r\n"
                b'{"status": "ok"}'
            )

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        try:
            smoke_container.wait_ready(f"http://127.0.0.1:{server.server_address[1]}")
        finally:
            server.shutdown()
            worker.join(timeout=5)
        assert not worker.is_alive()
    assert len(attempts) == 3
    assert all(request.startswith(b"GET /healthz ") for request in attempts)


def test_readiness_still_times_out_when_connections_keep_resetting(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    attempts = []

    def sleep(seconds):
        clock.now += seconds

    def unavailable(*args, **kwargs):
        attempts.append(clock.now)
        raise ConnectionResetError("container has not started")

    monkeypatch.setattr(
        smoke_container, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
    )
    monkeypatch.setattr(smoke_container.urllib.request, "urlopen", unavailable)
    with pytest.raises(RuntimeError, match="did not become ready within 45 seconds"):
        smoke_container.wait_ready("http://localhost:8000")
    assert clock.now >= 45
    assert len(attempts) > 1
