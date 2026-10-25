"""Asynchronous shell jobs with pipe input, POSIX terminals and bounded output."""

import asyncio
import os
import signal
import sys
import uuid
from dataclasses import dataclass, field
from typing import Literal

from . import runner, terminal
from .config import Settings
from .errors import SandboxError
from .workspaces import Workspaces, finish_task, now

MAX_STDIN_BYTES = 65_536
STDIN_TIMEOUT = 5
JobState = Literal["running", "completed", "failed", "cancelled", "timed_out"]


@dataclass
class Job:
    job_id: str
    workspace_id: str
    command: str
    cwd: str
    env: dict[str, str]
    timeout: float
    request_id: str | None
    tty: bool = False
    initial_size: tuple[int, int] = (24, 80)
    terminal_size: tuple[int, int] = (24, 80)
    input_offset: int = 0
    stdin_closed: bool = False
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    master_fd: int | None = None
    started_at: str = field(default_factory=now)
    finished_at: str | None = None
    state: JobState = "running"
    exit_code: int | None = None
    error: str | None = None
    output: bytearray = field(default_factory=bytearray)
    output_start: int = 0
    cancel_requested: bool = False
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)


async def stop_job(process: asyncio.subprocess.Process) -> None:
    """Let the supervisor clean up its children, with a bounded hard-stop fallback."""
    if process.returncode is not None:
        return
    try:
        process.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), 3)
    except TimeoutError:
        await asyncio.to_thread(runner.signal_children, process.pid, signal.SIGKILL)
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await asyncio.wait_for(asyncio.shield(process.wait()), 1)


