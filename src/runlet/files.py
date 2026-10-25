"""Bounded text/binary file operations relative to a workspace."""

import asyncio
import base64
import binascii
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from .errors import SandboxError
from .images import MAX_CONCURRENT_IMAGES, ImagePreview, preview
from .workspaces import Workspaces, finish_task


def _atomic_replace(target: Path, data: bytes) -> None:
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".sandbox-write-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def _finish_io[T](operation: Callable[[], T]) -> T:
    """Hold lifetime reservations and path locks until the filesystem work finishes."""
    writing = asyncio.create_task(asyncio.to_thread(operation))
    return await finish_task(writing)


class Files:
    def __init__(self, workspaces: Workspaces):
        self.workspaces = workspaces
        self._image_slots = asyncio.Semaphore(MAX_CONCURRENT_IMAGES)

    async def image(self, workspace_id: str, path: str, max_dimension: int = 1600) -> ImagePreview:
        workspace = self.workspaces.get(workspace_id)
        with workspace.operation():
            target = workspace.resolve_path(path)
            if not target.is_file():
                raise SandboxError("file_not_found", path)
            async with self._image_slots:
                return await _finish_io(lambda: preview(target, path, max_dimension))

    async def read(
        self,
        workspace_id: str,
        path: str,
        offset: int = 0,
        max_bytes: int = 65_536,
        encoding: Literal["utf-8", "base64"] = "utf-8",
    ) -> dict:
        if offset < 0 or not 0 < max_bytes <= self.workspaces.settings.max_file_bytes:
            raise SandboxError("invalid_range", "Use a nonnegative offset and a bounded max_bytes")
        if encoding not in {"utf-8", "base64"}:
            raise SandboxError("invalid_encoding", "Use utf-8 or base64")
        workspace = self.workspaces.get(workspace_id)
        with workspace.operation():
            target = workspace.resolve_path(path)
            if not target.is_file():
                raise SandboxError("file_not_found", path)

            def read_chunk() -> dict:
                with target.open("rb") as stream:
                    stream.seek(offset)
                    data = stream.read(max_bytes)
                    size = os.fstat(stream.fileno()).st_size
                return {
                    "path": path,
                    "encoding": encoding,
                    "offset": offset,
                    "next_offset": offset + len(data),
                    "eof": offset + len(data) >= size,
                    "content": base64.b64encode(data).decode()
                    if encoding == "base64"
                    else data.decode("utf-8", errors="replace"),
                }

            return await _finish_io(read_chunk)

    async def write(
        self,
        workspace_id: str,
        path: str,
        content: str,
        encoding: Literal["utf-8", "base64"] = "utf-8",
    ) -> dict:
        if encoding not in {"utf-8", "base64"}:
            raise SandboxError("invalid_encoding", "Use utf-8 or base64")
        if len(content) > self.workspaces.settings.max_file_bytes * 2:
            raise SandboxError("file_too_large", "Use shell tools for larger files")
        try:
            data = (
                base64.b64decode(content, validate=True)
                if encoding == "base64"
                else content.encode()
            )
        except (ValueError, binascii.Error) as error:
            raise SandboxError("invalid_content", "Invalid base64 data") from error
        if len(data) > self.workspaces.settings.max_file_bytes:
            raise SandboxError("file_too_large", "Use shell tools for larger files")
        workspace = self.workspaces.get(workspace_id)
        async with workspace.file_mutation(path) as target:

            def write_atomic() -> dict:
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_replace(target, data)
                return {"path": path, "bytes_written": len(data)}

            return await _finish_io(write_atomic)

    async def edit(
        self,
        workspace_id: str,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> dict:
        if not old_text or old_text == new_text:
            raise SandboxError("invalid_edit", "old_text must be nonempty and differ from new_text")
        limit = self.workspaces.settings.max_file_bytes
        if max(len(old_text), len(new_text)) > limit:
            raise SandboxError("file_too_large", "Edit text exceeds MAX_FILE_BYTES")
        try:
            old_size, new_size = len(old_text.encode("utf-8")), len(new_text.encode("utf-8"))
            if max(old_size, new_size) > limit:
                raise SandboxError("file_too_large", "Edit text exceeds MAX_FILE_BYTES")
        except UnicodeEncodeError as error:
            raise SandboxError("invalid_encoding", "Edit text must be valid UTF-8") from error
        workspace = self.workspaces.get(workspace_id)
        async with workspace.file_mutation(path) as target:
            if not target.is_file():
                raise SandboxError("file_not_found", path)

            def edit_atomic() -> dict:
                with target.open("rb") as stream:
                    data = stream.read(limit + 1)
                if len(data) > limit:
                    raise SandboxError("file_too_large", "File exceeds MAX_FILE_BYTES")
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise SandboxError(
                        "invalid_encoding", "file_edit requires a UTF-8 file"
                    ) from error
                first = text.find(old_text)
                if first < 0:
                    raise SandboxError("edit_not_found", "old_text does not match the current file")
                # Include overlapping matches when checking whether an edit is unambiguous.
                if not replace_all and text.find(old_text, first + 1) >= 0:
                    raise SandboxError(
                        "edit_ambiguous",
                        "old_text matches multiple locations; add context or set replace_all=true",
                    )
                replacements = text.count(old_text) if replace_all else 1
                result_size = len(data) + replacements * (new_size - old_size)
                if result_size > limit:
                    raise SandboxError("file_too_large", "Edited file would exceed MAX_FILE_BYTES")
                result = text.replace(old_text, new_text, -1 if replace_all else 1).encode("utf-8")
                _atomic_replace(target, result)
                return {
                    "path": path,
                    "replacements": replacements,
                    "bytes_written": len(result),
                }

            return await _finish_io(edit_atomic)
