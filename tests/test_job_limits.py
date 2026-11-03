import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx2 as httpx
import pytest

from runlet.errors import SandboxError
from runlet.jobs import Jobs
from runlet.server import create_app
from runlet.workspaces import Workspace, Workspaces
from tests.test_mcp import connect


def register(workspaces):
    for workspace_id in ("alpha", "beta", "gamma"):
        directory = workspaces.root / workspace_id
        (directory / "work").mkdir(parents=True)
        workspace = Workspace(workspace_id, directory, None, "test")
        workspaces.items[workspace_id] = workspace


@pytest.fixture
def limited_jobs(settings):
    @asynccontextmanager
    async def opened(global_limit=3, workspace_limit=2):
        configured = replace(
            settings, max_jobs=global_limit, max_jobs_per_workspace=workspace_limit
        )
        workspaces = Workspaces(configured)
        register(workspaces)
        jobs = Jobs(configured, workspaces, "limits-test")
        try:
            yield workspaces, jobs
        finally:
            await jobs.shutdown()

    return opened


async def test_parallel_admission_enforces_both_limits_without_leaking_slots(limited_jobs):
    async with limited_jobs() as (workspaces, jobs):
        results = await asyncio.gather(
            *(jobs.start("alpha", "sleep 30", wait_seconds=0) for _ in range(8)),
            return_exceptions=True,
        )
        admitted = [result for result in results if isinstance(result, dict)]
        rejected = [result for result in results if isinstance(result, SandboxError)]
        assert len(admitted) == 2 and len(rejected) == 6
        assert {error.code for error in rejected} == {"workspace_job_limit"}
        assert workspaces.get("alpha").active_jobs == jobs._active == 2

        # Rejections in alpha do not consume global slots needed by another workspace.
        results = await asyncio.gather(
            *(jobs.start("beta", "sleep 30", wait_seconds=0) for _ in range(8)),
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        assert {result.code for result in results if isinstance(result, SandboxError)} == {
            "server_busy"
        }
        assert jobs._active == 3
        assert workspaces.get("beta").active_jobs == 1
        assert len(jobs.items) == 3
        assert workspaces.get("gamma").active_operations == 0


async def test_workspace_cap_above_global_cap_does_not_expand_global_capacity(limited_jobs):
    async with limited_jobs(global_limit=1, workspace_limit=4) as (workspaces, jobs):
        first = await jobs.start("alpha", "sleep 30", wait_seconds=0)
        with pytest.raises(SandboxError, match="server_busy"):
            await jobs.start("alpha", "true", wait_seconds=0)
        with pytest.raises(SandboxError, match="server_busy"):
            await jobs.start("beta", "true", wait_seconds=0)
        assert jobs._active == 1 and workspaces.get("beta").active_jobs == 0
        await jobs.cancel(first["job_id"])
        assert (await jobs.start("beta", "printf available"))["output"] == "available"


async def test_matching_request_retry_succeeds_when_both_limits_are_full(limited_jobs):
    async with limited_jobs(global_limit=2, workspace_limit=1) as (workspaces, jobs):
        arguments = {
            "workspace_id": "alpha",
            "command": "sleep 30",
            "request_id": "same-request",
            "wait_seconds": 0,
        }
        first = await jobs.start(**arguments)
        other = await jobs.start("beta", "sleep 30", wait_seconds=0)
        replay = await jobs.start(**arguments)
        assert replay["job_id"] == first["job_id"]
        assert jobs._active == 2 and workspaces.get("alpha").active_jobs == 1
        with pytest.raises(SandboxError, match="request_conflict"):
            await jobs.start("alpha", "echo changed", request_id="same-request")
        with pytest.raises(SandboxError, match="workspace_job_limit"):
            await jobs.start("alpha", "true", wait_seconds=0)
        await jobs.cancel(other["job_id"])
        # Identical request IDs in different workspaces remain independent.
        independent = await jobs.start("beta", "printf independent", request_id="same-request")
        assert independent["job_id"] != first["job_id"]
        assert independent["output"] == "independent"


@pytest.mark.parametrize("ending", ["completed", "failed", "timed_out", "cancelled", "spawn_error"])
async def test_every_job_exit_releases_workspace_and_global_capacity(
    limited_jobs, monkeypatch, ending
):
    async with limited_jobs(global_limit=1, workspace_limit=1) as (workspaces, jobs):
        if ending == "spawn_error":
            original = jobs._spawn
            failed = False

            async def fail_once(job, environment):
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("simulated spawn failure")
                await original(job, environment)

            monkeypatch.setattr(jobs, "_spawn", fail_once)
        command = {"completed": "printf done", "failed": "exit 7"}.get(ending, "sleep 30")
        started = await jobs.start(
            "alpha",
            command,
            timeout=0.05 if ending == "timed_out" else None,
            wait_seconds=0 if ending == "cancelled" else 2,
        )
        result = await jobs.cancel(started["job_id"]) if ending == "cancelled" else started
        assert result["state"] == ("failed" if ending == "spawn_error" else ending)
        workspace = workspaces.get("alpha")
        assert workspace.active_jobs == workspace.active_operations == jobs._active == 0
        assert (await jobs.start("alpha", "printf recovered"))["output"] == "recovered"


async def test_cancelling_client_wait_does_not_release_a_running_job_slot(limited_jobs):
    async with limited_jobs(global_limit=2, workspace_limit=1) as (workspaces, jobs):
        waiting = asyncio.create_task(jobs.start("alpha", "sleep 30", wait_seconds=20))
        await asyncio.sleep(0)
        job = next(iter(jobs.items.values()))
        await asyncio.wait_for(job.ready.wait(), 2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert workspaces.get("alpha").active_jobs == jobs._active == 1
        with pytest.raises(SandboxError, match="workspace_job_limit"):
            await jobs.start("alpha", "true")
        assert (await jobs.start("beta", "printf other"))["output"] == "other"
        await jobs.cancel(job.job_id)
        assert (await jobs.start("alpha", "printf recovered"))["output"] == "recovered"


async def test_mcp_reports_both_limits_and_distinct_admission_errors(settings):
    app = create_app(replace(settings, max_jobs=2, max_jobs_per_workspace=1))
    register(app.state.services.workspaces)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"X-Sandbox-Token": settings.token},
        ) as http:
            async with connect(http) as client:
                result = await client.call_tool("server_info", {})
                assert result.structured_content["max_concurrent_jobs"] == 2
                assert result.structured_content["max_jobs_per_workspace"] == 1
                first = await client.call_tool(
                    "exec", {"workspace_id": "alpha", "command": "sleep 30", "wait_seconds": 0}
                )
                assert not first.is_error
                blocked = await client.call_tool(
                    "exec", {"workspace_id": "alpha", "command": "true", "wait_seconds": 0}
                )
                assert blocked.is_error
                assert any(
                    block.type == "text" and "workspace_job_limit" in block.text
                    for block in blocked.content
                )
                other = await client.call_tool(
                    "exec", {"workspace_id": "beta", "command": "sleep 30", "wait_seconds": 0}
                )
                assert not other.is_error
                blocked = await client.call_tool(
                    "exec", {"workspace_id": "gamma", "command": "true", "wait_seconds": 0}
                )
                assert blocked.is_error
                assert any(
                    block.type == "text" and "server_busy" in block.text
                    for block in blocked.content
                )
