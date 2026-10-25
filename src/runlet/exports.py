"""Bounded file snapshots with temporary, single-file download capabilities."""

import asyncio
import hashlib
import mimetypes
import os
import re
import secrets
import shutil
import stat
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from starlette.responses import FileResponse, JSONResponse, Response
from starlette.types import Receive, Scope, Send

from .errors import SandboxError
from .files import _finish_io
from .workspaces import Workspaces, finish_task

DOWNLOAD_PATH = re.compile(r"/downloads/[A-Za-z0-9_-]{43}\Z")
DOWNLOAD_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _open_source(work: Path, target: Path):
    """Open a resolved workspace file without following swapped parent/leaf symlinks."""
    parts = target.relative_to(work.resolve()).parts
    descriptor = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if not parts:
            raise SandboxError("invalid_file", "Export a regular file, not a directory")
        source = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=descriptor)
        return os.fdopen(source, "rb")
    finally:
        os.close(descriptor)


def _snapshot(work: Path, source: Path, destination: Path, limit: int) -> tuple[int, str]:
    try:
        with _open_source(work, source) as reader:
            before = os.fstat(reader.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SandboxError("invalid_file", "Only regular files can be exported")
            if before.st_size > limit:
                raise SandboxError("file_too_large", "File exceeds MAX_EXPORT_BYTES")
            digest, size = hashlib.sha256(), 0
            with destination.open("xb") as writer:
                os.chmod(destination, 0o600)
                while chunk := reader.read(65_536):
                    size += len(chunk)
                    if size > limit:
                        raise SandboxError("file_too_large", "File exceeds MAX_EXPORT_BYTES")
                    writer.write(chunk)
                    digest.update(chunk)
            after = os.fstat(reader.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise SandboxError(
                    "file_changed", "File changed during export; stop writers and retry"
                )
        return size, digest.hexdigest()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


@dataclass
class Export:
    export_id: str
    path: Path
    filename: str
    mime_type: str
    size: int
    sha256: str
    deadline: float
    expires_at: str
    readers: int = 0


def _not_found() -> JSONResponse:
    return JSONResponse({"error": "export_not_found"}, status_code=404, headers=DOWNLOAD_HEADERS)


class _DownloadResponse(FileResponse):
    def __init__(self, owner: "Exports", token: str, export: Export):
        super().__init__(
            export.path,
            media_type=export.mime_type,
            filename=export.filename,
            headers={**DOWNLOAD_HEADERS, "ETag": f'"{export.sha256}"'},
        )
        self.owner, self.token, self.export = owner, token, export

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        export = self.export
        if self.owner.items.get(self.token) is not export or export.deadline <= time.monotonic():
            await _not_found()(scope, receive, send)
            return
        export.readers += 1
        try:
            await super().__call__(scope, receive, send)
        finally:
            export.readers -= 1


class Exports:
    def __init__(self, workspaces: Workspaces):
        self.workspaces = workspaces
        self.settings = workspaces.settings
        self.directory = self.settings.root.resolve() / "exports"
        self.items: dict[str, Export] = {}
        self._reserved = 0
        self._copies: set[asyncio.Task] = set()
        self._cleanup_lock = asyncio.Lock()
        self._collector: asyncio.Task | None = None
        self._closing = False

    async def start(self) -> None:
        def reset() -> None:
            if self.directory.is_symlink():
                self.directory.unlink()
            elif self.directory.exists():
                shutil.rmtree(self.directory)
            self.directory.mkdir(mode=0o700)

        await _finish_io(reset)
        self._collector = asyncio.create_task(self._collect())

    async def _collect(self) -> None:
        while True:
            await asyncio.sleep(min(30, self.settings.export_ttl_seconds))
            await self.cleanup()

    async def cleanup(self) -> None:
        async with self._cleanup_lock:
            expired = [
                token
                for token, export in self.items.items()
                if export.deadline <= time.monotonic() and not export.readers
            ]
            paths = [self.items.pop(token).path for token in expired]
            if paths:
                await _finish_io(lambda: [path.unlink(missing_ok=True) for path in paths])

    async def create(self, workspace_id: str, path: str) -> dict:
        if not self.settings.public_base_url:
            raise SandboxError("exports_disabled", "Set PUBLIC_BASE_URL to enable file exports")
        if self._closing:
            raise SandboxError("server_stopping", "Server is stopping")
        await self.cleanup()
        if len(self.items) + self._reserved >= self.settings.max_exports:
            raise SandboxError(
                "export_limit", "MAX_EXPORTS reached; wait for existing links to expire"
            )
        self._reserved += 1
        export_id = uuid.uuid4().hex
        destination = self.directory / export_id
        published = False
        try:
            workspace = self.workspaces.get(workspace_id)
            # Join the native file queue and retain workspace lifetime throughout the copy.
            async with workspace.file_mutation(path) as source:
                copying = asyncio.create_task(
                    asyncio.to_thread(
                        _snapshot,
                        workspace.work,
                        source,
                        destination,
                        self.settings.max_export_bytes,
                    )
                )
                self._copies.add(copying)
                try:
                    size, digest = await finish_task(copying)
                except OSError as error:
                    raise SandboxError(
                        "export_failed", "Cannot snapshot the requested file"
                    ) from error
                finally:
                    self._copies.discard(copying)
            if self._closing:
                raise SandboxError("server_stopping", "Server is stopping")
            token = secrets.token_urlsafe(32)
            filename = Path(path).name
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            ttl = self.settings.export_ttl_seconds
            expires_at = (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()
            self.items[token] = Export(
                export_id,
                destination,
                filename,
                mime_type,
                size,
                digest,
                time.monotonic() + ttl,
                expires_at,
            )
            published = True
            return {
                "export_id": export_id,
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": size,
                "sha256": digest,
                "expires_at": expires_at,
                "download_url": f"{self.settings.public_base_url.rstrip('/')}/downloads/{token}",
            }
        finally:
            self._reserved -= 1
            if not published:
                await _finish_io(lambda: destination.unlink(missing_ok=True))

    def response(self, token: str) -> Response:
        export = self.items.get(token)
        if export is None or export.deadline <= time.monotonic():
            return _not_found()
        return _DownloadResponse(self, token, export)

    async def close(self) -> None:
        self._closing = True
        if self._collector:
            self._collector.cancel()
            with suppress(asyncio.CancelledError):
                await self._collector
        if self._copies:
            await asyncio.gather(*self._copies, return_exceptions=True)
        self.items.clear()
        await _finish_io(lambda: shutil.rmtree(self.directory, ignore_errors=True))
