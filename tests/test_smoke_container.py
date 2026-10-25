import socket
import socketserver
import struct
import threading
from contextlib import ExitStack
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


def test_restart_uses_the_current_published_port(monkeypatch):
    state = SimpleNamespace(restarted=False, now=0.0)
    requests = []
    exercises = []

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                request += chunk
            port = self.server.server_address[1]
            requests.append(port)
            status = (
                b"503 Service Unavailable" if state.restarted and port == ports[0] else b"200 OK"
            )
            self.request.sendall(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Length: 16\r\nConnection: close\r\n\r\n"
                + b'{"status": "ok"}'
            )

    def sleep(seconds):
        state.now += seconds

    def docker(*args, **kwargs):
        if args[0] == "port":
            return f"127.0.0.1:{ports[int(state.restarted)]}"
        if args[0] == "restart":
            state.restarted = True
        elif args[0] == "exec":
            return "0"
        return ""

    async def exercise(url, token, workspace_id=None, *, identity=None):
        exercises.append((url, workspace_id, identity))
        return f"boot-{int(state.restarted)}", "workspace"

    monkeypatch.setattr("sys.argv", ["smoke_container.py", "--image", "test-image"])
    monkeypatch.setattr(smoke_container, "docker", docker)
    monkeypatch.setattr(smoke_container, "exercise", exercise)
    monkeypatch.setattr(
        smoke_container,
        "subprocess",
        SimpleNamespace(run=lambda *a, **k: SimpleNamespace(stdout="", stderr="")),
    )
    monkeypatch.setattr(
        smoke_container,
        "time",
        SimpleNamespace(monotonic=lambda: state.now, sleep=sleep),
    )
    with ExitStack() as stack:
        servers = [
            stack.enter_context(socketserver.TCPServer(("127.0.0.1", 0), Handler)) for _ in range(2)
        ]
        ports = [server.server_address[1] for server in servers]
        workers = [
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            for server in servers
        ]
        for worker in workers:
            worker.start()
        try:
            smoke_container.main()
        finally:
            for server, worker in zip(servers, workers, strict=True):
                server.shutdown()
                worker.join(timeout=5)
        assert all(not worker.is_alive() for worker in workers)

    assert requests == ports
    assert exercises == [
        (f"http://127.0.0.1:{ports[0]}", None, (0, 0)),
        (f"http://127.0.0.1:{ports[1]}", "workspace", (0, 0)),
    ]
