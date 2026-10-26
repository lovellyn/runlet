import asyncio
import shutil

import pytest

from runlet.errors import SandboxError
from runlet.files import Files
from runlet.workspaces import Workspaces, normalize_repo_url

from .conftest import git


async def test_reuse_preserves_edits_and_never_pushes(sandbox, local_repo):
    workspaces, jobs, files = sandbox
    bare, _ = local_repo
    original_tip = git(bare, "rev-parse", "main")
    created = await workspaces.create(str(bare), "main", "project")
    await files.write("project", "README.md", "uncommitted edit\n")
    await files.write("project", "untracked.txt", "new file")
    reused = await workspaces.create(str(bare), "main", "project")
    assert created["created"] is True
    assert reused["created"] is False
    assert reused["dirty"] is True
    assert (await files.read("project", "README.md"))["content"] == "uncommitted edit\n"
    assert git(bare, "rev-parse", "main") == original_tip
    assert len(workspaces.items) == 1
    command = await jobs.start("project", "python -m pip --version")
    assert command["state"] == "completed"
    assert str(workspaces.get("project").runtime / "venv") in command["output"]


async def test_recovery_from_explicit_push_after_filesystem_loss(sandbox, local_repo, settings):
    workspaces, jobs, files = sandbox
    bare, _ = local_repo
    await workspaces.create(str(bare), "main", "project")
    await files.write("project", "progress.md", "Finished parsing; next: summarize.\n")
    pushed = await jobs.start(
        "project",
        "git -c user.name=Agent -c user.email=agent@example.invalid add progress.md && "
        "git -c user.name=Agent -c user.email=agent@example.invalid commit -m Progress && "
        "git push origin HEAD:refs/heads/task/report",
    )
    assert pushed["state"] == "completed", pushed
    await files.write("project", "unsaved.txt", "not pushed")
    await jobs.shutdown()
    shutil.rmtree(settings.root)
    recovered = Workspaces(settings)
    with pytest.raises(SandboxError, match="workspace_not_found"):
        await recovered.create(workspace_id="project")
    result = await recovered.create(str(bare), "task/report", "project")
    assert result["branch"] == "task/report"
    assert (await Files(recovered).read("project", "progress.md"))["content"].startswith("Finished")
    assert not (recovered.get("project").work / "unsaved.txt").exists()
    assert result["created"] is True


async def test_independent_clones_and_conflicting_ref(sandbox, local_repo):
    workspaces, _, files = sandbox
    bare, seed = local_repo
    git(seed, "switch", "-c", "other")
    (seed / "README.md").write_text("other branch")
    git(seed, "commit", "-am", "Other branch")
    git(seed, "push", "origin", "other")
    first, second = await asyncio.gather(
        workspaces.create(str(bare), "main", "one"),
        workspaces.create(str(bare), "other", "two"),
    )
    assert first["path"] != second["path"]
    await files.write("one", "README.md", "agent one")
    assert (await files.read("two", "README.md"))["content"] == "other branch"
    with pytest.raises(SandboxError, match="workspace_conflict"):
        await workspaces.create(str(bare), "other", "one")
    assert (await workspaces.info("one"))["branch"] == "main"
    assert (await files.read("one", "README.md"))["content"] == "agent one"


async def test_scratch_reuse_reload_delete_and_validation(sandbox, settings):
    workspaces, _, _ = sandbox
    created = await workspaces.create()
    workspace_id = created["workspace_id"]
    reused = await workspaces.create(workspace_id=workspace_id)
    assert reused["created"] is False
    reloaded = Workspaces(settings)
    assert reloaded.get(workspace_id).work == workspaces.get(workspace_id).work
    with pytest.raises(SandboxError, match="invalid_workspace_id"):
        await workspaces.create(workspace_id="../escape")
    with pytest.raises(SandboxError, match="workspace_not_found"):
        await workspaces.create(workspace_id="missing")
    await workspaces.delete(workspace_id)
    assert not reloaded.get(workspace_id).directory.exists()


async def test_empty_workspace_id_is_not_replaced_with_a_generated_id(sandbox, local_repo):
    workspaces, _, _ = sandbox
    bare, _ = local_repo
    with pytest.raises(SandboxError, match="invalid_workspace_id"):
        await workspaces.create(str(bare), workspace_id="")
    assert not workspaces.items
    assert list(workspaces.root.iterdir()) == []


@pytest.mark.parametrize("ref", ["README.md", "."])
async def test_clone_rejects_file_paths_as_refs_without_retaining_a_workspace(
    sandbox, local_repo, ref
):
    workspaces, _, _ = sandbox
    bare, _ = local_repo
    with pytest.raises(SandboxError, match="setup_failed"):
        await workspaces.create(str(bare), ref, "invalid-ref")
    assert not workspaces.items
    assert list(workspaces.root.iterdir()) == []


@pytest.mark.parametrize("kind", ["tag", "commit"])
async def test_clone_selects_the_requested_tag_or_commit(sandbox, local_repo, kind):
    workspaces, _, files = sandbox
    bare, seed = local_repo
    original = git(seed, "rev-parse", "HEAD")
    git(seed, "tag", "initial")
    (seed / "README.md").write_text("newer revision\n")
    git(seed, "commit", "-am", "Newer revision")
    git(seed, "push", "origin", "main", "--tags")
    result = await workspaces.create(str(bare), "initial" if kind == "tag" else original)
    assert result["head_commit"] == original
    assert result["branch"] is None
    assert (await files.read(result["workspace_id"], "README.md"))["content"] == "original\n"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com.evil.invalid/org/repo",
        "https://token@github.com/org/repo",
        "http://github.com/org/repo",
        "file:///tmp/repo",
        "https://github.com/org/repo?token=secret",
        "https://github.com/org/..",
    ],
)
def test_rejects_non_github_and_credential_urls(url):
    with pytest.raises(SandboxError, match="invalid_repository"):
        normalize_repo_url(url)


def test_normalizes_github_url():
    assert normalize_repo_url("https://github.com/Example/Project/") == (
        "https://github.com/example/project.git"
    )
