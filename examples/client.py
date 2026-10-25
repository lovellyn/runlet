"""Smoke client: export SANDBOX_TOKEN, then run python examples/client.py."""

import asyncio
import os

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
                while True:
                    print(result["output"], end="")
                    if result["state"] != "running" and not result["has_more"]:
                        break
                    result = data(
                        await client.call_tool(
                            "job_status",
                            {
                                "job_id": job_id,
                                "cursor": result["next_cursor"],
                                "wait_seconds": 2,
                            },
                        )
                    )
                print(f"state={result['state']} exit_code={result['exit_code']}")
                if result["state"] != "completed" or result["exit_code"] != 0:
                    raise RuntimeError("Sandbox command failed")
            finally:
                if job_id is not None:
                    data(await client.call_tool("job_cancel", {"job_id": job_id}))
                data(await client.call_tool("workspace_delete", {"workspace_id": workspace_id}))


if __name__ == "__main__":
    asyncio.run(main())
