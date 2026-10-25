import asyncio
import base64

import pytest

from runlet.errors import SandboxError
from runlet.jobs import Jobs
from runlet.workspaces import Workspaces


async def test_parallel_workspaces_and_mutation_limits(sandbox):
    workspaces, jobs, files = sandbox
    ids = [(await workspaces.create())["workspace_id"] for _ in range(3)]
    first = await jobs.start(ids[0], "printf first; sleep 30", wait_seconds=0)
    second = await jobs.start(ids[1], "printf second; sleep 30", wait_seconds=0)
    assert first["state"] == second["state"] == "running"
    await files.write(ids[0], "file.txt", "written alongside a running job")
    assert (await files.read(ids[0], "file.txt"))["content"] == "written alongside a running job"
    with pytest.raises(SandboxError, match="workspace_busy"):
        await workspaces.delete(ids[0])
    with pytest.raises(SandboxError, match="server_busy"):
        await jobs.start(ids[2], "echo should-not-run")
    await jobs.cancel(first["job_id"])
    third = await jobs.start(ids[2], "printf third")
    assert third["state"] == "completed"
    assert third["output"] == "third"
    await jobs.cancel(second["job_id"])


async def test_cancel_kills_descendants_and_timeout_releases_workspace(sandbox):
    workspaces, jobs, _ = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    started = await jobs.start(
        workspace_id,
        "printf ready; (sleep 0.8; printf leaked > leaked.txt) & wait",
        wait_seconds=0,
    )
    ready = await jobs.status(started["job_id"], wait_seconds=2)
    assert "ready" in ready["output"]
    cancelled = await jobs.cancel(started["job_id"])
    assert cancelled["state"] == "cancelled"
    await asyncio.sleep(0.9)
    assert not (workspaces.get(workspace_id).work / "leaked.txt").exists()
    timed_out = await jobs.start(workspace_id, "sleep 30", timeout=0.1)
    assert timed_out["state"] == "timed_out"
    assert (await jobs.start(workspace_id, "printf recovered"))["output"] == "recovered"


async def test_deduplicated_requests_and_client_cancellation(sandbox):
    workspaces, jobs, _ = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    arguments = dict(
        workspace_id=workspace_id,
        command="printf x >> count; sleep 0.3; printf done",
        wait_seconds=0,
        request_id="request-1",
    )
    first, second = await asyncio.gather(jobs.start(**arguments), jobs.start(**arguments))
    assert first["job_id"] == second["job_id"]
    await jobs.get(first["job_id"]).task
    assert (workspaces.get(workspace_id).work / "count").read_text() == "x"
    with pytest.raises(SandboxError, match="request_conflict"):
        await jobs.start(workspace_id, "echo different", request_id="request-1")
    waiting = asyncio.create_task(
        jobs.start(workspace_id, "sleep 0.3; printf survived", wait_seconds=20)
    )
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    job = list(jobs.items.values())[-1]
    await job.task
    assert (await jobs.status(job.job_id))["output"] == "survived"


async def test_output_is_bounded_and_incremental(tiny_settings):
    workspaces = Workspaces(tiny_settings)
    jobs = Jobs(tiny_settings, workspaces, "bounded")
    try:
        workspace_id = (await workspaces.create())["workspace_id"]
        first = await jobs.start(workspace_id, "python -c \"print('x' * 1000)\"")
        assert first["state"] == "completed"
        assert first["truncated"] is True
        assert len(jobs.get(first["job_id"]).output) == 128
        pieces = [first["output"]]
        chunk = first
        while chunk["has_more"]:
            chunk = await jobs.status(first["job_id"], chunk["next_cursor"])
            pieces.append(chunk["output"])
        assert "".join(pieces) == ("x" * 127 + "\n")
        assert (await jobs.status(first["job_id"], chunk["next_cursor"]))["output"] == ""
    finally:
        await jobs.shutdown()


async def test_file_chunks_modes_and_path_boundaries(sandbox, tmp_path):
    workspaces, _, files = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    await files.write(workspace_id, "src/script.sh", "echo hello\n")
    script = workspaces.get(workspace_id).work / "src/script.sh"
    script.chmod(0o755)
    await files.write(workspace_id, "src/script.sh", "echo updated\n")
    assert script.stat().st_mode & 0o777 == 0o755
    data = b"\x00\xffhello"
    await files.write(workspace_id, "data.bin", base64.b64encode(data).decode(), "base64")
    first = await files.read(workspace_id, "data.bin", max_bytes=2, encoding="base64")
    assert base64.b64decode(first["content"]) == data[:2]
    assert first["eof"] is False
    second = await files.read(
        workspace_id,
        "data.bin",
        offset=first["next_offset"],
        encoding="base64",
    )
    assert base64.b64decode(second["content"]) == data[2:]
    secret = tmp_path / "outside.txt"
    secret.write_text("outside")
    (workspaces.get(workspace_id).work / "escape").symlink_to(secret)
    for path in ("../outside.txt", str(secret), "escape"):
        with pytest.raises(SandboxError, match="invalid_path"):
            await files.read(workspace_id, path)
        with pytest.raises(SandboxError, match="invalid_path"):
            await files.write(workspace_id, path, "no")
    assert secret.read_text() == "outside"


async def test_execution_environment_and_fresh_shells(sandbox, monkeypatch):
    monkeypatch.setenv("SANDBOX_TOKEN", "must-not-inherit")
    monkeypatch.setenv("OTHER_PROVIDER_KEY", "must-not-inherit")
    workspaces, jobs, _ = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    result = await jobs.start(
        workspace_id,
        'python -c "import os,sys; '
        "assert 'SANDBOX_TOKEN' not in os.environ; "
        "assert 'OTHER_PROVIDER_KEY' not in os.environ; "
        "print(sys.prefix); print(os.environ['HOME']); print(os.getcwd())\"",
    )
    assert result["state"] == "completed", result
    runtime = workspaces.get(workspace_id).runtime
    assert str(runtime / "venv") in result["output"]
    assert str(runtime / "home") in result["output"]
    await jobs.start(workspace_id, "export LOCAL_VALUE=temporary")
    result = await jobs.start(workspace_id, "printenv LOCAL_VALUE || printf unset")
    assert result["output"] == "unset"
