"""Exercise a built image using disposable Docker resources and a real MCP client."""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit

import httpx2 as httpx
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types.version import LATEST_MODERN_VERSION


def docker(*args: str, **kwargs) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, **kwargs
    ).stdout.strip()


def wait_ready(url: str) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=2) as response:
                if json.load(response) == {"status": "ok"}:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            # Docker's port mapping may reset/close HTTP connections during startup.
            pass
        time.sleep(0.25)
    raise RuntimeError("Container did not become ready within 45 seconds")


async def exercise(
    url: str,
    token: str,
    workspace_id: str | None = None,
    *,
    identity: tuple[int, int] | None = None,
) -> tuple[str, str]:
    async with httpx.AsyncClient(
        headers={"X-Sandbox-Token": token},
        timeout=httpx.Timeout(120, connect=5),
    ) as http:
        unauthorized = await http.post(f"{url}/mcp", headers={"X-Sandbox-Token": "wrong"}, json={})
        assert unauthorized.status_code == 401, unauthorized.text
        async with Client(
            streamable_http_client(f"{url}/mcp", http_client=http),
            mode=LATEST_MODERN_VERSION,
            cache=None,
        ) as client:

            async def call(name: str, **arguments) -> dict:
                result = await client.call_tool(name, arguments)
                assert not result.is_error, result.content
                return result.structured_content

            info = await call("server_info")
            payload = bytes(range(256)) * 20_000
            checksum = hashlib.sha256(payload).hexdigest()
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert len(names) == 16, names
            assert {
                "job_resize",
                "image_read",
                "job_write_stdin",
                "file_export",
                "file_upload",
            } <= names
            invalid = await client.call_tool("workspace_create", {"workspace_id": ""})
            assert invalid.is_error
            assert any(
                block.type == "text" and "invalid_workspace_id" in block.text
                for block in invalid.content
            )
            if workspace_id is None:
                workspace_id = (await call("workspace_create"))["workspace_id"]
                await call(
                    "file_write", workspace_id=workspace_id, path="persist.txt", content="retained"
                )
            else:
                await call("workspace_get", workspace_id=workspace_id)
                marker = await call("file_read", workspace_id=workspace_id, path="persist.txt")
                assert marker["content"] == "retained", marker
                retained = await call("file_export", workspace_id=workspace_id, path="received.bin")
                assert retained["sha256"] == checksum

            assert info["uploads"]["enabled"]
            upload = await call(
                "file_upload",
                workspace_id=workspace_id,
                path="received.bin",
                size_bytes=len(payload),
                sha256=checksum,
                overwrite=True,
            )

            async def chunks():
                for offset in range(0, len(payload), 65_536):
                    yield payload[offset : offset + 65_536]

            async with httpx.AsyncClient() as uploader:
                upload_path = urlsplit(upload["upload_url"]).path
                response = await uploader.put(f"{url}{upload_path}", content=chunks())
                assert response.status_code == 201, response.text
                assert response.json()["sha256"] == checksum
                assert (await uploader.put(f"{url}{upload_path}", content=b"")).status_code == 404
            received = await call("file_export", workspace_id=workspace_id, path="received.bin")
            assert received["size_bytes"] == len(payload) and received["sha256"] == checksum
            assert info["exports"]["enabled"]
            exported = await call("file_export", workspace_id=workspace_id, path="persist.txt")
            download_path = urlsplit(exported["download_url"]).path
            async with httpx.AsyncClient() as downloader:
                downloaded = await downloader.get(f"{url}{download_path}")
                assert downloaded.status_code == 200 and downloaded.content == b"retained"
                partial = await downloader.get(
                    f"{url}{download_path}", headers={"Range": "bytes=0-2"}
                )
                assert partial.status_code == 206 and partial.content == b"ret"

            identity_check = ""
            if identity is not None:
                uid, gid = identity
                identity_check += f'test "$(id -u)" = {uid}; test "$(id -g)" = {gid}; '
            command = (
                "set -e; "
                + identity_check
                + 'python -c "import sys; assert sys.version_info >= (3, 12)"; '
                "node --version; npm --version; git --version; gh --version; "
                "ffmpeg -version >/dev/null; ffprobe -version >/dev/null; "
                "gcc --version >/dev/null; rg --version >/dev/null; jq --version; "
                "test -s /usr/local/share/doc/node/LICENSE; "
                "test -s /usr/local/lib/node_modules/npm/LICENSE; "
                "test -s /usr/local/share/doc/gh/LICENSE; "
                "test -s /app/THIRD_PARTY_NOTICES.md; "
                "printf toolchain-ok"
            )
            result = await call("exec", workspace_id=workspace_id, command=command)
            while result["state"] == "running":
                result = await call("job_status", job_id=result["job_id"], wait_seconds=2)
            assert result["state"] == "completed" and result["exit_code"] == 0, result
            assert "toolchain-ok" in result["output"], result

            jobs = await asyncio.gather(
                *(
                    call(
                        "exec",
                        workspace_id=workspace_id,
                        command=f"sleep 0.3; printf job-{number}",
                        wait_seconds=0,
                    )
                    for number in range(2)
                )
            )
            assert len({job["job_id"] for job in jobs}) == 2
            await call("file_write", workspace_id=workspace_id, path="parallel.txt", content="old")
            await call(
                "file_edit",
                workspace_id=workspace_id,
                path="parallel.txt",
                old_text="old",
                new_text="new",
            )
            assert (await call("file_read", workspace_id=workspace_id, path="parallel.txt"))[
                "content"
            ] == "new"
            for number, job in enumerate(jobs):
                while job["state"] == "running":
                    job = await call("job_status", job_id=job["job_id"], wait_seconds=2)
                assert job["state"] == "completed" and job["exit_code"] == 0, job
                assert job["output"] == f"job-{number}", job
            # Keep long-lived jobs active while checking both admission boundaries.
            assert info["max_concurrent_jobs"] == 3 and info["max_jobs_per_workspace"] == 2
            other_workspace = (await call("workspace_create"))["workspace_id"]
            held = []
            try:
                for _ in range(2):
                    held.append(
                        await call(
                            "exec", workspace_id=workspace_id, command="sleep 30", wait_seconds=0
                        )
                    )
                blocked = await client.call_tool(
                    "exec", {"workspace_id": workspace_id, "command": "true", "wait_seconds": 0}
                )
                assert blocked.is_error and any(
                    block.type == "text" and "workspace_job_limit" in block.text
                    for block in blocked.content
                )
                held.append(
                    await call(
                        "exec", workspace_id=other_workspace, command="sleep 30", wait_seconds=0
                    )
                )
                blocked = await client.call_tool(
                    "exec", {"workspace_id": other_workspace, "command": "true", "wait_seconds": 0}
                )
                assert blocked.is_error and any(
                    block.type == "text" and "server_busy" in block.text
                    for block in blocked.content
                )
            finally:
                for job in held:
                    await call("job_cancel", job_id=job["job_id"])
                await call("workspace_delete", workspace_id=other_workspace)
            recovered = await call("exec", workspace_id=workspace_id, command="printf recovered")
            assert recovered["state"] == "completed" and recovered["output"] == "recovered"
            return info["boot_id"], workspace_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-uid", type=int, help="Optional deployment-specific UID")
    parser.add_argument("--expected-gid", type=int, help="Optional deployment-specific GID")
    arguments = parser.parse_args()
    suffix = uuid.uuid4().hex[:12]
    name, volume = f"runlet-smoke-{suffix}", f"runlet-data-{suffix}"
    token = secrets.token_urlsafe(32)
    container_created = False
    volume_created = False
    try:
        docker("volume", "create", volume)
        volume_created = True
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--env",
            "SANDBOX_TOKEN",
            "--env",
            "PUBLIC_BASE_URL=http://localhost:8000",
            "--env",
            "MAX_CONCURRENT_JOBS=3",
            "--env",
            "MAX_JOBS_PER_WORKSPACE=2",
            "--publish",
            "127.0.0.1::8000",
            "--mount",
            f"type=volume,src={volume},dst=/data",
            arguments.image,
            env={**os.environ, "SANDBOX_TOKEN": token},
        )
        container_created = True
        uid = int(docker("exec", name, "id", "-u"))
        gid = int(docker("exec", name, "id", "-g"))
        if arguments.expected_uid is not None:
            assert uid == arguments.expected_uid, (uid, arguments.expected_uid)
        if arguments.expected_gid is not None:
            assert gid == arguments.expected_gid, (gid, arguments.expected_gid)
        endpoint = docker("port", name, "8000/tcp").splitlines()[0]
        url = f"http://{endpoint}"
        wait_ready(url)
        first_boot, workspace_id = asyncio.run(exercise(url, token, identity=(uid, gid)))
        docker("restart", "--time", "10", name)
        # Docker may assign a different ephemeral host port after a restart.
        endpoint = docker("port", name, "8000/tcp").splitlines()[0]
        url = f"http://{endpoint}"
        wait_ready(url)
        second_boot, same_id = asyncio.run(exercise(url, token, workspace_id, identity=(uid, gid)))
        assert second_boot != first_boot and same_id == workspace_id
        print(
            "Container smoke passed: auth, MCP, streamed uploads, downloads, identity, toolchain, "
            "parallel jobs, workspace/global limits, persistence"
        )
    except BaseException:
        if container_created:
            logs = subprocess.run(
                ["docker", "logs", "--tail", "60", name], capture_output=True, text=True
            )
            print(logs.stdout, logs.stderr)
        raise
    finally:
        if container_created:
            subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True)
        if volume_created:
            subprocess.run(["docker", "volume", "rm", volume], check=False, capture_output=True)


if __name__ == "__main__":
    main()
