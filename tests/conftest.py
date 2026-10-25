import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio

from runlet.config import Settings
from runlet.files import Files
from runlet.jobs import Jobs
from runlet.workspaces import Workspaces


@pytest.fixture
def settings(tmp_path):
    return Settings(
        token="test-token-which-is-not-a-real-secret",
        root=tmp_path / "sandbox",
        setup_timeout=30,
        default_timeout=10,
        max_timeout=30,
    )


@pytest_asyncio.fixture
async def sandbox(settings):
    workspaces = Workspaces(settings)
    jobs = Jobs(settings, workspaces, "test-boot")
    try:
        yield workspaces, jobs, Files(workspaces)
    finally:
        await jobs.shutdown()


def git(directory, *args):
    result = subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def local_repo(tmp_path, monkeypatch):
    """Use actual local Git transport, without requiring a GitHub account or credentials."""
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "clone", str(bare), str(seed)], check=True, capture_output=True)
    git(seed, "config", "user.name", "Test Agent")
    git(seed, "config", "user.email", "test@example.invalid")
    (seed / "README.md").write_text("original\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-m", "Initial project")
    git(seed, "push", "origin", "main")
    import runlet.workspaces as module

    original = module.normalize_repo_url

    def local_or_github(value):
        return str(Path(value).resolve()) if Path(value).is_dir() else original(value)

    monkeypatch.setattr(module, "normalize_repo_url", local_or_github)
    return bare, seed


@pytest.fixture
def tiny_settings(settings):
    return replace(settings, max_output_bytes=128, response_bytes=32)
