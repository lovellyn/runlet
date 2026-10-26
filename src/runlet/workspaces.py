"""Local workspaces; Git is only mutated by an explicit create/clone request."""

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings
from .errors import SandboxError
from .git_auth import git_environment

WORKSPACE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
REPO_PATH = re.compile(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def now() -> str:
    return datetime.now(UTC).isoformat()


def validate_id(value: str) -> str:
    if not WORKSPACE_ID.fullmatch(value):
        raise SandboxError(
            "invalid_workspace_id", "Use 1–64 letters, digits, underscores or hyphens"
        )
    return value


def normalize_repo_url(value: str) -> str:
    parsed = urlsplit(value)
    path = parsed.path.removesuffix("/").removesuffix(".git")
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
        or not REPO_PATH.fullmatch(path)
        or any(part in {".", ".."} for part in path.split("/")[1:])
    ):
        raise SandboxError("invalid_repository", "Use an HTTPS github.com owner/repository URL")
    return f"https://github.com{path.lower()}.git"


@dataclass
class _LockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


@asynccontextmanager
async def _key_lock[K](pool: dict[K, _LockEntry], key: K):
    """Queue matching keys without retaining idle locks or serializing other keys."""
    if key not in pool:
        pool[key] = _LockEntry()
    entry = pool[key]
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if not entry.users:
            del pool[key]


