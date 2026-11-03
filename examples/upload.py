"""Upload a local file: python -m examples.upload WORKSPACE_ID SOURCE DESTINATION."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx2 as httpx
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types.version import LATEST_MODERN_VERSION

from examples.client import connection_headers, data


def _fingerprint(path: Path) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        while chunk := source.read(1_048_576):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


async def upload_file(
    client: Client,
    workspace_id: str,
    source: Path,
    destination: str,
    *,
    overwrite: bool = False,
    gateway_headers: dict[str, str] | None = None,
) -> dict:
    """Send metadata over MCP and stream file bytes through a separate HTTP connection."""
    size, checksum = await asyncio.to_thread(_fingerprint, source)
    upload = data(
        await client.call_tool(
            "file_upload",
            {
                "workspace_id": workspace_id,
                "path": destination,
                "size_bytes": size,
                "sha256": checksum,
                "overwrite": overwrite,
            },
        )
    )

    async def chunks():
        with source.open("rb") as stream:
            while chunk := await asyncio.to_thread(stream.read, 65_536):
                yield chunk

    # The upload ticket authorizes one destination. Do not forward the sandbox's main key.
    async with httpx.AsyncClient(
        headers={"User-Agent": "Runlet-Client/0.4.0", **(gateway_headers or {})},
        timeout=httpx.Timeout(360, connect=10),
    ) as uploader:
        response = await uploader.put(
            upload["upload_url"],
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
            content=chunks(),
        )
        response.raise_for_status()
        result = response.json()
        if result != {"path": destination, "size_bytes": size, "sha256": checksum}:
            raise RuntimeError("Upload response did not match the requested file")
        return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace_id")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", help="Workspace-relative destination path")
    parser.add_argument("--overwrite", action="store_true")
    arguments = parser.parse_args()
    url = os.getenv("SANDBOX_URL", "http://localhost:8000/mcp")
    gateway_headers = {}
    if gateway_token := os.getenv("GATEWAY_TOKEN"):
        gateway_headers["Authorization"] = f"Bearer {gateway_token}"
    async with httpx.AsyncClient(
        headers=connection_headers(), timeout=httpx.Timeout(120, connect=10)
    ) as http:
        async with Client(
            streamable_http_client(url, http_client=http),
            mode=LATEST_MODERN_VERSION,
            cache=None,
        ) as client:
            result = await upload_file(
                client,
                arguments.workspace_id,
                arguments.source,
                arguments.destination,
                overwrite=arguments.overwrite,
                gateway_headers=gateway_headers,
            )
            print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
