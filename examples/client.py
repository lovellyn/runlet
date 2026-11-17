"""Smoke client: export SANDBOX_TOKEN, then run python examples/client.py."""

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Literal

import httpx2 as httpx
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types.version import LATEST_MODERN_VERSION


def connection_headers() -> dict[str, str]:
    headers = {"X-Sandbox-Token": os.environ["SANDBOX_TOKEN"]}
    if gateway_token := os.getenv("GATEWAY_TOKEN"):
        headers["Authorization"] = f"Bearer {gateway_token}"
    return headers


def data(result) -> dict:
    if result.is_error:
        raise RuntimeError(result.content)
    return result.structured_content


async def follow_job(
    client: Client,
    result: dict,
    *,
    wait_for: Literal["completion", "output"] = "completion",
    wait_seconds: float = 20,
) -> AsyncIterator[dict]:
    """Follow a job in host code; yield snapshots without accumulating logs in memory.

    Consumers can spool output or update a UI without invoking the model per response.
    Truncation flags remain visible. Errors propagate; this never replays a command or
    cancels its job when the consumer stops waiting. Complete logs belong in a server file.
    """
    if not 0 < wait_seconds <= 20:
        raise ValueError("Use 0 < wait_seconds <= 20 for a polling loop")
    if wait_for not in {"completion", "output"}:
        raise ValueError("wait_for must be completion or output")
    while True:
        yield result
        if result["state"] != "running" and not result["has_more"]:
            return
        # Drain available pages immediately, including bytes retained after termination.
        immediate = result["has_more"] or result["state"] != "running"
        result = data(
            await client.call_tool(
                "job_status",
                {
                    "job_id": result["job_id"],
                    "cursor": result["next_cursor"],
                    "wait_seconds": 0 if immediate else wait_seconds,
                    "wait_for": wait_for,
                },
            )
        )


async def main() -> None:
    url = os.getenv("SANDBOX_URL", "http://localhost:8000/mcp")
    async with httpx.AsyncClient(
        headers=connection_headers(),
        timeout=httpx.Timeout(600, connect=10),
    ) as http:
        async with Client(
            streamable_http_client(url, http_client=http),
            mode=LATEST_MODERN_VERSION,
            cache=None,
        ) as client:
            created = data(await client.call_tool("workspace_create", {}))
            workspace_id = created["workspace_id"]
            job_id = None
            try:
                result = data(
                    await client.call_tool(
                        "exec",
                        {
                            "workspace_id": workspace_id,
                            "command": "python -c \"print('Hello from the sandbox')\"",
                        },
                    )
                )
                job_id = result["job_id"]
                async for snapshot in follow_job(client, result):
                    result = snapshot
                    if result["truncated"]:
                        print("[Earlier output was truncated]")
                    print(result["output"], end="")
                print(f"state={result['state']} exit_code={result['exit_code']}")
                if result["state"] != "completed" or result["exit_code"] != 0:
                    raise RuntimeError("Sandbox command failed")
            finally:
                if job_id is not None:
                    data(await client.call_tool("job_cancel", {"job_id": job_id}))
                data(await client.call_tool("workspace_delete", {"workspace_id": workspace_id}))


if __name__ == "__main__":
    asyncio.run(main())
