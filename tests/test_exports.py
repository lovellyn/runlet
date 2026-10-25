import asyncio
import hashlib
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import urlsplit

import httpx2 as httpx
import pytest

import runlet.exports as module
from runlet.errors import SandboxError
from runlet.exports import Exports, _open_source
from runlet.server import create_app
from runlet.workspaces import Workspace, Workspaces
from tests.test_mcp import connect


@pytest.fixture
def export_app(settings):
    @asynccontextmanager
    async def opened():
        app = create_app(replace(settings, public_base_url="http://localhost"))
        services = app.state.services
        directory = services.workspaces.root / "export-test"
        (directory / "work").mkdir(parents=True)
        workspace = Workspace("export-test", directory, None, "test")
        services.workspaces.items[workspace.workspace_id] = workspace
        # AnyIO lifespan scopes must enter and exit in the same task as the test.
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as http:
                yield app, http, workspace

    return opened


async def test_mcp_export_downloads_exact_snapshot_after_edit_and_workspace_deletion(export_app):
    async with export_app() as (app, http, workspace):
        payload = bytes(range(256)) * 6000  # Larger than the native file_read/write limit.
        filename = "résultat final.zip"
        (workspace.work / filename).write_bytes(payload)
        http.headers["X-Sandbox-Token"] = app.state.services.settings.token
        async with connect(http) as client:
            result = await client.call_tool(
                "file_export", {"workspace_id": workspace.workspace_id, "path": filename}
            )
            assert not result.is_error, result.content
            exported = result.structured_content
            assert exported["filename"] == filename
            assert exported["mime_type"] == "application/zip"
            assert exported["size_bytes"] == len(payload)
            assert exported["sha256"] == hashlib.sha256(payload).hexdigest()
            assert app.state.services.settings.token not in exported["download_url"]
            assert "content" not in exported
        await app.state.services.files.write(workspace.workspace_id, filename, "changed")
        await app.state.services.workspaces.delete(workspace.workspace_id)
        http.headers.clear()
        response = await http.get(exported["download_url"])
        assert response.status_code == 200
        assert response.content == payload
        assert response.headers["content-length"] == str(len(payload))
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["content-disposition"].startswith("attachment; filename*=utf-8''")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
        head = await http.head(exported["download_url"])
        assert head.status_code == 200 and head.content == b""
        assert head.headers["content-length"] == str(len(payload))
        partial = await http.get(exported["download_url"], headers={"Range": "bytes=7-22"})
        assert partial.status_code == 206 and partial.content == payload[7:23]
        assert (
            await http.get(exported["download_url"], headers={"Range": "bytes=99999999-"})
        ).status_code == 416


async def test_download_capability_never_authorizes_mcp_or_another_file(export_app):
    async with export_app() as (app, http, workspace):
        (workspace.work / "file.txt").write_text("private")
        exported = await app.state.services.exports.create(workspace.workspace_id, "file.txt")
        url = exported["download_url"]
        assert (await http.get(url)).text == "private"
        token = urlsplit(url).path.rsplit("/", 1)[-1]
        wrong = ("A" if token[0] != "A" else "B") + token[1:]
        assert (await http.get(f"/downloads/{wrong}")).status_code == 404
        assert (await http.get("/downloads/" + "A" * 43)).status_code == 404
        assert (await http.get(f"/downloads/{token}/file.txt")).status_code == 401
        assert (await http.post(url)).status_code == 401
        assert (
            await http.post("/mcp", json={}, headers={"X-Sandbox-Token": token})
        ).status_code == 401
        assert (await http.post("/mcp", json={}, params={"token": token})).status_code == 401


