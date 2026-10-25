import asyncio

import pytest

from runlet.errors import SandboxError


async def test_pipe_input_replay_guard_and_eof(sandbox):
    workspaces, jobs, _ = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    result = await jobs.start(workspace_id, "cat", wait_seconds=0)
    job_id = result["job_id"]
    sent = await jobs.write_stdin(job_id, "café\n", expected_input_offset=0)
    assert sent["input_offset"] == len("café\n".encode())
    with pytest.raises(SandboxError, match="stdin_conflict"):
        await jobs.write_stdin(job_id, "duplicate", expected_input_offset=0)
    await jobs.write_stdin(job_id, close_stdin=True)
    await asyncio.wait_for(jobs.get(job_id).task, 3)
    final = await jobs.status(job_id)
    assert final["state"] == "completed" and final["exit_code"] == 0
    assert final["output"] == "café\n"


async def test_pty_input_and_resize(sandbox):
    workspaces, jobs, _ = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    result = await jobs.start(
        workspace_id,
        "read -r x; stty size; printf 'received:%s' \"$x\"",
        tty=True,
        wait_seconds=0,
    )
    job_id = result["job_id"]
    resized = await jobs.resize(job_id, 40, 120)
    assert resized["terminal_size"] == {"rows": 40, "cols": 120}
    await jobs.write_stdin(job_id, "hello\n")
    await asyncio.wait_for(jobs.get(job_id).task, 3)
    final = await jobs.status(job_id)
    assert final["state"] == "completed" and final["exit_code"] == 0
    assert "40 120" in final["output"] and "received:hello" in final["output"]


async def test_pty_control_c_stops_foreground_command(sandbox):
    workspaces, jobs, _ = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    result = await jobs.start(
        workspace_id,
        "python -c \"import time; print('ready', flush=True); time.sleep(30)\"",
        tty=True,
        wait_seconds=0,
    )
    job_id = result["job_id"]
    async with asyncio.timeout(3):
        snapshot = await jobs.status(job_id, wait_seconds=2)
        while "ready" not in snapshot["output"]:
            snapshot = await jobs.status(job_id, cursor=snapshot["next_cursor"], wait_seconds=2)
    await jobs.write_stdin(job_id, "\x03")
    await asyncio.wait_for(jobs.get(job_id).task, 3)
    final = await jobs.status(job_id)
    assert final["state"] == "failed" and final["exit_code"] != 0
    assert final["error"] is None