class Jobs:
    def __init__(self, settings: Settings, workspaces: Workspaces, boot_id: str):
        self.settings = settings
        self.workspaces = workspaces
        self.boot_id = boot_id
        self.items: dict[str, Job] = {}
        self._requests: dict[tuple[str, str], str] = {}
        self._active = 0
        self._closing = False

    def get(self, job_id: str) -> Job:
        job = self.items.get(job_id)
        if job is None:
            raise SandboxError(
                "job_not_found",
                f"Job history is local to server boot {self.boot_id}; it may have restarted "
                "or the completed record may have expired. Do not blindly replay commands.",
            )
        return job

    @staticmethod
    def _validate_size(rows: int, cols: int) -> None:
        if not 1 <= rows <= 1000 or not 1 <= cols <= 1000:
            raise SandboxError("invalid_terminal_size", "rows and cols must be between 1 and 1000")

    async def start(
        self,
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
    ) -> dict:
        if self._closing:
            raise SandboxError("server_stopping", "Reconnect after the server restarts")
        if not command.strip() or "\0" in command or len(command) > 65_536:
            raise SandboxError("invalid_command", "Provide a nonempty command up to 64 KiB")
        timeout = self.settings.default_timeout if timeout is None else timeout
        if not 0 < timeout <= self.settings.max_timeout:
            raise SandboxError("invalid_timeout", f"Use 0 < timeout <= {self.settings.max_timeout}")
        if not 0 <= wait_seconds <= 20:
            raise SandboxError("invalid_wait", "Use 0 <= wait_seconds <= 20")
        if request_id is not None and not 1 <= len(request_id) <= 128:
            raise SandboxError("invalid_request_id", "Use 1–128 characters")
        self._validate_size(rows, cols)
        workspace = self.workspaces.get(workspace_id)
        directory = workspace.resolve_path(cwd, directory=True)
        child_env = workspace.environment(env)
        if tty and "TERM" not in (env or {}):
            child_env["TERM"] = "xterm-256color"
        key = (workspace_id, request_id) if request_id is not None else None
        if key is not None and key in self._requests:
            job = self.get(self._requests[key])
            if (job.command, job.cwd, job.env, job.timeout, job.tty, job.initial_size) != (
                command,
                str(directory),
                env or {},
                timeout,
                tty,
                (rows, cols),
            ):
                raise SandboxError(
                    "request_conflict", "request_id was used for a different command"
                )
        else:
            if self._active >= self.settings.max_jobs:
                raise SandboxError(
                    "server_busy", "Global job limit reached; retry after a job finishes"
                )
            self._evict_completed()
            if len(self.items) >= self.settings.max_job_records:
                raise SandboxError("server_busy", "Job record capacity reached")
            workspace.reserve()
            workspace.active_jobs += 1
            job = Job(
                f"{self.boot_id}-{uuid.uuid4().hex[:12]}",
                workspace_id,
                command,
                str(directory),
                dict(env or {}),
                timeout,
                request_id,
                tty=tty,
                initial_size=(rows, cols),
                terminal_size=(rows, cols),
            )
            self.items[job.job_id] = job
            if key is not None:
                self._requests[key] = job.job_id
            self._active += 1
            job.task = asyncio.create_task(self._run(job, child_env))
        if wait_seconds and job.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(job.task), wait_seconds)
            except TimeoutError:
                pass
        return self.snapshot(job, 0)

    def _evict_completed(self) -> None:
        while len(self.items) >= self.settings.max_job_records:
            candidate = next((job for job in self.items.values() if job.state != "running"), None)
            if candidate is None:
                break
            del self.items[candidate.job_id]
            if candidate.request_id is not None:
                self._requests.pop((candidate.workspace_id, candidate.request_id), None)

    def forget_workspace(self, workspace_id: str) -> None:
        for job in list(self.items.values()):
            if job.workspace_id == workspace_id:
                del self.items[job.job_id]
                if job.request_id is not None:
                    self._requests.pop((workspace_id, job.request_id), None)

    async def _spawn(self, job: Job, child_env: dict[str, str]) -> None:
        if job.tty:
            master, slave = os.openpty()
            job.master_fd = master
            os.set_blocking(master, False)
            try:
                terminal.resize(slave, *job.terminal_size)
                job.process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    runner.__file__,
                    "tty",
                    job.command,
                    cwd=job.cwd,
                    env=child_env,
                    start_new_session=True,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                )
            finally:
                os.close(slave)
        else:
            job.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                runner.__file__,
                "pipe",
                job.command,
                cwd=job.cwd,
                env=child_env,
                start_new_session=True,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

    async def _run(self, job: Job, child_env: dict[str, str]) -> None:
        workspace = self.workspaces.get(job.workspace_id)
        reader = None
        final_state: JobState = "failed"
        try:
            if job.cancel_requested:
                final_state = "cancelled"
                return
            await self._spawn(job, child_env)
            job.ready.set()
            reader = asyncio.create_task(self._drain(job))
            assert job.process is not None
            if job.cancel_requested:
                await stop_job(job.process)
            try:
                await asyncio.wait_for(job.process.wait(), job.timeout)
                final_state = (
                    "cancelled"
                    if job.cancel_requested
                    else "completed"
                    if job.process.returncode == 0
                    else "failed"
                )
            except TimeoutError:
                final_state = "timed_out"
        except Exception as error:
            job.error = str(error)[:2048]
        finally:
            job.ready.set()
            try:
                if job.process is not None:
                    await stop_job(job.process)
                    if reader is not None:
                        try:
                            await asyncio.wait_for(reader, 1)
                        except TimeoutError:
                            job.error = "Output draining exceeded its deadline"
                            if final_state == "completed":
                                final_state = "failed"
                    job.exit_code = job.process.returncode
            except Exception as error:
                job.error = str(error)[:2048]
                if final_state == "completed":
                    final_state = "failed"
            finally:
                async with job.input_lock:
                    if job.process is not None and job.process.stdin is not None:
                        job.process.stdin.close()
                    if job.master_fd is not None:
                        os.close(job.master_fd)
                        job.master_fd = None
                    job.stdin_closed = True
                job.state = final_state
                job.finished_at = now()
                job.changed.set()
                self._active -= 1
                workspace.active_jobs -= 1
                workspace.release()

    async def _drain(self, job: Job) -> None:
        assert job.process is not None
        while True:
            if job.tty:
                assert job.master_fd is not None
                data = await terminal.read(job.master_fd)
            else:
                assert job.process.stdout is not None
                data = await job.process.stdout.read(8192)
            if not data:
                break
            job.output.extend(data)
            extra = len(job.output) - self.settings.max_output_bytes
            if extra > 0:
                del job.output[:extra]
                job.output_start += extra
            job.changed.set()

    def snapshot(self, job: Job, cursor: int) -> dict:
        end = job.output_start + len(job.output)
        if cursor < 0 or cursor > end:
            raise SandboxError(
                "invalid_cursor", "Cursor must be between zero and next output offset"
            )
        start = max(cursor, job.output_start)
        stop = min(end, start + self.settings.response_bytes)
        output = bytes(job.output[start - job.output_start : stop - job.output_start])
        return {
            "job_id": job.job_id,
            "workspace_id": job.workspace_id,
            "boot_id": self.boot_id,
            "state": job.state,
            "exit_code": job.exit_code,
            "error": job.error,
            "output": output.decode("utf-8", errors="replace"),
            "cursor": start,
            "next_cursor": stop,
            "output_end": end,
            "truncated": cursor < job.output_start,
            "has_more": stop < end,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "tty": job.tty,
            "terminal_size": {"rows": job.terminal_size[0], "cols": job.terminal_size[1]}
            if job.tty
            else None,
            "stdin_closed": job.stdin_closed,
            "input_offset": job.input_offset,
        }

    async def status(self, job_id: str, cursor: int = 0, wait_seconds: float = 0) -> dict:
        if not 0 <= wait_seconds <= 20:
            raise SandboxError("invalid_wait", "Use 0 <= wait_seconds <= 20")
        job = self.get(job_id)
        return await self._wait_snapshot(job, cursor, wait_seconds)

    async def _wait_snapshot(self, job: Job, cursor: int, wait_seconds: float) -> dict:
        if wait_seconds:
            try:
                async with asyncio.timeout(wait_seconds):
                    while job.state == "running" and job.output_start + len(job.output) <= cursor:
                        job.changed.clear()
                        await job.changed.wait()
            except TimeoutError:
                pass
        return self.snapshot(job, cursor)

    @staticmethod
    def _check_input(job: Job) -> None:
        if (
            job.state != "running"
            or job.cancel_requested
            or job.process is None
            or job.process.returncode is not None
        ):
            raise SandboxError("job_not_running", "Job has ended or is stopping")
        if job.stdin_closed:
            raise SandboxError("stdin_closed", "Job stdin has already been closed")

    async def _send_input(self, job: Job, data: bytes, close_stdin: bool) -> None:
        try:
            async with asyncio.timeout(STDIN_TIMEOUT):
                if job.tty:
                    assert job.master_fd is not None
                    offset = 0
                    while offset < len(data):
                        try:
                            count = os.write(job.master_fd, data[offset:])
                        except BlockingIOError:
                            await terminal.ready(job.master_fd, writing=True)
                            continue
                        offset += count
                        job.input_offset += count
                else:
                    assert job.process is not None and job.process.stdin is not None
                    stream = job.process.stdin
                    stream.write(data)
                    job.input_offset += len(data)
                    await stream.drain()
                    if close_stdin:
                        job.stdin_closed = True
                        stream.close()
                        await stream.wait_closed()
        except TimeoutError as error:
            raise SandboxError(
                "stdin_blocked",
                "Input backpressure timed out; inspect input_offset before retrying",
            ) from error
        except OSError as error:
            job.stdin_closed = True
            raise SandboxError(
                "stdin_closed", "Process closed stdin; inspect input_offset"
            ) from error

    async def write_stdin(
        self,
        job_id: str,
        chars: str = "",
        close_stdin: bool = False,
        expected_input_offset: int | None = None,
        cursor: int = 0,
        wait_seconds: float = 0,
    ) -> dict:
        if not 0 <= wait_seconds <= 20:
            raise SandboxError("invalid_wait", "Use 0 <= wait_seconds <= 20")
        try:
            data = chars.encode("utf-8")
        except UnicodeEncodeError as error:
            raise SandboxError("invalid_input", "stdin must be valid UTF-8") from error
        if len(data) > MAX_STDIN_BYTES:
            raise SandboxError("input_too_large", "Send at most 64 KiB of UTF-8 per call")
        if expected_input_offset is not None and expected_input_offset < 0:
            raise SandboxError("invalid_input", "expected_input_offset must be nonnegative")
        job = self.get(job_id)
        self.snapshot(job, cursor)
        if job.tty and close_stdin:
            raise SandboxError(
                "invalid_input", "A PTY cannot half-close; send Ctrl-D or cancel the job"
            )
        await job.ready.wait()
        async with job.input_lock:
            self._check_input(job)
            if expected_input_offset is not None and expected_input_offset != job.input_offset:
                raise SandboxError("stdin_conflict", "Input offset changed; inspect job_status")
            sending = asyncio.create_task(self._send_input(job, data, close_stdin))
            await finish_task(sending)
        return {**await self._wait_snapshot(job, cursor, wait_seconds), "bytes_written": len(data)}

    async def resize(self, job_id: str, rows: int, cols: int) -> dict:
        self._validate_size(rows, cols)
        job = self.get(job_id)
        if not job.tty:
            raise SandboxError("not_a_terminal", "Resize requires a job started with tty=true")
        await job.ready.wait()
        async with job.input_lock:
            self._check_input(job)
            assert job.master_fd is not None
            terminal.resize(job.master_fd, rows, cols)
            job.terminal_size = (rows, cols)
        return self.snapshot(job, 0)

    async def cancel(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job.state == "running":
            job.cancel_requested = True

            async def stop():
                if job.process is not None:
                    await stop_job(job.process)
                if job.task is not None:
                    await asyncio.shield(job.task)

            await finish_task(asyncio.create_task(stop()))
        return self.snapshot(job, 0)

    async def shutdown(self) -> None:
        self._closing = True
        await asyncio.gather(
            *[
                self.cancel(job.job_id)
                for job in list(self.items.values())
                if job.state == "running"
            ]
        )
