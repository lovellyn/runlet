import shutil
import subprocess
from dataclasses import replace

import pytest

from runlet.config import Settings
from runlet.workspaces import Workspaces


@pytest.fixture
def settings(settings):
    return replace(
        settings,
        git_user_name='Sandbox "Agent" Bot',
        git_user_email="sandbox@example.invalid",
    )


def workspace_git(workspace, *args):
    result = subprocess.run(
        ["git", "-C", str(workspace.work), *args],
        env=workspace.environment(),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_identity_settings_from_server_environment(monkeypatch):
    monkeypatch.setenv("SANDBOX_TOKEN", "test-token")
    monkeypatch.setenv("GIT_USER_NAME", "Server Agent")
    monkeypatch.setenv("GIT_USER_EMAIL", "server@example.invalid")
    settings = Settings.from_env()
    assert settings.git_user_name == "Server Agent"
    assert settings.git_user_email == "server@example.invalid"
    monkeypatch.delenv("GIT_USER_NAME")
    monkeypatch.delenv("GIT_USER_EMAIL")
    settings = Settings.from_env()
    assert settings.git_user_name == settings.git_user_email == ""


async def test_commits_use_defaults_and_respect_agent_overrides(sandbox):
    workspaces, jobs, _ = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    log = "git log -1 --format='%an%n%ae%n%cn%n%ce'"

    async def commit(command, expected, env=None):
        result = await jobs.start(
            workspace_id,
            f"{command} && git commit --quiet --allow-empty -m Test && {log}",
            env=env,
        )
        assert result["state"] == "completed", result
        assert result["exit_code"] == 0, result
        assert result["output"].strip().splitlines()[-4:] == expected

    await commit(
        "git init --quiet",
        ['Sandbox "Agent" Bot', "sandbox@example.invalid"] * 2,
    )
    await commit(
        "git config --global user.name Global && "
        "git config --global user.email global@example.invalid",
        ["Global", "global@example.invalid"] * 2,
    )
    await commit(
        "git config user.name Project && git config user.email project@example.invalid",
        ["Project", "project@example.invalid"] * 2,
    )
    result = await jobs.start(
        workspace_id,
        "git -c user.name=Option -c user.email=option@example.invalid "
        f"commit --quiet --allow-empty -m Override && {log}",
    )
    assert result["exit_code"] == 0, result
    assert result["output"].strip().splitlines() == ["Option", "option@example.invalid"] * 2
    await commit(
        "git status --porcelain",
        ["Author", "author@example.invalid", "Committer", "committer@example.invalid"],
        env={
            "GIT_AUTHOR_NAME": "Author",
            "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_COMMITTER_NAME": "Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.invalid",
        },
    )


async def test_restart_and_reuse_preserve_independent_workspace_configs(sandbox, settings):
    workspaces, _, files = sandbox
    workspace_id = (await workspaces.create())["workspace_id"]
    workspace = workspaces.get(workspace_id)
    workspace_git(workspace, "init", "--quiet")
    workspace_git(workspace, "config", "--global", "user.name", "Agent Choice")
    workspace_git(workspace, "config", "user.email", "project@example.invalid")
    await files.write(workspace_id, "progress.txt", "uncommitted progress\n")
    original_config = (workspace.work / ".git" / "config").read_bytes()
    original_global = (workspace.runtime / "home" / ".gitconfig").read_bytes()

    reloaded = Workspaces(
        replace(settings, git_user_name="Updated Agent", git_user_email="updated@example.invalid")
    )
    assert (await reloaded.create(workspace_id=workspace_id))["created"] is False
    restored = reloaded.get(workspace_id)
    assert workspace_git(restored, "config", "user.name") == "Agent Choice"
    assert workspace_git(restored, "config", "user.email") == "project@example.invalid"
    assert workspace_git(restored, "config", "--global", "user.email") == "sandbox@example.invalid"
    assert (restored.runtime / "home" / ".gitconfig").read_bytes() == original_global
    assert (restored.work / ".git" / "config").read_bytes() == original_config
    assert (restored.work / "progress.txt").read_text() == "uncommitted progress\n"
    assert "GIT_CONFIG_SYSTEM" not in restored.environment()

    fresh_id = (await reloaded.create())["workspace_id"]
    fresh = reloaded.get(fresh_id)
    assert workspace_git(fresh, "config", "--global", "user.name") == "Updated Agent"
    assert workspace_git(fresh, "config", "--global", "user.email") == "updated@example.invalid"
    workspace_git(fresh, "config", "--global", "user.name", "Other Agent")
    assert workspace_git(restored, "config", "--global", "user.name") == "Agent Choice"


async def test_unconfigured_existing_workspace_is_never_backfilled(settings):
    unconfigured = Workspaces(replace(settings, git_user_name="", git_user_email=""))
    workspace_id = (await unconfigured.create())["workspace_id"]
    workspace = unconfigured.get(workspace_id)
    config = workspace.runtime / "home" / ".gitconfig"
    assert not config.exists()

    configured = Workspaces(settings)
    assert (await configured.create(workspace_id=workspace_id))["created"] is False
    assert not config.exists()


async def test_recreated_clone_gets_server_identity_after_filesystem_loss(settings, local_repo):
    bare, _ = local_repo
    workspaces = Workspaces(settings)
    await workspaces.create(str(bare), "main", "project")
    workspace_git(workspaces.get("project"), "config", "--global", "user.name", "Agent Choice")

    shutil.rmtree(settings.root)
    recovered = Workspaces(settings)
    await recovered.create(str(bare), "main", "project")
    workspace = recovered.get("project")
    assert workspace_git(workspace, "config", "--global", "user.name") == 'Sandbox "Agent" Bot'
    assert workspace_git(workspace, "config", "--global", "user.email") == "sandbox@example.invalid"
