"""Authenticated Streamable HTTP MCP server."""

import base64
import hmac
import json
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any, Literal

import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent, TextContent, ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings
from .exports import DOWNLOAD_PATH, Exports
from .files import Files
from .images import (
    DEFAULT_IMAGE_DIMENSION,
    MAX_CONCURRENT_IMAGES,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_OUTPUT_BYTES,
    MAX_IMAGE_PIXELS,
)
from .jobs import MAX_STDIN_BYTES, Jobs
from .workspaces import Workspaces

APP_VERSION = version("runlet")
MCP_VERSION = version("mcp")


class SandboxTokenMiddleware:
    """Authenticate tool access independently of any upstream gateway credentials."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.expected = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] == "GET" and scope["path"] in {"/", "/healthz"}:
            await self.app(scope, receive, send)
            return
        if scope["method"] in {"GET", "HEAD"} and DOWNLOAD_PATH.fullmatch(scope["path"]):
            # The route validates a temporary, single-file capability instead of the main key.
            await self.app(scope, receive, send)
            return
        sandbox_headers = [v for k, v in scope["headers"] if k.lower() == b"x-sandbox-token"]
        authenticated = len(sandbox_headers) == 1 and hmac.compare_digest(
            sandbox_headers[0], self.expected
        )
        if not authenticated:
            response = JSONResponse(
                {"error": "unauthorized", "message": "Provide a valid X-Sandbox-Token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


@dataclass
class Services:
    settings: Settings
    boot_id: str
    workspaces: Workspaces
    jobs: Jobs
    files: Files
    exports: Exports


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    boot_id = uuid.uuid4().hex[:16]
    workspaces = Workspaces(settings)
    jobs = Jobs(settings, workspaces, boot_id)
    files = Files(workspaces)
    exports = Exports(workspaces)

    @asynccontextmanager
    async def lifespan(_server: MCPServer):
        await exports.start()
        try:
            yield
        finally:
            try:
                await jobs.shutdown()
            finally:
                await exports.close()

    mcp = MCPServer(
        "Runlet",
        version=APP_VERSION,
        lifespan=lifespan,
        subscriptions=False,
        instructions=(
            "Shared execution environment for trusted agents. Use separate workspaces for "
            "independent tasks. Git commits, pushes and branch changes are your responsibility. "
            "No automatic checkpoints. Store repo URL and recovery ref outside this server. "
            "After a restart, recreate missing workspaces with that repo/ref. "
            "Jobs and file tools can run concurrently within a workspace. Native writes/edits "
            "to the same resolved file queue in order. Coordinate shell commands that touch "
            "the same files, Git checkout or dependency environment yourself. "
            "Workspace deletion requires all active operations to finish. "
            "Use exec tty=true for a terminal, job_write_stdin for input, job_resize for size, "
            "and image_read to inspect screenshots as native image content. "
            "Use file_export for temporary download links to finished files; links grant access "
            "to one snapshot until expiry or server restart. "
            "Jobs continue across client disconnects, but not server restarts."
        ),
    )

    @mcp.custom_route("/", methods=["GET"])
    async def index(request: Request):
        return JSONResponse(
            {
                "name": "Runlet",
                "mcp_endpoint": "/mcp",
                "authentication": "X-Sandbox-Token: <SANDBOX_TOKEN>",
            }
        )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def health(request: Request):
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/downloads/{token}", methods=["GET", "HEAD"])
    async def download(request: Request):
        return exports.response(request.path_params["token"])

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def server_info() -> dict[str, Any]:
        """Inspect the server boot ID and limits. A new boot ID means previous jobs are gone."""
        return {
            "boot_id": boot_id,
            "server_version": APP_VERSION,
            "mcp_sdk_version": MCP_VERSION,
            "max_concurrent_jobs": settings.max_jobs,
            "default_timeout": settings.default_timeout,
            "max_timeout": settings.max_timeout,
            "output_buffer_bytes": settings.max_output_bytes,
            "response_bytes": settings.response_bytes,
            "max_file_bytes": settings.max_file_bytes,
            "exports": {
                "enabled": bool(settings.public_base_url),
                "ttl_seconds": settings.export_ttl_seconds,
                "max_file_bytes": settings.max_export_bytes,
                "max_exports": settings.max_exports,
            },
            "interactive": {
                "pty": True,
                "stdin": True,
                "resize": True,
                "max_stdin_bytes": MAX_STDIN_BYTES,
            },
            "images": {
                "formats": ["PNG", "JPEG", "WebP", "GIF"],
                "max_input_bytes": MAX_IMAGE_BYTES,
                "max_pixels": MAX_IMAGE_PIXELS,
                "max_output_bytes": MAX_IMAGE_OUTPUT_BYTES,
                "default_max_dimension": DEFAULT_IMAGE_DIMENSION,
                "max_dimension": MAX_IMAGE_DIMENSION,
                "max_concurrent_reads": MAX_CONCURRENT_IMAGES,
            },
            "concurrency": {
                "parallel_jobs_per_workspace": True,
                "file_mutations": "queued_per_resolved_path",
                "workspace_deletion": "idle_only",
            },
            "persistence": "Persist SANDBOX_ROOT or explicitly push Git state to recover files",
        }

    @mcp.tool()
    async def workspace_create(
        repo_url: str | None = None,
        ref: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a scratch workspace or clone GitHub HTTPS repo/ref. Reuse existing IDs unchanged.

        Omit all arguments for a new scratch workspace. A supplied ID that is missing requires
        repo_url to avoid silently replacing lost work with an empty workspace. Keep the ID,
        repo_url and desired recovery ref outside this server. Dependencies get a fresh Python venv.
        """
        return {**await workspaces.create(repo_url, ref, workspace_id), "boot_id": boot_id}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def workspace_list() -> dict[str, Any]:
        """List local workspaces, checkout state, busy flags and active job counts."""
        return {"boot_id": boot_id, "workspaces": await workspaces.list()}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def workspace_get(workspace_id: str) -> dict[str, Any]:
        """Inspect an existing workspace and its retained job IDs without changing its checkout."""
        return {
            **await workspaces.info(workspace_id),
            "boot_id": boot_id,
            "jobs": [
                {"job_id": job.job_id, "state": job.state}
                for job in jobs.items.values()
                if job.workspace_id == workspace_id
            ],
        }

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
    async def workspace_delete(workspace_id: str) -> dict[str, Any]:
        """Delete local files, including unpushed work. Reject active jobs or file operations."""
        result = await workspaces.delete(workspace_id)
        jobs.forget_workspace(workspace_id)
        return result

    @mcp.tool(name="exec", annotations=ToolAnnotations(destructive_hint=True))
    async def execute(
        workspace_id: str,
        command: str,
        cwd: str = ".",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        wait_seconds: float = 2,
        request_id: str | None = None,
        tty: bool = False,
        rows: int = 24,
        cols: int = 80,
    ) -> dict[str, Any]:
        """Run Bash in a workspace. Short commands return results; long commands return a job ID.

        Each command starts a fresh shell with workspace-local HOME, TMPDIR and Python venv.
        cwd is relative. stdout/stderr are merged, buffered and bounded. Jobs survive a client
        disconnect; poll job_status with next_cursor for new output. All descendants are stopped
        when the job ends. Reusing request_id deduplicates only while its record is retained in
        this server boot. Check server_info after connection loss; never blindly replay a job
        after restart. Git operations happen only when you explicitly run them.
        Multiple jobs and file tools may run in the same workspace at once. Coordinate commands
        that change the same files, Git checkout, or dependency environment yourself.
        stdin is open for every job. Use job_write_stdin to send input or close a pipe's stdin.
        tty=true allocates a controlling terminal; rows/cols set its initial size. Terminal output
        includes echo, ANSI sequences and CRLF. Job timeout still applies while waiting for input.
        """
        return await jobs.start(
            workspace_id,
            command,
            cwd,
            env,
            timeout,
            wait_seconds,
            request_id,
            tty,
            rows,
            cols,
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def job_status(job_id: str, cursor: int = 0, wait_seconds: float = 0) -> dict[str, Any]:
        """Read incremental job output using its byte cursor; optionally long-poll for up to 20s."""
        return await jobs.status(job_id, cursor, wait_seconds)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
    async def job_cancel(job_id: str) -> dict[str, Any]:
        """Stop a job and its descendants. File edits and external side effects remain."""
        return await jobs.cancel(job_id)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
    async def job_write_stdin(
        job_id: str,
        chars: str = "",
        close_stdin: bool = False,
        expected_input_offset: int | None = None,
        cursor: int = 0,
        wait_seconds: float = 0,
    ) -> dict[str, Any]:
        """Send up to 64 KiB of UTF-8 to a running pipe or terminal job, then read output.

        Sends chars exactly; include a newline to submit a terminal line. Control characters
        such as Ctrl-C (\\u0003) and Ctrl-D (\\u0004) are supported in terminals. close_stdin sends
        real EOF for pipe jobs; PTYs cannot half-close, so send Ctrl-D or cancel instead.
        Input calls queue per job. expected_input_offset rejects stale/replayed writes; input_offset
        counts bytes accepted for delivery, not consumption by the application. Inspect job_status
        after an interrupted call before retrying. cursor and wait_seconds follow job_status.
        """
        return await jobs.write_stdin(
            job_id, chars, close_stdin, expected_input_offset, cursor, wait_seconds
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False))
    async def job_resize(job_id: str, rows: int, cols: int) -> dict[str, Any]:
        """Resize a running PTY to 1–1000 rows/columns and notify its foreground process."""
        return await jobs.resize(job_id, rows, cols)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    async def image_read(
        workspace_id: str, path: str, max_dimension: int = DEFAULT_IMAGE_DIMENSION
    ) -> list[TextContent | ImageContent]:
        """Return a validated PNG/JPEG/WebP/GIF as native MCP image content plus metadata.

        Paths stay inside the workspace. Input limits are 10 MiB and 25 megapixels. Previews fit
        max_dimension (default 1600, maximum 4096), and may shrink further to stay under 2 MiB.
        EXIF orientation is applied and metadata stripped. Animated files show their first frame.
        """
        image = await files.image(workspace_id, path, max_dimension)
        return [
            TextContent(type="text", text=json.dumps(image.metadata)),
            ImageContent(
                type="image", data=base64.b64encode(image.data).decode(), mime_type=image.mime_type
            ),
        ]

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    async def file_export(workspace_id: str, path: str) -> dict[str, Any]:
        """Snapshot one workspace file and return metadata plus an expiring download URL.

        Requires PUBLIC_BASE_URL. Anyone holding the URL can download this single snapshot
        without X-Sandbox-Token until expiry or server restart. The snapshot survives edits
        and workspace deletion. Native file writes/edits queue with the snapshot copy; stop
        shell writers before exporting. Use shell tools to ZIP directories first.
        """
        return await exports.create(workspace_id, path)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def file_read(
        workspace_id: str,
        path: str,
        offset: int = 0,
        max_bytes: int = 65_536,
        encoding: Literal["utf-8", "base64"] = "utf-8",
    ) -> dict[str, Any]:
        """Read a bounded relative file chunk. Use base64 for exact binary or UTF-8 chunks."""
        return await files.read(workspace_id, path, offset, max_bytes, encoding)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
    async def file_write(
        workspace_id: str,
        path: str,
        content: str,
        encoding: Literal["utf-8", "base64"] = "utf-8",
    ) -> dict[str, Any]:
        """Atomically create/replace a file. Writes to the same resolved path queue.

        Different files run in parallel, alongside shell jobs. Shell commands do not participate
        in native file locks.
        """
        return await files.write(workspace_id, path, content, encoding)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True))
    async def file_edit(
        workspace_id: str,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        """Edit an existing UTF-8 file by exact text replacement, without rewriting it in the call.

        old_text must match exactly one location unless replace_all=true. Include surrounding
        context to distinguish repeated text. Missing or ambiguous matches leave the file unchanged.
        Native edits/writes to the same resolved path queue; different files run in parallel,
        including while shell jobs run. Shell commands do not participate in native file locks.
        Writes are atomic and preserve permissions/newlines. Both the current file and edited
        result must fit MAX_FILE_BYTES. No shell or Git command is run.
        """
        return await files.edit(workspace_id, path, old_text, new_text, replace_all)

    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=4 * 1024 * 1024,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=list(settings.allowed_origins),
        ),
    )
    app.state.services = Services(settings, boot_id, workspaces, jobs, files, exports)
    app.add_middleware(SandboxTokenMiddleware, token=settings.token)
    return app


def main() -> None:
    # Access-log request paths would disclose temporary download capabilities.
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
