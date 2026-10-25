# Security

Runlet runs code supplied by trusted agents. Authentication grants access
to that execution environment.

All jobs and the server share one OS identity. Workspace paths, Python venvs,
and file-tool path checks organize work; they do not isolate hostile code.
A shell command can reach other files and processes accessible to that identity.
Use separate containers or VMs and separate credentials for different owners.

The shared image inherits Ubuntu's default root user. Deployments choose their
runtime identity and permissions; the server supports root and non-root accounts.
Root agents can modify the container's server files and install system packages.
Keep the Docker socket, privileged devices, and sensitive host directories outside
the container. Apply container CPU, memory, and network limits according to your workload.

SANDBOX_TOKEN is mandatory. The / and /healthz routes are public; MCP requests
require exactly one valid X-Sandbox-Token header. The app does not implement
OAuth or validate an upstream gateway's Authorization token. Terminate TLS at
your proxy and configure ALLOWED_HOSTS and, for browser clients, ALLOWED_ORIGINS.

Optional file_export creates a temporary download capability for one snapshot.
GET/HEAD /downloads/<capability> validate that capability instead of requiring
X-Sandbox-Token. Anyone holding the link can download its file until expiry or
restart. Keep PUBLIC_BASE_URL unset to disable exports. Treat links as bearer
credentials, use HTTPS remotely, and redact download paths from gateway logs.
The bundled launcher disables HTTP access logs. Snapshots survive workspace
deletion until expiry; do not interpret deletion as revoking an existing link.
See [exports](docs/exports.md) for limits and cleanup behavior.

GITHUB_TOKEN is deliberately available to agent jobs. Other server secrets are
not automatically inherited, but a shared OS user is not a secret boundary.
Use narrowly scoped credentials and keep them out of source control.

Job supervision adopts and reaps ordinary detached descendants. It is lifecycle
management for cooperative workloads, not containment against malicious code
or a fork bomb. Use OS/container resource controls as well.

Only the latest published version receives fixes. No response-time commitment
is offered. Report suspected vulnerabilities through the repository's private
GitHub vulnerability reporting feature when enabled. Otherwise contact the
maintainer privately through their GitHub profile before disclosing details.
Do not put secrets or exploit details in public issues.
