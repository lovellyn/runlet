import asyncio
import hashlib
import os
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import urlsplit

import httpx2 as httpx
import pytest
from starlette.requests import Request

import runlet.uploads as module
from runlet.errors import SandboxError
from runlet.server import create_app
from runlet.uploads import Uploads, _publish
from runlet.workspaces import Workspace
from tests.test_mcp import connect


@pytest.fixture
def upload_app(settings):
    @asynccontextmanager
    async def opened(**changes):
        app = create_app(replace(settings, public_base_url="http://localhost", **changes))
        directory = app.state.services.workspaces.root / "upload-test"
        (directory / "work").mkdir(parents=True)
        workspace = Workspace("upload-test", directory, None, "test")
        app.state.services.workspaces.items[workspace.workspace_id] = workspace
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as http:
                yield app, http, workspace

    return opened


def ticket(app, workspace, path="input.bin", data=b"contents", **options):
    return app.state.services.uploads.create(
        workspace.workspace_id, path, len(data), hashlib.sha256(data).hexdigest(), **options
    )


def assert_idle(app, workspace):
    manager = app.state.services.uploads
    assert not manager._active
    assert workspace.active_operations == 0
    assert list(manager.directory.iterdir()) == []


async def test_mcp_upload_streams_binary_larger_than_mcp_body_limit_and_exports(upload_app):
    async with upload_app() as (app, http, workspace):
        payload = bytes(range(256)) * 20_000  # 5.12 MB, above the 4 MiB MCP body limit.
        digest = hashlib.sha256(payload).hexdigest()
        http.headers["X-Sandbox-Token"] = app.state.services.settings.token
        async with connect(http) as client:
            result = await client.call_tool("server_info", {})
            assert result.structured_content["uploads"]["enabled"]
            result = await client.call_tool(
                "file_upload",
                {
                    "workspace_id": workspace.workspace_id,
                    "path": "assets/résultat final.zip",
                    "size_bytes": len(payload),
                    "sha256": digest,
                },
            )
            assert not result.is_error, result.content
            upload = result.structured_content
            assert upload["sha256"] == digest
            assert "content" not in upload and "data" not in upload
            assert app.state.services.settings.token not in upload["upload_url"]
        http.headers.clear()

        async def body():
            for offset in range(0, len(payload), 65_536):
                yield payload[offset : offset + 65_536]

        response = await http.put(upload["upload_url"], content=body())
        assert response.status_code == 201, response.text
        assert response.json() == {
            "path": "assets/résultat final.zip",
            "size_bytes": len(payload),
            "sha256": digest,
        }
        assert response.headers["cache-control"] == "no-store"
        assert (workspace.work / upload["path"]).read_bytes() == payload
        exported = await app.state.services.exports.create(workspace.workspace_id, upload["path"])
        assert (await http.get(exported["download_url"])).content == payload
        assert (await http.put(upload["upload_url"], content=payload)).status_code == 404
        assert_idle(app, workspace)


@pytest.mark.parametrize("payload", [b"", b"\x00\xff\x01", b"hello"])
async def test_empty_and_binary_uploads(upload_app, payload):
    async with upload_app() as (app, http, workspace):
        upload = ticket(app, workspace, data=payload)
        response = await http.put(upload["upload_url"], content=payload)
        assert response.status_code == 201
        assert (workspace.work / "input.bin").read_bytes() == payload
        assert_idle(app, workspace)


async def test_upload_capability_authorizes_only_its_single_put(upload_app):
    async with upload_app() as (app, http, workspace):
        upload = ticket(app, workspace)
        url = upload["upload_url"]
        token = urlsplit(url).path.rsplit("/", 1)[-1]
        assert (await http.put("/uploads/" + "A" * 43, content=b"x")).status_code == 404
        for method in ("GET", "HEAD", "POST", "DELETE"):
            assert (await http.request(method, url)).status_code == 401
        assert (await http.put(url + "/other", content=b"x")).status_code == 401
        assert (
            await http.post("/mcp", headers={"X-Sandbox-Token": token}, json={})
        ).status_code == 401
        assert (await http.put(url, content=b"contents")).status_code == 201


@pytest.mark.parametrize(
    ("headers", "status"),
    [({"Host": "untrusted.invalid"}, 421), ({"Origin": "https://untrusted.invalid"}, 403)],
)
async def test_upload_validates_host_and_origin_before_consuming_ticket(
    upload_app, headers, status
):
    async with upload_app() as (app, http, workspace):
        upload = ticket(app, workspace)
        assert (
            await http.put(upload["upload_url"], headers=headers, content=b"contents")
        ).status_code == status
        assert (await http.put(upload["upload_url"], content=b"contents")).status_code == 201


