import asyncio


async def test_parallel_edits_preserve_all_changes_while_job_runs(sandbox):
    workspaces, jobs, files = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    await files.write(workspace_id, "settings.txt", "".join(f"k{i}=old\n" for i in range(12)))
    job = await jobs.start(workspace_id, "sleep 30", wait_seconds=0)
    await asyncio.gather(
        *(files.edit(workspace_id, "settings.txt", f"k{i}=old", f"k{i}=new") for i in range(12))
    )
    assert (await files.read(workspace_id, "settings.txt"))["content"] == "".join(
        f"k{i}=new\n" for i in range(12)
    )
    await asyncio.gather(*(files.write(workspace_id, f"file-{i}.txt", str(i)) for i in range(12)))
    results = await asyncio.gather(*(files.read(workspace_id, f"file-{i}.txt") for i in range(12)))
    assert [result["content"] for result in results] == [str(i) for i in range(12)]
    assert (await jobs.status(job["job_id"]))["state"] == "running"
    await jobs.cancel(job["job_id"])
