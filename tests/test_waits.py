import asyncio
import shlex
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx2 as httpx
import pytest

import runlet.jobs as module
from examples.client import data, follow_job
from runlet.errors import SandboxError
from runlet.jobs import Jobs
from runlet.server import create_app
from runlet.workspaces import Workspace, Workspaces
from tests.test_mcp import connect


def python(code):
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(code)}"


def register(workspaces):
    directory = workspaces.root / "wait-test"
    (directory / "work").mkdir(parents=True)
    workspace = Workspace("wait-test", directory, None, "test")
    workspaces.items[workspace.workspace_id] = workspace
    return workspace


@pytest.fixture
def wait_jobs(settings):
    @asynccontextmanager
    async def opened(**changes):
        configured = replace(settings, **changes)
        workspaces = Workspaces(configured)
        workspace = register(workspaces)
        jobs = Jobs(configured, workspaces, "wait-boot")
        try:
            yield jobs, workspace
        finally:
            await jobs.shutdown()

    return opened


async def emitted(jobs, job_id, marker=b"ready"):
    job = jobs.get(job_id)
    async with asyncio.timeout(3):
        while marker not in job.output:
            job.changed.clear()
            await job.changed.wait()


@pytest.mark.parametrize("api", ["exec", "status"])
async def test_completion_mode_ignores_output_until_process_finishes(wait_jobs, api):
    async with wait_jobs() as (jobs, workspace):
        command = "printf ready; read -r x; printf done"
        if api == "exec":
            waiting = asyncio.create_task(
                jobs.start(workspace.workspace_id, command, wait_seconds=3)
            )
            await asyncio.sleep(0)
            job_id = next(iter(jobs.items))
        else:
            started = await jobs.start(workspace.workspace_id, command, wait_seconds=0)
            job_id = started["job_id"]
            waiting = asyncio.create_task(jobs.status(job_id, wait_seconds=3))
        try:
            await emitted(jobs, job_id)
            await asyncio.sleep(0.25)
            assert not waiting.done()
            await jobs.write_stdin(job_id, "go\n")
            result = await asyncio.wait_for(waiting, 2)
            assert result["state"] == "completed" and result["output"] == "readydone"
        finally:
            if not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.parametrize("api", ["exec", "status"])
async def test_output_mode_batches_small_writes_and_returns_while_running(wait_jobs, api):
    async with wait_jobs() as (jobs, workspace):
        command = python(
            "import sys,time; "
            "sys.stdout.write('a'); sys.stdout.flush(); time.sleep(0.02); "
            "sys.stdout.write('b'); sys.stdout.flush(); time.sleep(0.02); "
            "sys.stdout.write('c'); sys.stdout.flush(); sys.stdin.readline()"
        )
        started = time.monotonic()
        if api == "exec":
            result = await jobs.start(
                workspace.workspace_id, command, wait_seconds=3, wait_for="output"
            )
        else:
            initial = await jobs.start(workspace.workspace_id, command, wait_seconds=0)
            result = await jobs.status(initial["job_id"], wait_seconds=3, wait_for="output")
        elapsed = time.monotonic() - started
        assert result["state"] == "running"
        assert result["output"] == "abc"
        assert 0.15 <= elapsed < 2
        await jobs.write_stdin(result["job_id"], "\n")


async def test_continuous_output_does_not_reset_the_batch_deadline(wait_jobs):
    async with wait_jobs() as (jobs, workspace):
        command = python(
            "import sys,time\n"
            "for _ in range(50):\n"
            " print('x',end='',flush=True)\n"
            " time.sleep(0.03)\n"
            "sys.stdin.readline()"
        )
        initial = await jobs.start(workspace.workspace_id, command, wait_seconds=0)
        started = time.monotonic()
        result = await jobs.status(initial["job_id"], wait_seconds=3, wait_for="output")
        elapsed = time.monotonic() - started
        assert result["state"] == "running"
        assert 0 < len(result["output"]) < 50
        assert elapsed < 1


