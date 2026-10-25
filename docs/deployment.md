# Deployment

## Docker

The root Dockerfile builds the same image for local Docker, a VM, or a container
hosting service. The provided Compose file builds locally and mounts /data.
A published GHCR image can also be run directly; see the root README.

The image uses Ubuntu 24.04, Node 24, and Ubuntu's Python 3.12. Application
dependencies are installed in /opt/runlet. Runtime jobs use their own
workspace venvs. The shared image inherits Ubuntu's default root user and sets
HOME=/root. It does not create a runtime account or set a USER directive.
Hosting-specific identity requirements belong in deployment wrappers.

The default port is 8000. PORT changes the server listener and the health probe;
change the host/container port mapping and hosting service's port setting too.

## Persistent files

Mount a named volume at /data, the image's SANDBOX_ROOT. A replacement container
can load existing workspace registrations and reuse files and environments.
Jobs and their history are process-local and end at server shutdown.

For a bind mount, create a directory writable by the selected runtime account.
Inspect the built image's IDs before assigning ownership on the host:

~~~bash
docker run --rm --entrypoint id runlet:local
~~~

Deployments can use Docker's --user option or a derived image to choose another
identity. Ensure both HOME and SANDBOX_ROOT are writable by that identity.
The image does not run a root startup script to change host permissions. If the
runtime IDs change when upgrading an image, migrate existing volume ownership.
Persistent storage is not an automatic backup.

Run exactly one instance against a data directory. Multiple Uvicorn workers or
replicas do not share job records, reservations, and per-file locks.

## Reverse proxies

Bind to loopback for local use. For access from another machine, terminate HTTPS
at a reverse proxy and pass the Host, X-Sandbox-Token, and MCP headers through.
Disable response buffering for MCP streaming responses and allow long requests.

Set ALLOWED_HOSTS to the exact public host (include the port when nonstandard).
Set ALLOWED_ORIGINS when browser clients send an Origin header. Localhost
defaults remain enabled. CORS is not an authentication mechanism; applications
requiring cross-origin browser access should configure it at their gateway.

The public /healthz endpoint checks HTTP liveness. It does not execute a job or
validate that external Git services are reachable.

No proxy or tunnel service is required by the application. A gateway can use its
own Authorization header independently from the sandbox's X-Sandbox-Token.

To enable [file exports](exports.md), set PUBLIC_BASE_URL to the reachable server
URL without /mcp, and forward GET/HEAD /downloads/ routes, including Range headers.
Keep these responses streaming, and redact capability paths from proxy access
logs. Upstream authentication still applies to downloads; a private gateway must
permit the intended downloader's request. Export links end at server restart.

## Development servers

An agent can run a frontend dev server while using other jobs and file tools.
The server is bounded by that job's timeout and is stopped when its job ends.
A browser running inside the container can reach it through localhost.
External previews require an explicit Docker port mapping or a configured proxy;
this project does not automatically publish development-server ports.

## Hugging Face

Use the two-file [deployment example](../deploy/huggingface/README.md). It pulls
the published GHCR image, configures the platform's numeric runtime identity, and
sets the app port. The core server contains no Hugging Face-specific environment detection.