@pytest.mark.parametrize(
    ("body", "headers", "error", "status"),
    [
        (b"bad", {}, "size_mismatch", 400),
        (b"contentsX", {}, "size_mismatch", 400),
        (b"ContentS", {}, "checksum_mismatch", 400),
        (b"contents", {"Content-Length": "9"}, "size_mismatch", 400),
        (b"contents", {"Content-Length": "oops"}, "invalid_size", 400),
        (b"contents", {"Content-Encoding": "gzip"}, "unsupported_encoding", 415),
    ],
)
async def test_invalid_uploads_never_replace_existing_file(
    upload_app, body, headers, error, status
):
    async with upload_app() as (app, http, workspace):
        target = workspace.work / "input.bin"
        target.write_bytes(b"original")
        upload = ticket(app, workspace, overwrite=True)
        response = await http.put(upload["upload_url"], content=body, headers=headers)
        assert response.status_code == status
        assert response.json()["error"] == error
        assert target.read_bytes() == b"original"
        assert (await http.put(upload["upload_url"], content=b"contents")).status_code == 404
        assert_idle(app, workspace)


@pytest.mark.parametrize("body", [b"short", b"contentsX"])
async def test_chunked_upload_enforces_actual_byte_count(upload_app, body):
    async with upload_app() as (app, http, workspace):
        upload = ticket(app, workspace)

        async def chunks():
            yield body[:2]
            yield body[2:]

        response = await http.put(upload["upload_url"], content=chunks())
        assert response.status_code == (413 if len(body) > 8 else 400)
        assert not (workspace.work / "input.bin").exists()
        assert_idle(app, workspace)


