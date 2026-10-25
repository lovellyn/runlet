# Contributing

Use Linux and Python 3.12+. Install Bash, Git, and Python venv/ensurepip support.
Tests use real processes and /proc; a restricted Python runner without /proc
cannot exercise the process supervisor.

~~~bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e '.[dev]'
python -m ruff check src tests examples scripts
python -m ruff format --check src tests examples scripts
python -m pytest -q
python -m build
~~~

Tests use temporary directories, local bare Git repositories, and the official
MCP 2.x client. They require no GitHub credentials. Keep tests for changes to
process lifetime, authentication, concurrent operations, and persistence.

Build and exercise the container when Docker is available:

~~~bash
docker build --tag runlet:test .
python scripts/smoke_container.py --image runlet:test
~~~

The container smoke test checks matching server/job identities, authentication,
MCP tools, file exports/downloads, workspace writes, parallel jobs, the development toolchain,
and persistence after a restart. It creates and removes its own container and volume.
Optional --expected-uid and --expected-gid flags check deployment-specific IDs.
The separate deploy/huggingface/smoke.sh check builds and exercises that wrapper.

## Dependency updates

requirements.txt pins production dependencies for Docker and CI. pyproject.toml
declares the supported direct dependency ranges. There is no required additional
package manager.

To refresh the production pins, use a disposable environment:

~~~bash
python3 -m venv /tmp/runlet-resolution
/tmp/runlet-resolution/bin/python -m pip install --upgrade pip
/tmp/runlet-resolution/bin/python -m pip install .
/tmp/runlet-resolution/bin/python -m pip freeze --exclude runlet > requirements.txt
~~~

Review the diff and run the full checks on a supported Linux environment before
committing. Regenerate in a fresh environment to avoid exporting unrelated tools.
The pins currently target Linux; regenerate and test deliberately when changing
Python support. Ubuntu OS packages receive the versions available at image build
time. NODE_VERSION pins the official Node runtime copied into the image.

## Pull requests

Explain the behavior being changed and how it was validated. Keep unrelated
refactors separate. Do not add compatibility layers for older MCP SDKs.
Never commit .env, credentials, runtime workspaces, venvs, or build outputs.

The GitHub workflow runs Python checks, package builds, and a real Docker smoke
test. Published stable releases also build linux/amd64 and linux/arm64 images for
GHCR. See [release instructions](docs/releases.md).

Contributions are provided under the repository's Apache License 2.0.
