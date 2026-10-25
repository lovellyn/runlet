# Configuration

The server reads environment variables at startup. It does not load .env itself.
The example .env file works with Docker's --env-file, Compose, or Bash source.

| Variable | Default | Meaning |
| --- | --- | --- |
| SANDBOX_TOKEN | Required | Shared key sent in X-Sandbox-Token |
| GITHUB_TOKEN | Unset | GitHub HTTPS credential available to trusted jobs |
| GIT_USER_NAME | Unset | Initial Git name for new workspaces |
| GIT_USER_EMAIL | Unset | Initial Git email for new workspaces |
| SANDBOX_ROOT | ./var; /data in the image | Runtime data directory |
| PORT | 8000 | HTTP listener port |
| MAX_CONCURRENT_JOBS | 2 | Running-job limit across all workspaces |
| MAX_WORKSPACES | 32 | Workspace count limit |
| MAX_JOB_RECORDS | 256 | Retained job records |
| DEFAULT_JOB_TIMEOUT | 300 | Default job timeout, seconds |
| MAX_JOB_TIMEOUT | 3600 | Maximum job timeout, seconds |
| MAX_OUTPUT_BYTES | 1048576 | Retained merged output per job, bytes |
| RESPONSE_BYTES | 65536 | Maximum job output per response, bytes |
| MAX_FILE_BYTES | 1048576 | Maximum read/write chunk and complete text-edit size |
| SETUP_TIMEOUT | 180 | Timeout per clone/checkout/venv setup command, seconds |
| PUBLIC_BASE_URL | Unset (exports disabled) | Reachable HTTP(S) server URL or proxy prefix, without /mcp |
| EXPORT_TTL_SECONDS | 900 | Download-link lifetime after the snapshot completes, seconds |
| MAX_EXPORT_BYTES | 268435456 | Maximum snapshot size, bytes (256 MiB) |
| MAX_EXPORTS | 16 | Maximum retained exports plus copies in progress |
| ALLOWED_HOSTS | Loopback hosts | Additional allowed hosts, comma-separated |
| ALLOWED_ORIGINS | Local HTTP origins | Additional browser origins, comma-separated |

Local host defaults include localhost, 127.0.0.1, and [::1], with optional ports.
Local origin defaults include HTTP localhost and 127.0.0.1, with optional ports.
Extra entries are added to those defaults. Use exact public hostnames and origins
instead of disabling DNS rebinding protection.

No hosting-provider environment variables are read. Set the public host explicitly:

~~~bash
ALLOWED_HOSTS=sandbox.example.com
ALLOWED_ORIGINS=https://sandbox.example.com
~~~

SANDBOX_TOKEN must be nonempty and have no leading or trailing whitespace.
Limits must be positive; the default timeout must not exceed the maximum, and
RESPONSE_BYTES must not exceed MAX_OUTPUT_BYTES.

Image preview limits are fixed: 10 MiB input, 25 megapixels, 2 MiB output, up to
4096 pixels on the long edge, and two concurrent decoders. The default preview
dimension is 1600 pixels. PTY dimensions range from 1 to 1000 rows/columns.
Each stdin write accepts at most 64 KiB of UTF-8.

PUBLIC_BASE_URL must have no credentials, query, fragment, or whitespace. It is
configured explicitly rather than derived from request headers. See
[file exports](exports.md) for routing, link access, and cleanup semantics.

Run one process per data directory. More parallel jobs use MAX_CONCURRENT_JOBS;
additional Uvicorn workers do not coordinate this server's in-memory state.
