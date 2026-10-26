# Runlet

A Linux MCP server that gives trusted development agents workspaces, parallel
shell jobs, interactive terminals, file editing, and image previews.

Source repository: [lovellyn/runlet](https://github.com/lovellyn/runlet).

Run it with Docker on your own machine, a server, or a container hosting service.
Workspaces have separate Python environments, home directories, and temporary
directories. Agents own their Git commits, pushes, and branch changes.

**Execution is for trusted agents.** All workspaces share one OS identity.
Use a separate container or VM for each trust boundary. See [SECURITY.md](SECURITY.md).
The shared image inherits Ubuntu's root user; deployment wrappers can select
another runtime account.

## Features

- Scratch workspaces and optional GitHub HTTPS clones.
- Parallel jobs and file operations within each workspace.
- Background jobs with incremental output, cancellation, timeouts, and reconnects.
- PTYs with stdin, control keys, and explicit terminal resizing.
- Per-file queued, atomic writes and exact text edits.
- Native MCP image previews for PNG, JPEG, WebP, and GIF.
- Expiring download links to file snapshots, with streamed and resumable transfers.
- Python, Node.js/npm, Git, build tools, Chromium runtime libraries, and FFmpeg.

Uses the official Python MCP SDK 2.x and Streamable HTTP. Clients send
`X-Sandbox-Token` on every MCP request; there is no OAuth sign-in flow.

## Quick start with Docker

Create a local environment file and generate a token:

~~~bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
~~~

Set `SANDBOX_TOKEN` in .env to the generated value, then run:

~~~bash
docker compose up --build -d
curl --fail http://127.0.0.1:8000/healthz
~~~

The MCP endpoint is [http://localhost:8000/mcp](http://localhost:8000/mcp).
Compose binds to loopback and keeps workspace files in a named Docker volume.
`docker compose down` keeps that volume; `docker compose down --volumes` deletes it.

Run the latest stable image with:

~~~bash
docker run -d --name runlet \
  --env-file .env \
  --publish 127.0.0.1:8000:8000 \
  --mount type=volume,src=runlet-data,dst=/data \
  ghcr.io/lovellyn/runlet:latest
~~~

## Run from source

Requires Linux, Python 3.12+, Bash, Git, and Python venv/ensurepip support.
On Ubuntu, the OS packages are python3, python3-venv, and git.
Docker supplies the additional development tools listed above.

~~~bash
git clone https://github.com/lovellyn/runlet.git
cd runlet
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
cp .env.example .env
~~~

Set the token in .env, then export its values and start the server:

~~~bash
set -a
source .env
set +a
runlet
~~~

The server does not load .env automatically. Run exactly one server process per
data directory: jobs, reservations, and file locks are held in that process.

In a second shell using the same environment, run:

~~~bash
python examples/client.py
~~~

The client creates a scratch workspace, runs Python, prints the output, and
deletes its temporary workspace.

## Documentation

- [MCP tools and concurrency](docs/tools.md)
- [Configuration](docs/configuration.md)
- [File exports and downloads](docs/exports.md)
- [Runtime tools and project dependencies](docs/runtime.md)
- [Workspaces, Git ownership, and recovery](docs/workspaces.md)
- [Docker deployment and reverse proxies](docs/deployment.md)
- [Hugging Face Docker Space](deploy/huggingface/README.md)
- [Contributing and validation](CONTRIBUTING.md)
- [GitHub releases and container publishing](docs/releases.md)

## Persistence

Mount `SANDBOX_ROOT` to retain workspaces and their dependency environments across
container replacement. The image uses /data; source launches default to ./var.
Running jobs and their records end when the server stops.
Export links and their temporary snapshots also end at server shutdown/restart.

The server does not create checkpoints or push Git state automatically.
Explicitly push commits or back up the data volume before losing its filesystem.

## License

Copyright 2026 Runlet contributors.

Licensed under [Apache-2.0](LICENSE). Container images also contain third-party
software under its own licenses. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