async def test_wait_deadline_caps_batching_and_leaves_job_running(wait_jobs):
    async with wait_jobs() as (jobs, workspace):
        initial = await jobs.start(
            workspace.workspace_id, "printf ready; read -r x", wait_seconds=0
        )
        job_id = initial["job_id"]
        await emitted(jobs, job_id)
        started = time.monotonic()
        result = await jobs.status(job_id, wait_seconds=0.05, wait_for="output")
        assert 0.035 <= time.monotonic() - started < 0.5
        assert result["state"] == "running" and result["output"] == "ready"
        assert not jobs.get(job_id).task.done()
        # Completion mode must also honor its deadline despite unread output.
        result = await jobs.status(job_id, wait_seconds=0.05, wait_for="completion")
        assert result["state"] == "running"
        await jobs.write_stdin(job_id, "\n")
        assert (await jobs.status(job_id))["state"] == "completed"


async def test_zero_wait_is_immediate_and_completion_can_interrupt_a_batch(wait_jobs, monkeypatch):
    monkeypatch.setattr(module, "OUTPUT_BATCH_SECONDS", 2)
    async with wait_jobs() as (jobs, workspace):
        initial = await jobs.start(
            workspace.workspace_id, "printf ready; read -r x", wait_seconds=0
        )
        job_id = initial["job_id"]
        await emitted(jobs, job_id)
        result = await asyncio.wait_for(jobs.status(job_id, wait_seconds=0, wait_for="output"), 0.5)
        assert result["state"] == "running" and result["output"] == "ready"
        waiting = asyncio.create_task(jobs.status(job_id, wait_seconds=3, wait_for="output"))
        await asyncio.sleep(0)
        await jobs.write_stdin(job_id, "\n")
        result = await asyncio.wait_for(waiting, 1)
        assert result["state"] == "completed"


@pytest.mark.parametrize("wait_for", ["completion", "output"])
async def test_waiter_cancellation_does_not_cancel_the_job(wait_jobs, wait_for):
    async with wait_jobs() as (jobs, workspace):
        initial = await jobs.start(workspace.workspace_id, "read -r x", wait_seconds=0)
        job_id = initial["job_id"]
        waiting = asyncio.create_task(jobs.status(job_id, wait_seconds=20, wait_for=wait_for))
        await asyncio.sleep(0.02)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not jobs.get(job_id).task.cancelled()
        assert workspace.active_jobs == 1
        await jobs.write_stdin(job_id, "\n")
        assert (await jobs.status(job_id))["state"] == "completed"


@pytest.mark.parametrize("wait_for", ["completion", "output"])
async def test_explicit_job_cancellation_wakes_waiters(wait_jobs, wait_for):
    async with wait_jobs() as (jobs, workspace):
        initial = await jobs.start(workspace.workspace_id, "read -r x", wait_seconds=0)
        waiting = asyncio.create_task(
            jobs.status(initial["job_id"], wait_seconds=20, wait_for=wait_for)
        )
        await asyncio.sleep(0)
        await jobs.cancel(initial["job_id"])
        result = await asyncio.wait_for(waiting, 1)
        assert result["state"] == "cancelled" and workspace.active_jobs == 0


async def test_completion_wait_reports_process_timeout_as_terminal(wait_jobs):
    async with wait_jobs() as (jobs, workspace):
        initial = await jobs.start(workspace.workspace_id, "sleep 30", timeout=0.05, wait_seconds=0)
        result = await jobs.status(initial["job_id"])
        assert result["state"] == "timed_out" and result["finished_at"] is not None


async def test_output_wait_handles_a_pty_prompt_without_a_newline(wait_jobs):
    async with wait_jobs() as (jobs, workspace):
        result = await jobs.start(
            workspace.workspace_id,
            "printf 'name? '; read -r name; printf 'hello:%s' \"$name\"",
            tty=True,
            wait_for="output",
            wait_seconds=3,
        )
        assert result["state"] == "running" and result["output"] == "name? "
        await jobs.write_stdin(result["job_id"], "agent\n")
        final = await jobs.status(result["job_id"], cursor=result["next_cursor"])
        assert final["state"] == "completed" and "hello:agent" in final["output"]


async def test_response_sized_output_bypasses_batch_delay_and_preserves_cursors(
    wait_jobs, monkeypatch
):
    monkeypatch.setattr(module, "OUTPUT_BATCH_SECONDS", 2)
    async with wait_jobs(response_bytes=4, max_output_bytes=8) as (jobs, workspace):
        initial = await jobs.start(
            workspace.workspace_id, "printf abcdefghijkl; read -r x", wait_seconds=0
        )
        job_id = initial["job_id"]
        await emitted(jobs, job_id, b"ijkl")
        result = await asyncio.wait_for(jobs.status(job_id, wait_for="output", wait_seconds=3), 0.5)
        assert result["output"] == "efgh"
        assert result["truncated"] and result["has_more"]
        assert result["cursor"] == 4 and result["next_cursor"] == 8
        next_page = await asyncio.wait_for(
            jobs.status(job_id, cursor=8, wait_for="output", wait_seconds=3), 0.5
        )
        assert next_page["output"] == "ijkl" and not next_page["has_more"]
        assert next_page["next_cursor"] == 12 and not next_page["truncated"]