async def test_upload_is_atomic_without_locking_during_network_receive(upload_app):
    async with upload_app() as (app, http, workspace):
        target = workspace.work / "input.bin"
        target.write_bytes(b"original")
        target.chmod(0o750)
        upload = ticket(app, workspace, overwrite=True)
        entered, release = asyncio.Event(), asyncio.Event()

        async def chunks():
            yield b"cont"
            entered.set()
            await release.wait()
            yield b"ents"

        receiving = asyncio.create_task(http.put(upload["upload_url"], content=chunks()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert target.read_bytes() == b"original"
            await asyncio.wait_for(
                app.state.services.files.write(workspace.workspace_id, "input.bin", "intervening"),
                2,
            )
            assert target.read_bytes() == b"intervening"
            with pytest.raises(SandboxError, match="workspace_busy"):
                await app.state.services.workspaces.delete(workspace.workspace_id)
            # The same ticket cannot be used concurrently.
            assert (await http.put(upload["upload_url"], content=b"contents")).status_code == 404
        finally:
            release.set()
        assert (await receiving).status_code == 201
        assert target.read_bytes() == b"contents"
        assert target.stat().st_mode & 0o777 == 0o750
        assert_idle(app, workspace)


async def test_upload_publication_joins_file_queue_and_other_files_remain_parallel(upload_app):
    async with upload_app() as (app, http, workspace):
        target = workspace.work / "input.bin"
        target.write_bytes(b"old")
        upload = ticket(app, workspace, overwrite=True)
        finished = asyncio.Event()

        async def chunks():
            yield b"contents"
            finished.set()

        async with workspace.file_mutation("input.bin"):
            receiving = asyncio.create_task(http.put(upload["upload_url"], content=chunks()))
            await asyncio.wait_for(finished.wait(), 2)
            await asyncio.sleep(0.02)
            assert not receiving.done() and target.read_bytes() == b"old"
            other = ticket(app, workspace, path="other.bin")
            assert (await http.put(other["upload_url"], content=b"contents")).status_code == 201
        assert (await receiving).status_code == 201
        assert target.read_bytes() == b"contents"
        assert_idle(app, workspace)


async def test_new_file_created_after_ticket_is_never_overwritten(upload_app):
    async with upload_app() as (app, http, workspace):
        upload = ticket(app, workspace)
        target = workspace.work / "input.bin"
        target.write_bytes(b"created-later")
        response = await http.put(upload["upload_url"], content=b"contents")
        assert response.status_code == 409 and response.json()["error"] == "file_exists"
        assert target.read_bytes() == b"created-later"
        assert_idle(app, workspace)


@pytest.mark.parametrize("path", ["../escape", "/tmp/escape", ".", "", "bad\x00path"])
async def test_upload_rejects_invalid_paths(upload_app, path):
    async with upload_app() as (app, _, workspace):
        with pytest.raises(SandboxError, match="invalid_path|invalid_file"):
            ticket(app, workspace, path=path)


async def test_upload_rejects_special_files_and_escaping_symlinks(upload_app, tmp_path):
    async with upload_app() as (app, _, workspace):
        outside = tmp_path / "outside"
        outside.write_bytes(b"private")
        (workspace.work / "escape").symlink_to(outside)
        os.mkfifo(workspace.work / "fifo")
        for path in ("escape", "fifo"):
            with pytest.raises(SandboxError, match="invalid_path|invalid_file"):
                ticket(app, workspace, path=path)


async def test_swapped_parent_symlink_cannot_publish_outside_workspace(upload_app, tmp_path):
    async with upload_app() as (app, http, workspace):
        upload = ticket(app, workspace, path="nested/input.bin")
        outside = tmp_path / "outside"
        outside.mkdir()
        (workspace.work / "nested").symlink_to(outside, target_is_directory=True)
        response = await http.put(upload["upload_url"], content=b"contents")
        assert response.status_code == 400 and response.json()["error"] == "invalid_path"
        assert list(outside.iterdir()) == []
        assert_idle(app, workspace)


def test_publish_itself_refuses_swapped_parent_symlink(tmp_path):
    work, outside = tmp_path / "work", tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    temporary = tmp_path / "temporary"
    temporary.write_bytes(b"contents")
    (work / "parent").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        _publish(work, "parent/input.bin", temporary, True)
    assert list(outside.iterdir()) == []


async def test_expiry_limits_and_deleted_workspace_tickets(upload_app):
    async with upload_app(max_uploads=1) as (app, http, workspace):
        manager = app.state.services.uploads
        upload = ticket(app, workspace)
        with pytest.raises(SandboxError, match="upload_limit"):
            ticket(app, workspace, path="other")
        next(iter(manager.items.values())).deadline = 0
        assert (await http.put(upload["upload_url"], content=b"contents")).status_code == 404
        upload = ticket(app, workspace)
        await app.state.services.workspaces.delete(workspace.workspace_id)
        assert (await http.put(upload["upload_url"], content=b"contents")).status_code == 404
        assert not manager.items


async def test_active_upload_reserves_limit_and_failure_releases_it(upload_app):
    async with upload_app(max_uploads=1) as (app, http, workspace):
        upload = ticket(app, workspace)
        entered, release = asyncio.Event(), asyncio.Event()

        async def chunks():
            yield b"x"
            entered.set()
            await release.wait()

        receiving = asyncio.create_task(http.put(upload["upload_url"], content=chunks()))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with pytest.raises(SandboxError, match="upload_limit"):
                ticket(app, workspace, path="other.bin")
        finally:
            release.set()
        assert (await receiving).status_code == 400
        ticket(app, workspace)
        assert_idle(app, workspace)


async def test_timeout_and_cancellation_remove_partial_files(upload_app):
    async with upload_app(upload_timeout=0.03) as (app, http, workspace):
        entered = asyncio.Event()

        async def slow():
            yield b"cont"
            entered.set()
            await asyncio.Event().wait()

        upload = ticket(app, workspace)
        response = await http.put(upload["upload_url"], content=slow())
        assert response.status_code == 408 and response.json()["error"] == "upload_timeout"
        assert_idle(app, workspace)
        entered.clear()
        app.state.services.uploads.settings = replace(
            app.state.services.uploads.settings, upload_timeout=3
        )
        upload = ticket(app, workspace)
        receiving = asyncio.create_task(http.put(upload["upload_url"], content=slow()))
        await asyncio.wait_for(entered.wait(), 2)
        receiving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await receiving
        assert not (workspace.work / "input.bin").exists()
        assert_idle(app, workspace)


async def test_disconnect_discards_partial_and_shutdown_cancels_receivers(upload_app):
    async with upload_app() as (app, http, workspace):
        manager = app.state.services.uploads
        upload = ticket(app, workspace)
        token = urlsplit(upload["upload_url"]).path.rsplit("/", 1)[-1]
        events = iter(
            [
                {"type": "http.request", "body": b"cont", "more_body": True},
                {"type": "http.disconnect"},
            ]
        )

        async def receive():
            return next(events)

        request = Request({"type": "http", "headers": []}, receive)
        response = await manager.receive(token, request)
        assert response.status_code == 400
        assert_idle(app, workspace)
        entered = asyncio.Event()

        async def slow():
            yield b"cont"
            entered.set()
            await asyncio.Event().wait()

        upload = ticket(app, workspace)
        receiving = asyncio.create_task(http.put(upload["upload_url"], content=slow()))
        await asyncio.wait_for(entered.wait(), 2)
        await manager.close()
        with pytest.raises(asyncio.CancelledError):
            await receiving
        assert not manager.items and not manager.directory.exists()
        assert workspace.active_operations == 0


async def test_restart_invalidates_tickets_and_cleans_abandoned_files(upload_app):
    async with upload_app() as (app, http, workspace):
        old = app.state.services.uploads
        upload = ticket(app, workspace)
        (old.directory / "abandoned").write_bytes(b"partial")
        new = Uploads(app.state.services.workspaces)
        await old.close()
        new.directory.mkdir()
        (new.directory / "crashed-upload").write_bytes(b"partial")
        await new.start()
        try:
            assert list(new.directory.iterdir()) == []
            assert (
                await new.receive("unknown", Request({"type": "http", "headers": []}))
            ).status_code == 404
            assert (await http.put(upload["upload_url"], content=b"contents")).status_code == 404
        finally:
            await new.close()


@pytest.mark.parametrize(
    ("size", "digest", "error"),
    [
        (-1, "a" * 64, "invalid_size"),
        (True, "a" * 64, "invalid_size"),
        (1.5, "a" * 64, "invalid_size"),
        (100, "a" * 64, "file_too_large"),
        (0, "oops", "invalid_checksum"),
    ],
)
async def test_upload_validates_size_and_checksum_before_allocating(
    upload_app, size, digest, error
):
    async with upload_app(max_upload_bytes=8) as (app, _, workspace):
        with pytest.raises(SandboxError, match=error):
            app.state.services.uploads.create(workspace.workspace_id, "input", size, digest)
        assert not app.state.services.uploads.items


async def test_upload_requires_public_url_and_preserves_proxy_prefix(settings):
    app = create_app(settings)
    with pytest.raises(SandboxError, match="uploads_disabled"):
        app.state.services.uploads.create("unused", "input", 0, hashlib.sha256(b"").hexdigest())
    app = create_app(replace(settings, public_base_url="https://files.example.com/agent/"))
    directory = app.state.services.workspaces.root / "test"
    (directory / "work").mkdir(parents=True)
    workspace = Workspace("test", directory, None, "test")
    app.state.services.workspaces.items["test"] = workspace
    upload = ticket(app, workspace)
    assert upload["upload_url"].startswith("https://files.example.com/agent/uploads/")


async def test_cancellation_waits_for_atomic_publication_before_releasing_workspace(
    upload_app, monkeypatch
):
    async with upload_app() as (app, http, workspace):
        upload = ticket(app, workspace)
        entered, release = threading.Event(), threading.Event()
        original = module._publish

        def slow_publish(*args):
            entered.set()
            assert release.wait(5)
            return original(*args)

        monkeypatch.setattr(module, "_publish", slow_publish)
        receiving = asyncio.create_task(http.put(upload["upload_url"], content=b"contents"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            receiving.cancel()
            await asyncio.sleep(0)
            assert not receiving.done()
            assert workspace.active_operations > 0
            with pytest.raises(SandboxError, match="workspace_busy"):
                await app.state.services.workspaces.delete(workspace.workspace_id)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await receiving
        # Publication may complete after cancellation; it must always be a complete file.
        assert (workspace.work / "input.bin").read_bytes() == b"contents"
        assert_idle(app, workspace)


async def test_io_failure_masks_diagnostics_and_removes_staging_file(upload_app, monkeypatch):
    async with upload_app() as (app, http, workspace):
        upload = ticket(app, workspace)
        original = module.os.fdopen

        class FailedDisk:
            def __init__(self, descriptor, mode):
                self.stream = original(descriptor, mode)

            def write(self, data):
                self.stream.write(data)
                raise OSError("private-diagnostic-disk-full")

            def close(self):
                self.stream.close()
                raise OSError("private-diagnostic-close-failed")

        monkeypatch.setattr(module.os, "fdopen", FailedDisk)
        response = await http.put(upload["upload_url"], content=b"contents")
        assert response.status_code == 500
        assert response.json() == {"error": "upload_failed"}
        assert not (workspace.work / "input.bin").exists()
        assert_idle(app, workspace)