async def test_expiry_cleanup_and_restart_invalidate_links(export_app):
    async with export_app() as (app, http, workspace):
        manager = app.state.services.exports
        (workspace.work / "file.txt").write_text("snapshot")
        exported = await manager.create(workspace.workspace_id, "file.txt")
        token = urlsplit(exported["download_url"]).path.rsplit("/", 1)[-1]
        record = manager.items[token]
        record.deadline = 0
        assert (await http.get(exported["download_url"])).status_code == 404
        record.readers = 1
        await manager.cleanup()
        assert record.path.exists() and token in manager.items
        record.readers = 0
        await manager.cleanup()
        assert not record.path.exists() and token not in manager.items
        exported = await manager.create(workspace.workspace_id, "file.txt")
        await manager.close()
        new = Exports(app.state.services.workspaces)
        await new.start()
        try:
            assert (
                new.response(urlsplit(exported["download_url"]).path.rsplit("/", 1)[-1]).status_code
                == 404
            )
            assert list(new.directory.iterdir()) == []
        finally:
            await new.close()


async def test_expiry_does_not_interrupt_an_admitted_download(export_app):
    async with export_app() as (app, _, workspace):
        manager = app.state.services.exports
        payload = b"x" * 200_000
        (workspace.work / "file.bin").write_bytes(payload)
        exported = await manager.create(workspace.workspace_id, "file.bin")
        token = urlsplit(exported["download_url"]).path.rsplit("/", 1)[-1]
        record, chunks = manager.items[token], []

        async def send(message):
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                record.deadline = 0
                await manager.cleanup()
                assert record.path.exists() and record.readers == 1

        async def receive():
            return {"type": "http.disconnect"}

        await manager.response(token)(
            {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}},
            receive,
            send,
        )
        assert b"".join(chunks) == payload and record.readers == 0
        await manager.cleanup()
        assert not record.path.exists()


