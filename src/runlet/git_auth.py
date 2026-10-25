"""HTTPS Git authentication without putting credentials in repository URLs."""

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit


def askpass() -> None:
    prompt = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    target = re.search(r"https://[^'\s]+", prompt)
    if target is None or urlsplit(target.group()).hostname != "github.com":
        return
    if "username" in prompt:
        print("x-access-token")
    elif "password" in prompt:
        print(os.getenv("GITHUB_TOKEN", ""))


def git_environment() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    helper = Path(sys.executable).parent / "runlet-askpass"
    if helper.exists():
        env["GIT_ASKPASS"] = str(helper)
    if token := os.getenv("GITHUB_TOKEN"):
        # All connected agents are trusted. This credential enables their explicit pushes.
        env["GITHUB_TOKEN"] = token
    return env
