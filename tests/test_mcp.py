import base64
import io
from dataclasses import replace

import httpx2 as httpx
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types.version import LATEST_MODERN_VERSION
from PIL import Image

from runlet.config import Settings
from runlet.errors import SandboxError
from runlet.server import create_app


def connect(http):
    return Client(
        streamable_http_client("http://localhost/mcp", http_client=http),
        mode=LATEST_MODERN_VERSION,
        cache=None,
    )


def test_missing_token_fails_closed():
    with pytest.raises(ValueError, match="SANDBOX_TOKEN"):
        Settings(token="")


async def test_authentication_and_origin_validation(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
        ) as client:
            assert (await client.get("/healthz")).json() == {"status": "ok"}
            assert (await client.post("/mcp", json={})).status_code == 401
            assert (
                await client.post("/mcp", json={}, headers={"X-Sandbox-Token": "wrong"})
            ).status_code == 401
            assert (
                await client.post(
                    "/mcp",
                    json={},
                    headers={
                        "X-Sandbox-Token": settings.token,
                        "Origin": "https://untrusted.invalid",
                    },
                )
            ).status_code == 403
            assert (
                await client.post(
                    "http://untrusted.invalid/mcp",
                    json={},
                    headers={"X-Sandbox-Token": settings.token},
                )
            ).status_code == 421


async def test_real_mcp_clients_reconnect_and_return_native_images(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={"X-Sandbox-Token": settings.token},
            base_url="http://localhost",
        ) as http:
            async with connect(http) as client:
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert names == {
                    "server_info",
                    "workspace_create",
                    "workspace_list",
                    "workspace_get",
                    "workspace_delete",
                    "exec",
                    "job_status",
                    "job_cancel",
                    "job_write_stdin",
                    "job_resize",
                    "image_read",
                    "file_read",
                    "file_write",
                    "file_edit",
                    "file_export",
                }
                result = await client.call_tool("workspace_create", {})
                assert not result.is_error, result
                workspace_id = result.structured_content["workspace_id"]
                result = await client.call_tool(
                    "file_write",
                    {"workspace_id": workspace_id, "path": "message.txt", "content": "hello"},
                )
                assert not result.is_error, result
                result = await client.call_tool(
                    "file_edit",
                    {
                        "workspace_id": workspace_id,
                        "path": "message.txt",
                        "old_text": "hello",
                        "new_text": "hello, edited",
                    },
                )
                assert result.structured_content["replacements"] == 1
                image = io.BytesIO()
                Image.new("RGB", (48, 24), "blue").save(image, format="PNG")
                await client.call_tool(
                    "file_write",
                    {
                        "workspace_id": workspace_id,
                        "path": "image.png",
                        "encoding": "base64",
                        "content": base64.b64encode(image.getvalue()).decode(),
                    },
                )
                result = await client.call_tool(
                    "image_read", {"workspace_id": workspace_id, "path": "image.png"}
                )
                assert not result.is_error, result
                assert result.structured_content is None
                block = next(block for block in result.content if block.type == "image")
                assert block.mime_type == "image/png"
                with Image.open(io.BytesIO(base64.b64decode(block.data))) as decoded:
                    assert decoded.size == (48, 24)
                result = await client.call_tool(
                    "exec",
                    {
                        "workspace_id": workspace_id,
                        "command": "sleep 0.3; cat message.txt",
                        "wait_seconds": 0,
                    },
                )
                assert not result.is_error, result
                job_id = result.structured_content["job_id"]
            async with connect(http) as client:
                await app.state.services.jobs.get(job_id).task
                result = await client.call_tool("job_status", {"job_id": job_id})
                assert result.structured_content["state"] == "completed"
                assert result.structured_content["output"] == "hello, edited"
                missing = await client.call_tool(
                    "workspace_get", {"workspace_id": "does-not-exist"}
                )
                assert missing.is_error
                listed = await client.call_tool("workspace_list", {})
                assert listed.structured_content["workspaces"][0]["workspace_id"] == workspace_id
    assert app.state.services.jobs._closing


async def test_new_server_boot_does_not_claim_old_jobs_survived(settings):
    first = create_app(settings)
    services = first.state.services
    workspace_id = (await services.workspaces.create())["workspace_id"]
    old = await services.jobs.start(workspace_id, "printf done")
    await services.jobs.shutdown()
    second = create_app(settings)
    assert second.state.services.boot_id != services.boot_id
    assert second.state.services.workspaces.get(workspace_id).work.exists()
    with pytest.raises(SandboxError, match="job_not_found"):
        await second.state.services.jobs.status(old["job_id"])


async def test_domain_error_codes_survive_mcp_transport(settings):
    app = create_app(replace(settings, max_jobs=1))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={"X-Sandbox-Token": settings.token},
            base_url="http://localhost",
        ) as http:
            async with connect(http) as client:

                async def expect_error(name, code, **arguments):
                    result = await client.call_tool(name, arguments)
                    assert result.is_error
                    text = "\n".join(block.text for block in result.content if block.type == "text")
                    assert code in text, text

                await expect_error("workspace_create", "invalid_workspace_id", workspace_id="")
                await expect_error(
                    "workspace_get", "workspace_not_found", workspace_id="does-not-exist"
                )
                created = await client.call_tool("workspace_create", {})
                workspace_id = created.structured_content["workspace_id"]
                await client.call_tool(
                    "file_write",
                    {"workspace_id": workspace_id, "path": "ambiguous.txt", "content": "old old"},
                )
                await expect_error(
                    "file_edit",
                    "edit_ambiguous",
                    workspace_id=workspace_id,
                    path="ambiguous.txt",
                    old_text="old",
                    new_text="new",
                )
                started = await client.call_tool(
                    "exec", {"workspace_id": workspace_id, "command": "cat", "wait_seconds": 0}
                )
                job_id = started.structured_content["job_id"]
                try:
                    sent = await client.call_tool(
                        "job_write_stdin",
                        {"job_id": job_id, "chars": "first\n", "expected_input_offset": 0},
                    )
                    assert not sent.is_error
                    await expect_error(
                        "job_write_stdin",
                        "stdin_conflict",
                        job_id=job_id,
                        chars="replay",
                        expected_input_offset=0,
                    )
                    await expect_error(
                        "exec",
                        "server_busy",
                        workspace_id=workspace_id,
                        command="printf must-not-run",
                        wait_seconds=0,
                    )
                finally:
                    await client.call_tool("job_cancel", {"job_id": job_id})


async def test_unexpected_tool_exceptions_remain_masked(settings, monkeypatch):
    app = create_app(settings)

    async def crash(*args, **kwargs):
        raise RuntimeError("private-internal-diagnostic")

    monkeypatch.setattr(app.state.services.files, "read", crash)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers={"X-Sandbox-Token": settings.token},
            base_url="http://localhost",
        ) as http:
            async with connect(http) as client:
                result = await client.call_tool(
                    "file_read", {"workspace_id": "unused", "path": "unused.txt"}
                )
                assert result.is_error
                text = "\n".join(block.text for block in result.content if block.type == "text")
                assert "private-internal-diagnostic" not in text
                assert "RuntimeError" not in text
                assert not (await client.call_tool("server_info", {})).is_error