async def finish_task[T](task: asyncio.Task[T]) -> T:
    """Finish filesystem work even if the caller receives repeated cancellations."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                if not task.cancelled():
                    task.exception()
                raise
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        else:
            if cancelled:
                raise asyncio.CancelledError
            return result


@dataclass
class Workspace:
    workspace_id: str
    directory: Path
    repo_url: str | None
    created_at: str
    active_operations: int = 0
    active_jobs: int = 0
    file_locks: dict[Path, _LockEntry] = field(default_factory=dict)
    deleting: bool = False

    def reserve(self) -> None:
        """Reserve lifetime before yielding; deletion requires all operations to finish."""
        if self.deleting:
            raise SandboxError("workspace_not_found", "Workspace is being deleted")
        self.active_operations += 1

    def release(self) -> None:
        self.active_operations -= 1

    @contextmanager
    def operation(self):
        self.reserve()
        try:
            yield
        finally:
            self.release()

    @asynccontextmanager
    async def file_mutation(self, path: str):
        with self.operation():
            target = self.resolve_path(path)
            async with _key_lock(self.file_locks, target):
                yield target

    @property
    def work(self) -> Path:
        return self.directory / "work"

    @property
    def runtime(self) -> Path:
        return self.directory / "runtime"

    def resolve_path(self, relative: str, *, directory: bool = False) -> Path:
        requested = Path(relative)
        if requested.is_absolute() or ".." in requested.parts:
            raise SandboxError("invalid_path", "Paths must be relative to the workspace")
        resolved = (self.work / requested).resolve()
        if not resolved.is_relative_to(self.work.resolve()):
            raise SandboxError("invalid_path", "Path resolves outside the workspace")
        if directory and not resolved.is_dir():
            raise SandboxError("invalid_directory", f"Directory does not exist: {relative}")
        return resolved

    def environment(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        env = git_environment()
        env.update(
            {
                "HOME": str(self.runtime / "home"),
                "TMPDIR": str(self.runtime / "tmp"),
                "VIRTUAL_ENV": str(self.runtime / "venv"),
                "PATH": f"{self.runtime / 'venv' / 'bin'}:{env['PATH']}",
                "PYTHONUNBUFFERED": "1",
                "TERM": "dumb",
            }
        )
        for key, value in (overrides or {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\0" in value:
                raise SandboxError("invalid_environment", "Invalid environment name or value")
            env[key] = value
        return env


async def run_setup(args: list[str], timeout: float, env: dict[str, str]) -> str:
    """Run bounded setup commands, killing their process group on timeout/cancellation."""
    process = await asyncio.create_subprocess_exec(
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    output = bytearray()

    async def drain() -> None:
        assert process.stdout is not None
        while data := await process.stdout.read(8192):
            output.extend(data)
            del output[:-8192]

    reader = asyncio.create_task(drain())
    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(process.wait(), reader)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.gather(process.wait(), reader, return_exceptions=True)
        raise
    if process.returncode:
        raise SandboxError(
            "setup_failed", output.decode("utf-8", errors="replace").strip() or "Setup failed"
        )
    return output.decode("utf-8", errors="replace").strip()


class Workspaces:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.root.resolve() / "workspaces"
        self.root.mkdir(parents=True, exist_ok=True)
        self.items: dict[str, Workspace] = {}
        self._creation_locks: dict[str, _LockEntry] = {}
        self._creating: set[str] = set()
        self._load_existing()

    def _load_existing(self) -> None:
        for path in self.root.iterdir():
            if path.is_symlink() or not WORKSPACE_ID.fullmatch(path.name):
                continue
            metadata = path / "workspace.json"
            if not metadata.is_file():
                continue
            try:
                record = json.loads(metadata.read_text())
                if record["workspace_id"] != path.name or not (path / "work").is_dir():
                    continue
                self.items[path.name] = Workspace(
                    path.name,
                    path,
                    record["repo_url"],
                    record["created_at"],
                )
            except (OSError, KeyError, ValueError, TypeError):
                continue

    def get(self, workspace_id: str) -> Workspace:
        validate_id(workspace_id)
        workspace = self.items.get(workspace_id)
        if workspace is None or workspace.deleting:
            raise SandboxError(
                "workspace_not_found",
                "Provide repo_url and ref to recreate a missing workspace after a restart",
            )
        return workspace

    async def create(
        self,
        repo_url: str | None = None,
        ref: str | None = None,
        workspace_id: str | None = None,
    ) -> dict:
        explicit_id = workspace_id is not None
        workspace_id = validate_id(workspace_id if explicit_id else f"ws-{uuid.uuid4().hex[:16]}")
        normalized = normalize_repo_url(repo_url) if repo_url is not None else None
        if ref is not None and (
            not ref or ref.startswith("-") or len(ref) > 255 or any(ord(char) < 32 for char in ref)
        ):
            raise SandboxError("invalid_ref", "Provide a branch, tag, or commit SHA")
        async with _key_lock(self._creation_locks, workspace_id):
            if workspace_id in self.items:
                workspace = self.get(workspace_id)
                with workspace.operation():
                    if normalized is not None and workspace.repo_url != normalized:
                        raise SandboxError(
                            "workspace_conflict", "This ID belongs to another repository"
                        )
                    info = await self._info(workspace)
                    if ref is not None and ref not in {info["branch"], info["head_commit"]}:
                        if not workspace.repo_url:
                            raise SandboxError(
                                "workspace_conflict", "Existing workspace is not a clone"
                            )
                        try:
                            target = await self._git(
                                workspace, "rev-parse", "--verify", f"{ref}^{{commit}}"
                            )
                        except SandboxError:
                            target = None
                        if target != info["head_commit"]:
                            raise SandboxError(
                                "workspace_conflict",
                                "Existing checkout differs; inspect it and change refs with exec",
                            )
                    return {**info, "created": False}
            if explicit_id and repo_url is None:
                raise SandboxError(
                    "workspace_not_found",
                    "ID alone cannot restore files; supply repo_url and optional ref",
                )
            if ref is not None and normalized is None:
                raise SandboxError("invalid_repository", "ref requires repo_url")
            if len(self.items.keys() | self._creating) >= self.settings.max_workspaces:
                raise SandboxError("workspace_limit", "Delete an unused workspace first")
            self._creating.add(workspace_id)
            try:
                return await self._create(workspace_id, normalized, ref)
            finally:
                self._creating.discard(workspace_id)

    async def _create(self, workspace_id: str, normalized: str | None, ref: str | None) -> dict:
        temporary = self.root / f".create-{workspace_id}-{uuid.uuid4().hex}"
        destination = self.root / workspace_id
        if destination.exists():
            raise SandboxError("workspace_conflict", "Incomplete workspace directory exists")
        temporary.mkdir()
        workspace = Workspace(workspace_id, temporary, normalized, now())
        try:
            for name in ("home", "tmp"):
                (workspace.runtime / name).mkdir(parents=True)
            env = git_environment()
            env["HOME"] = str(workspace.runtime / "home")
            for key, value in (
                ("user.name", self.settings.git_user_name),
                ("user.email", self.settings.git_user_email),
            ):
                if value:
                    await run_setup(
                        ["git", "config", "--global", "--", key, value],
                        self.settings.setup_timeout,
                        env,
                    )
            if normalized:
                await run_setup(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "clone",
                        "--no-recurse-submodules",
                        "--",
                        normalized,
                        str(workspace.work),
                    ],
                    self.settings.setup_timeout,
                    env,
                )
                if ref is not None:
                    await run_setup(
                        [
                            "git",
                            "-C",
                            str(workspace.work),
                            "-c",
                            "core.hooksPath=/dev/null",
                            "checkout",
                            "--quiet",
                            ref,
                            "--",  # A ref must not fall back to a file-path checkout.
                        ],
                        self.settings.setup_timeout,
                        env,
                    )
            else:
                workspace.work.mkdir()
            temporary.rename(destination)
            workspace.directory = destination
            try:
                # Venv script shebangs contain absolute paths: create at the final location.
                await run_setup(
                    [sys.executable, "-m", "venv", str(workspace.runtime / "venv")],
                    self.settings.setup_timeout,
                    git_environment(),
                )
                (destination / "workspace.json").write_text(
                    json.dumps(
                        {
                            "workspace_id": workspace_id,
                            "repo_url": normalized,
                            "created_at": workspace.created_at,
                        }
                    )
                )
            except BaseException:
                await finish_task(
                    asyncio.create_task(
                        asyncio.to_thread(shutil.rmtree, destination, ignore_errors=True)
                    )
                )
                raise
            self.items[workspace_id] = workspace
        except BaseException:
            await finish_task(
                asyncio.create_task(asyncio.to_thread(shutil.rmtree, temporary, ignore_errors=True))
            )
            raise
        return {**await self.info(workspace_id), "created": True}

    async def _git(self, workspace: Workspace, *args: str) -> str:
        return await run_setup(
            ["git", "-C", str(workspace.work), *args],
            min(self.settings.setup_timeout, 10),
            workspace.environment(),
        )

    async def info(self, workspace_id: str) -> dict:
        workspace = self.get(workspace_id)
        with workspace.operation():
            return await self._info(workspace)

    async def _info(self, workspace: Workspace) -> dict:
        """Inspect while the caller holds one lifetime reservation."""
        branch = head = dirty = None
        if workspace.repo_url:
            try:
                branch = await self._git(workspace, "symbolic-ref", "--quiet", "--short", "HEAD")
            except SandboxError:
                pass  # Detached HEAD is valid.
            try:
                head = await self._git(workspace, "rev-parse", "--verify", "HEAD")
                dirty = bool(await self._git(workspace, "status", "--porcelain"))
            except SandboxError:
                pass  # Empty remote repositories have no HEAD yet.
        return {
            "workspace_id": workspace.workspace_id,
            "path": str(workspace.work),
            "repo_url": workspace.repo_url,
            "branch": branch,
            "head_commit": head,
            "dirty": dirty,
            "busy": workspace.active_operations > 1,
            "active_jobs": workspace.active_jobs,
            "created_at": workspace.created_at,
        }

    async def list(self) -> list[dict]:
        results = await asyncio.gather(
            *(self.info(key) for key in sorted(self.items)), return_exceptions=True
        )
        workspaces = []
        for result in results:
            if isinstance(result, SandboxError) and result.code == "workspace_not_found":
                continue  # A concurrent deletion may finish after the list was requested.
            if isinstance(result, BaseException):
                raise result
            workspaces.append(result)
        return workspaces

    async def delete(self, workspace_id: str) -> dict:
        workspace = self.get(workspace_id)
        if workspace.active_operations:
            raise SandboxError("workspace_busy", "Finish active operations before deletion")
        workspace.deleting = True

        async def remove():
            try:
                await asyncio.to_thread(shutil.rmtree, workspace.directory)
            except BaseException:
                workspace.deleting = False
                raise
            del self.items[workspace_id]

        removal = asyncio.create_task(remove())
        await finish_task(removal)
        return {"workspace_id": workspace_id, "deleted": True}
