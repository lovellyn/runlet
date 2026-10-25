"""Reject release tags that do not match the source package's stable version."""

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def validate(tag: str, version: str) -> None:
    if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag):
        raise ValueError("Use a stable release tag vMAJOR.MINOR.PATCH")
    if tag != f"v{version}":
        raise ValueError(f"Release tag {tag!r} does not match package version {version!r}")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/check_release.py vMAJOR.MINOR.PATCH")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    try:
        validate(sys.argv[1], version)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"Release {sys.argv[1]} matches pyproject.toml")


if __name__ == "__main__":
    main()
