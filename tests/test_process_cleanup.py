import asyncio
import shlex
import time
from pathlib import Path

import pytest


def wait_for_pid_file(path):
    deadline = time.monotonic() + 3
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError("Detached child did not become ready")
        time.sleep(0.01)


@pytest.mark.parametrize("end", ["normal", "timeout", "cancel"])
async def test_double_fork_ignoring_term_is_reaped(sandbox, end):
    workspaces, jobs, _ = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    pid_file = workspaces.get(workspace_id).work / "detached.pid"
    script = f"""
import os, signal, time
from pathlib import Path
first = os.fork()
if not first:
    os.setsid()
    if os.fork():
        os._exit(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path("detached.pid").write_text(str(os.getpid()))
    while True: time.sleep(1)
os.waitpid(first, 0)
while not Path("detached.pid").exists(): time.sleep(.005)
if {end!r} == "normal":
    raise SystemExit(7)
while True: time.sleep(1)
"""
    result = await jobs.start(
        workspace_id,
        "python -c " + shlex.quote(script),
        timeout=1 if end == "timeout" else 10,
        wait_seconds=0,
    )
    await asyncio.to_thread(wait_for_pid_file, pid_file)
    pid = int(pid_file.read_text())
    if end == "cancel":
        await jobs.cancel(result["job_id"])
    await asyncio.wait_for(jobs.get(result["job_id"]).task, 4)
    final = await jobs.status(result["job_id"])
    assert (
        final["state"] == {"normal": "failed", "timeout": "timed_out", "cancel": "cancelled"}[end]
    ), final
    if end == "normal":
        assert final["exit_code"] == 7
        assert final["error"] is None
    assert not await asyncio.to_thread(Path(f"/proc/{pid}").exists), f"Child {pid} was not reaped"