async def test_failed_download_releases_its_reservation(export_app):
    async with export_app() as (app, _, workspace):
        manager = app.state.services.exports
        (workspace.work / "file.bin").write_bytes(b"x" * 200_000)
        exported = await manager.create(workspace.workspace_id, "file.bin")
        token = urlsplit(exported["download_url"]).path.rsplit("/", 1)[-1]

        async def send(message):
            raise OSError("client disconnected")

        async def receive():
            return {"type": "http.disconnect"}

        with pytest.raises(OSError):
            await manager.response(token)(
                {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
        assert manager.items[token].readers == 0


async def test_export_limits_reserve_parallel_copies_and_release_failures(export_app, monkeypatch):
    async with export_app() as (app, _, workspace):
        manager = app.state.services.exports
        manager.settings = replace(manager.settings, max_exports=1, max_export_bytes=4)
        path = workspace.work / "file.bin"
        path.write_bytes(b"12345")
        with pytest.raises(SandboxError, match="file_too_large"):
            await manager.create(workspace.workspace_id, path.name)
        assert manager._reserved == 0 and not manager.items
        assert list(manager.directory.iterdir()) == []
        path.write_bytes(b"1234")
        entered, release = threading.Event(), threading.Event()
        original = module._snapshot

        def slow_copy(*args):
            entered.set()
            assert release.wait(5)
            return original(*args)

        monkeypatch.setattr(module, "_snapshot", slow_copy)
        copying = asyncio.create_task(manager.create(workspace.workspace_id, path.name))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            with pytest.raises(SandboxError, match="export_limit"):
                await manager.create(workspace.workspace_id, path.name)
            with pytest.raises(SandboxError, match="workspace_busy"):
                await app.state.services.workspaces.delete(workspace.workspace_id)
        finally:
            release.set()
        await copying
        assert manager._reserved == 0 and len(manager.items) == 1


async def test_export_cancellation_waits_for_copy_and_removes_orphan(export_app, monkeypatch):
    async with export_app() as (app, _, workspace):
        manager = app.state.services.exports
        (workspace.work / "file.bin").write_bytes(b"data")
        entered, release = threading.Event(), threading.Event()
        original = module._snapshot

        def slow_copy(*args):
            entered.set()
            assert release.wait(5)
            return original(*args)

        monkeypatch.setattr(module, "_snapshot", slow_copy)
        copying = asyncio.create_task(manager.create(workspace.workspace_id, "file.bin"))
        assert await asyncio.to_thread(entered.wait, 5)
        copying.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await copying
        assert manager._reserved == workspace.active_operations == 0
        assert list(manager.directory.iterdir()) == [] and not manager.items


async def test_snapshot_joins_native_file_queue_and_distinct_exports_run_in_parallel(export_app):
    async with export_app() as (app, _, workspace):
        manager = app.state.services.exports
        (workspace.work / "file.bin").write_bytes(b"old")
        async with workspace.file_mutation("file.bin"):
            queued = asyncio.create_task(manager.create(workspace.workspace_id, "file.bin"))
            await asyncio.sleep(0)
            assert not queued.done()
            (workspace.work / "file.bin").write_bytes(b"new")
            other = workspace.work / "other.bin"
            other.write_bytes(b"other")
            await manager.create(workspace.workspace_id, other.name)
        result = await queued
        assert result["sha256"] == hashlib.sha256(b"new").hexdigest()
        results = await asyncio.gather(
            *(manager.create(workspace.workspace_id, "file.bin") for _ in range(4))
        )
        assert len({result["download_url"] for result in results}) == 4


async def test_export_rejects_escaping_and_special_files(export_app, tmp_path):
    async with export_app() as (app, _, workspace):
        manager = app.state.services.exports
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        (workspace.work / "escape").symlink_to(outside)
        for path in (str(outside), "../outside.txt", "escape"):
            with pytest.raises(SandboxError, match="invalid_path"):
                await manager.create(workspace.workspace_id, path)
        os.mkfifo(workspace.work / "pipe")
        for path in ("pipe", "."):
            with pytest.raises(SandboxError, match="invalid_file"):
                await manager.create(workspace.workspace_id, path)
        with pytest.raises(SandboxError, match="export_failed"):
            await manager.create(workspace.workspace_id, "missing")
        assert manager._reserved == 0 and list(manager.directory.iterdir()) == []


def test_snapshot_open_rejects_a_swapped_parent_symlink(tmp_path):
    root, outside = tmp_path / "work", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret").write_text("secret")
    (root / "parent").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        _open_source(root, root / "parent" / "secret")


@pytest.mark.parametrize("growing", [False, True])
def test_source_changes_during_copy_fail_without_retaining_snapshot(tmp_path, monkeypatch, growing):
    source, destination = tmp_path / "source", tmp_path / "snapshot"
    source.write_bytes(b"original")
    original = module._open_source

    class ChangingReader:
        def __init__(self):
            self.reader = original(tmp_path, source)
            self.changed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.reader.close()

        def fileno(self):
            return self.reader.fileno()

        def read(self, size):
            if not self.changed:
                self.changed = True
                source.write_bytes(b"x" * (100 if growing else 8))
                # Make the metadata change deterministic on filesystems with coarse timestamps.
                os.utime(source, ns=(1, 1))
            return self.reader.read(size)

    monkeypatch.setattr(module, "_open_source", lambda *args: ChangingReader())
    expected = "file_too_large" if growing else "file_changed"
    with pytest.raises(SandboxError, match=expected):
        module._snapshot(tmp_path, source, destination, 16)
    assert not destination.exists()


async def test_exports_disabled_until_public_url_is_configured(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        with pytest.raises(SandboxError, match="exports_disabled"):
            await app.state.services.exports.create("any", "file.txt")


async def test_export_public_url_uses_configured_proxy_prefix(export_app):
    async with export_app() as (app, _, workspace):
        manager = app.state.services.exports
        manager.settings = replace(
            manager.settings, public_base_url="https://files.example.com/agent/"
        )
        (workspace.work / "file").write_bytes(b"")
        result = await manager.create(workspace.workspace_id, "file")
        assert result["download_url"].startswith("https://files.example.com/agent/downloads/")
        assert result["mime_type"] == "application/octet-stream" and result["size_bytes"] == 0


async def test_start_removes_abandoned_snapshots(settings):
    manager = Exports(Workspaces(settings))
    manager.directory.mkdir()
    (manager.directory / "orphan").write_bytes(b"stale")
    await manager.start()
    try:
        assert list(manager.directory.iterdir()) == []
    finally:
        await manager.close()
