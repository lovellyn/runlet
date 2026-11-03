# File uploads

Uploads use MCP for small metadata and HTTP PUT for raw file bytes. This keeps
large binaries out of model context and avoids base64 encoding. Small generated
text files can still use file_write.

Set PUBLIC_BASE_URL to the reachable server URL, without /mcp. Forward that URL's
PUT /uploads/ route to Runlet, including any configured proxy prefix. Uploads are
disabled when PUBLIC_BASE_URL is unset.

## Harness workflow

1. Hash the local file with SHA-256 and count its bytes.
2. Call file_upload through authenticated MCP:
   ~~~json
   {
     "workspace_id": "project-task",
     "path": "inputs/project.zip",
     "size_bytes": 12345,
     "sha256": "THE_FILES_64_CHARACTER_HEX_SHA256",
     "overwrite": false
   }
   ~~~
3. Stream the original bytes to the returned upload_url with HTTP PUT. Do not
   encode them as base64 or wrap them in JSON/multipart:
   ~~~bash
   curl --fail --user-agent 'Runlet-Client/0.4.0' \
     --upload-file project.zip "$upload_url"
   ~~~
4. A successful PUT returns HTTP 201 and path, size_bytes, and sha256. The agent
   can now read, extract, or process the workspace file.

The ticket result includes path, size_bytes, sha256, upload_url, and expires_at.
No file bytes enter the MCP call or response. The harness must have access to the
source file and an HTTP client; creating a ticket does not itself read a user
attachment or a path on another computer.

The reusable upload_file helper in examples/upload.py hashes and streams a local
file with bounded memory. Run it from the repository root with the same
SANDBOX_URL, SANDBOX_TOKEN, and optional GATEWAY_TOKEN as examples/client.py:

~~~bash
python -m examples.upload WORKSPACE_ID ./project.zip inputs/project.zip
~~~

Add --overwrite only when replacement is intended. It never prints the upload URL
or sends X-Sandbox-Token on the PUT connection. It does not follow redirects with
the gateway credential. Keep the source file unchanged during hashing/upload.

## Capabilities and atomic writes

An upload URL is a random 256-bit bearer capability bound to one workspace,
destination, byte count, checksum, and overwrite policy. It cannot read files,
authorize MCP, or choose a different path. Only PUT is accepted. Host and Origin
checks also apply.

A PUT attempt consumes the ticket, including attempts with invalid data. Replays
and expired/unknown tickets return 404. Rejected Host/Origin requests do not
consume it. A ticket expires after 15 minutes by default; an admitted transfer
has its own UPLOAD_TIMEOUT, including publication. Tickets end at server restart
or workspace deletion and cannot target a recreated workspace with the same ID.

Bytes stream into a private temporary file. Actual received bytes are counted
even when Content-Length is absent or incorrect, and SHA-256 must match before
publication. Compressed HTTP request bodies are rejected; upload a compressed
file as ordinary raw bytes instead.

Existing destinations require overwrite=true at ticket creation and publication.
New-file publication atomically refuses an entry created while receiving. Regular
file modes are retained on replacement; new files use 0644. Paths stay inside the
workspace; the final parent walk rejects swapped symlinks and special files.

Only publication joins the existing per-file mutation queue. Slow network
transfers do not lock the destination against native edits, and different files
can upload concurrently. An overwrite=true upload replaces the file as it exists
at publication; coordinate other writers when that matters. Shell commands do
not join the native queue.

Active uploads reserve workspace lifetime; deletion reports workspace_busy.
Pending tickets do not prevent deletion. Disconnects, validation errors,
cancellation, and timeouts remove temporary files without publishing partial
content. If a connection is lost during atomic publication, a complete file may
already have been installed: inspect its size/checksum before retrying. The client
must obtain a new ticket for another PUT attempt.

## Limits, gateways, and cleanup

Defaults are 256 MiB per file, eight pending plus active uploads, 15-minute ticket
expiry, and a 300-second total transfer timeout. Configure MAX_UPLOAD_BYTES,
MAX_UPLOADS, UPLOAD_TTL_SECONDS, and UPLOAD_TIMEOUT as needed. Completed files
belong to the workspace and follow its normal persistence/deletion rules.

Pending tickets occupy bounded memory and are purged lazily on subsequent ticket
or PUT requests. Partial files are removed when the request ends; startup removes
abandoned upload staging files after a crash. There is no separate storage
service or new upload dependency.

Use HTTPS for remote access. Omit or redact /uploads/ capability paths from proxy
logs. The sandbox's main token is never part of the URL. Any upstream gateway
authentication still applies to PUT; pass that credential separately when needed.

Configure proxy body-size and request-timeout limits for the intended file sizes,
and disable request buffering when supported. A proxy may reject a file below
Runlet's configured limit. The MCP endpoint retains its independent 4 MiB request
limit. Browser harnesses also need their gateway's CORS policy to permit PUT.

Uploads are one raw HTTP request per file. Package directories as archives and
extract them explicitly with exec. Interrupted transfers restart from byte zero.