async def test_concurrent_readers_have_independent_output_cursors(wait_jobs):
    async with wait_jobs() as (jobs, workspace):
        initial = await jobs.start(
            workspace.workspace_id, "read -r x; printf same; read -r x", wait_seconds=0
        )
        job_id = initial["job_id"]
        waiters = [
            asyncio.create_task(jobs.status(job_id, wait_for="output", wait_seconds=3))
            for _ in range(2)
        ]
        await jobs.write_stdin(job_id, "\n")
        results = await asyncio.gather(*waiters)
        assert [result["output"] for result in results] == ["same", "same"]
        assert [result["next_cursor"] for result in results] == [4, 4]


async def test_invalid_wait_or_cursor_rejected_before_waiting_or_starting(wait_jobs):
    async with wait_jobs() as (jobs, workspace):
        for options in (
            {"wait_for": "invalid"},
            {"wait_seconds": 21},
            {"wait_seconds": float("nan")},
        ):
            with pytest.raises(SandboxError, match="invalid_wait"):
                await jobs.start(workspace.workspace_id, "true", **options)
        assert jobs.items == {} and workspace.active_jobs == 0
        initial = await jobs.start(workspace.workspace_id, "read -r x", wait_seconds=0)
        with pytest.raises(SandboxError, match="invalid_cursor"):
            await asyncio.wait_for(jobs.status(initial["job_id"], cursor=1), 0.5)


async def test_live_mcp_schemas_wait_defaults_and_follow_helper(settings):
    app = create_app(settings)
    workspace = register(app.state.services.workspaces)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"X-Sandbox-Token": settings.token},
        ) as http:
            async with connect(http) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                for name in ("exec", "job_status"):
                    schema = tools[name].input_schema
                    assert schema["properties"]["wait_for"]["default"] == "completion"
                    assert set(schema["properties"]["wait_for"]["enum"]) == {"completion", "output"}
                assert (
                    tools["job_status"].input_schema["properties"]["wait_seconds"]["default"] == 20
                )
                info = data(await client.call_tool("server_info", {}))
                assert info["waiting"]["output_batch_seconds"] == 0.2
                initial = data(
                    await client.call_tool(
                        "exec",
                        {
                            "workspace_id": workspace.workspace_id,
                            "command": "printf first; sleep 0.1; printf last",
                            "wait_seconds": 0,
                        },
                    )
                )
                results = [result async for result in follow_job(client, initial)]
                assert results[-1]["state"] == "completed"
                assert "".join(result["output"] for result in results) == "firstlast"


@pytest.mark.parametrize("wait_for", ["completion", "output"])
async def test_quiet_job_returns_at_wait_deadline_without_cancelling(wait_jobs, wait_for):
    async with wait_jobs() as (jobs, workspace):
        initial = await jobs.start(workspace.workspace_id, "read -r x", wait_seconds=0)
        started = time.monotonic()
        result = await jobs.status(initial["job_id"], wait_for=wait_for, wait_seconds=0.05)
        assert 0.035 <= time.monotonic() - started < 0.5
        assert result["state"] == "running" and result["output"] == ""
        assert workspace.active_jobs == 1


async def test_request_id_retry_can_change_wait_mode_without_starting_a_duplicate(wait_jobs):
    async with wait_jobs() as (jobs, workspace):
        command = "printf ready; read -r x; printf done"
        initial = await jobs.start(
            workspace.workspace_id,
            command,
            request_id="wait-mode",
            wait_for="output",
            wait_seconds=3,
        )
        waiting = asyncio.create_task(
            jobs.start(
                workspace.workspace_id,
                command,
                request_id="wait-mode",
                wait_for="completion",
                wait_seconds=3,
            )
        )
        await asyncio.sleep(0.02)
        assert not waiting.done() and len(jobs.items) == 1
        await jobs.write_stdin(initial["job_id"], "\n")
        final = await asyncio.wait_for(waiting, 2)
        assert final["job_id"] == initial["job_id"] and final["state"] == "completed"
