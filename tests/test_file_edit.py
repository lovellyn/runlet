from dataclasses import replace

import pytest

from runlet.errors import SandboxError
from runlet.files import Files
from runlet.workspaces import Workspaces


async def test_targeted_edit_preserves_other_bytes_permissions_and_line_endings(sandbox):
    workspaces, _, files = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    path = workspaces.get(workspace_id).work / "script.py"
    original = "# café\r\nTIMEOUT = 30\r\nprint('unchanged')\r\n".encode()
    path.write_bytes(original)
    path.chmod(0o755)
    result = await files.edit(workspace_id, "script.py", "TIMEOUT = 30", "TIMEOUT = 60")
    assert result["replacements"] == 1
    assert path.read_bytes() == original.replace(b"TIMEOUT = 30", b"TIMEOUT = 60")
    assert path.stat().st_mode & 0o777 == 0o755
    assert result["bytes_written"] == len(original)
    await files.edit(workspace_id, "script.py", "TIMEOUT = 60\r\n", "")
    assert path.read_bytes() == original.replace(b"TIMEOUT = 30\r\n", b"")


async def test_ambiguous_missing_invalid_and_replace_all_edits(sandbox):
    workspaces, _, files = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    await files.write(workspace_id, "settings.txt", "value=old\nbackup=old\n")
    target = workspaces.get(workspace_id).work / "settings.txt"
    original = target.read_bytes()
    for old_text, new_text, error in [
        ("old", "new", "edit_ambiguous"),
        ("missing", "new", "edit_not_found"),
        ("", "new", "invalid_edit"),
        ("old", "old", "invalid_edit"),
    ]:
        with pytest.raises(SandboxError, match=error):
            await files.edit(workspace_id, "settings.txt", old_text, new_text)
        assert target.read_bytes() == original
    result = await files.edit(workspace_id, "settings.txt", "old", "new", replace_all=True)
    assert result["replacements"] == 2
    assert target.read_text() == "value=new\nbackup=new\n"
    await files.write(workspace_id, "overlapping.txt", "aaa")
    with pytest.raises(SandboxError, match="edit_ambiguous"):
        await files.edit(workspace_id, "overlapping.txt", "aa", "x")
    assert (await files.read(workspace_id, "overlapping.txt"))["content"] == "aaa"


async def test_edit_rejects_missing_binary_and_escaping_files(sandbox, tmp_path):
    workspaces, _, files = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    with pytest.raises(SandboxError, match="file_not_found"):
        await files.edit(workspace_id, "missing.txt", "old", "new")
    root = workspaces.get(workspace_id).work
    (root / "binary.dat").write_bytes(b"\xffold")
    with pytest.raises(SandboxError, match="invalid_encoding"):
        await files.edit(workspace_id, "binary.dat", "old", "new")
    assert (root / "binary.dat").read_bytes() == b"\xffold"
    outside = tmp_path / "outside.txt"
    outside.write_text("old")
    (root / "link.txt").symlink_to(outside)
    for path in ("../outside.txt", str(outside), "link.txt"):
        with pytest.raises(SandboxError, match="invalid_path"):
            await files.edit(workspace_id, path, "old", "new")
    assert outside.read_text() == "old"


async def test_edit_size_limits_are_checked_before_writing(settings):
    small = replace(settings, max_file_bytes=16)
    workspaces = Workspaces(small)
    files = Files(workspaces)
    workspace_id = (await workspaces.create())["workspace_id"]
    path = workspaces.get(workspace_id).work / "small.txt"
    path.write_bytes(b"old " * 4)
    with pytest.raises(SandboxError, match="file_too_large"):
        await files.edit(workspace_id, "small.txt", "old", "longer", replace_all=True)
    assert path.read_bytes() == b"old " * 4
    with pytest.raises(SandboxError, match="file_too_large"):
        await files.edit(workspace_id, "small.txt", "old", "x" * 17)
    assert path.read_bytes() == b"old " * 4
    path.write_bytes(b"x" * 17)
    with pytest.raises(SandboxError, match="file_too_large"):
        await files.edit(workspace_id, "small.txt", "x", "y", replace_all=True)
    assert path.read_bytes() == b"x" * 17


async def test_edit_runs_alongside_active_job(sandbox):
    workspaces, jobs, files = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    await files.write(workspace_id, "settings.txt", "old")
    job = await jobs.start(workspace_id, "sleep 30", wait_seconds=0)
    await files.edit(workspace_id, "settings.txt", "old", "new")
    assert (await jobs.status(job["job_id"]))["state"] == "running"
    await jobs.cancel(job["job_id"])
    assert (await files.read(workspace_id, "settings.txt"))["content"] == "new"
