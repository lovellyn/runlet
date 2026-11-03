"""Single-file upload capabilities with streamed, verified, atomic publication."""

import asyncio
import hashlib
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse

from .errors import SandboxError
from .files import _finish_io
from .workspaces import Workspace, Workspaces

UPLOAD_PATH = re.compile(r"/uploads/[A-Za-z0-9_-]{43}\Z")
UPLOAD_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=UPLOAD_HEADERS)


def _publish(work: Path, relative: str, temporary: Path, overwrite: bool) -> None:
    """Walk parents without following symlinks; install only a complete regular file."""
    parts = Path(relative).parts
    descriptor = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        try:
            previous = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            mode = 0o644
        else:
            if not stat.S_ISREG(previous.st_mode):
                raise SandboxError("invalid_file", "Upload destination must be a regular file")
            if not overwrite:
                raise SandboxError("file_exists", "Set overwrite=true to replace an existing file")
            mode = stat.S_IMODE(previous.st_mode) & 0o777
        temporary.chmod(mode)
        if overwrite:
            os.replace(temporary, parts[-1], dst_dir_fd=descriptor)
        else:
            # link() is atomic and refuses an entry created since the existence check.
            try:
                os.link(temporary, parts[-1], dst_dir_fd=descriptor, follow_symlinks=False)
            except FileExistsError as error:
                raise SandboxError(
                    "file_exists", "Destination was created during upload"
                ) from error
    finally:
        os.close(descriptor)


@dataclass
class Upload:
    workspace: Workspace
    path: str
    destination: str
    size_bytes: int
    sha256: str
    overwrite: bool
    deadline: float


class Uploads:
    def __init__(self, workspaces: Workspaces):
        self.workspaces = workspaces
        self.settings = workspaces.settings
        self.directory = self.settings.root.resolve() / "uploads"
        self.items: dict[str, Upload] = {}
        self._active: set[asyncio.Task] = set()
        self._closing = False

    async def start(self) -> None:
        def reset() -> None:
            if self.directory.is_symlink():
                self.directory.unlink()
            elif self.directory.exists():
                shutil.rmtree(self.directory)
            self.directory.mkdir(mode=0o700)

        await _finish_io(reset)

    def _purge(self) -> None:
        for token, item in list(self.items.items()):
            if (
                item.deadline <= time.monotonic()
                or self.workspaces.items.get(item.workspace.workspace_id) is not item.workspace
            ):
                del self.items[token]

    def forget_workspace(self, workspace_id: str) -> None:
        for token, item in list(self.items.items()):
            if item.workspace.workspace_id == workspace_id:
                del self.items[token]

    def create(
        self,
        workspace_id: str,
        path: str,
        size_bytes: int,
        sha256: str,
        overwrite: bool = False,
    ) -> dict:
        if not self.settings.public_base_url:
            raise SandboxError("uploads_disabled", "Set PUBLIC_BASE_URL to enable file uploads")
        if self._closing:
            raise SandboxError("server_stopping", "Server is stopping")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise SandboxError("invalid_size", "size_bytes must be a nonnegative integer")
        if size_bytes > self.settings.max_upload_bytes:
            raise SandboxError("file_too_large", "File exceeds MAX_UPLOAD_BYTES")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise SandboxError("invalid_checksum", "sha256 must be a 64-character hex digest")
        workspace = self.workspaces.get(workspace_id)
        try:
            target = workspace.resolve_path(path)
        except (ValueError, OSError) as error:
            raise SandboxError("invalid_path", "Use a workspace-relative file path") from error
        destination = target.relative_to(workspace.work.resolve())
        if not destination.parts or target.is_dir():
            raise SandboxError("invalid_file", "Upload a single regular file, not a directory")
        if target.exists():
            if not target.is_file():
                raise SandboxError("invalid_file", "Upload destination must be a regular file")
            if not overwrite:
                raise SandboxError("file_exists", "Set overwrite=true to replace an existing file")
        self._purge()
        if len(self.items) + len(self._active) >= self.settings.max_uploads:
            raise SandboxError("upload_limit", "MAX_UPLOADS reached; wait for tickets to expire")
        token = secrets.token_urlsafe(32)
        ttl = self.settings.upload_ttl_seconds
        self.items[token] = Upload(
            workspace,
            path,
            str(destination),
            size_bytes,
            sha256.lower(),
            overwrite,
            time.monotonic() + ttl,
        )
        return {
            "path": path,
            "size_bytes": size_bytes,
            "sha256": sha256.lower(),
            "upload_url": f"{self.settings.public_base_url.rstrip('/')}/uploads/{token}",
            "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat(),
        }

    async def receive(self, token: str, request: Request) -> JSONResponse:
        self._purge()
        if self._closing or (item := self.items.pop(token, None)) is None:
            return _error("upload_not_found", 404)
        task = asyncio.current_task()
        self._active.add(task)
        try:
            with item.workspace.operation():
                async with asyncio.timeout(self.settings.upload_timeout):
                    return await self._receive(item, request)
        except TimeoutError:
            return _error("upload_timeout", 408)
        except ClientDisconnect:
            return _error("upload_interrupted", 400)
        except SandboxError as error:
            return _error(error.code, 409 if error.code == "file_exists" else 400)
        except OSError:
            return _error("upload_failed", 500)
        finally:
            self._active.discard(task)

    async def _receive(self, item: Upload, request: Request) -> JSONResponse:
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return _error("unsupported_encoding", 415)
        if length := request.headers.get("content-length"):
            try:
                declared = int(length)
            except ValueError:
                return _error("invalid_size", 400)
            if declared != item.size_bytes:
                return _error("size_mismatch", 400)

        def open_temporary():
            descriptor, name = tempfile.mkstemp(dir=self.directory)
            return os.fdopen(descriptor, "wb"), Path(name)

        stream, temporary = open_temporary()
        try:
            digest, size = hashlib.sha256(), 0
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > item.size_bytes:
                    return _error("file_too_large", 413)

                def write_chunk(data: bytes = chunk) -> None:
                    stream.write(data)
                    digest.update(data)

                await _finish_io(write_chunk)
            await _finish_io(stream.close)
            if size != item.size_bytes:
                return _error("size_mismatch", 400)
            checksum = digest.hexdigest()
            if checksum != item.sha256:
                return _error("checksum_mismatch", 400)
            # Network transfer does not hold the file lock. Only publication joins the queue.
            async with item.workspace.file_mutation(item.destination):
                await _finish_io(
                    lambda: _publish(
                        item.workspace.work, item.destination, temporary, item.overwrite
                    )
                )
            return JSONResponse(
                {"path": item.path, "size_bytes": size, "sha256": checksum},
                status_code=201,
                headers=UPLOAD_HEADERS,
            )
        finally:

            def discard() -> None:
                try:
                    stream.close()
                finally:
                    temporary.unlink(missing_ok=True)

            await _finish_io(discard)

    async def close(self) -> None:
        self._closing = True
        self.items.clear()
        active = list(self._active)
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        await _finish_io(lambda: shutil.rmtree(self.directory, ignore_errors=True))
