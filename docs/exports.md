# File exports and downloads

Set PUBLIC_BASE_URL to the server's reachable HTTP(S) URL, without /mcp. A proxy
prefix is supported, for example https://sandbox.example.com/agent. The proxy
must forward that prefix's /downloads/ routes to the server's /downloads/ routes.
For local Docker/Compose, use http://localhost:8000 when the downloader is on
that machine. A remote harness needs an address it can reach.

Exports are disabled when PUBLIC_BASE_URL is unset. Other file tools still work.
No additional package or storage service is needed.

## Create and download

Call file_export through authenticated MCP:

~~~json
{"workspace_id": "project-task", "path": "dist/result.zip"}
~~~

The result contains export_id, filename, mime_type, size_bytes, sha256,
expires_at (UTC), and download_url. File bytes stay out of the MCP response.
Download the URL as a normal HTTP file; X-Sandbox-Token is not required:

~~~bash
curl --fail "$download_url" --output result.zip
~~~

GET streams the snapshot with Content-Disposition: attachment. HEAD returns
metadata without the body. Byte ranges support resuming downloads. Responses
use Cache-Control: no-store, Referrer-Policy: no-referrer, and nosniff. The ETag
is the snapshot's SHA-256 digest.

The agent harness can stream the bytes into its own workspace, verify sha256,
and register the result with its host's file/download interface. Returning this
URL does not itself create a ChatGPT attachment or a sandbox: link. Presentation
and any durable artifact storage belong to the harness or host application.

## Access and routing

Anyone holding a download URL can retrieve that one snapshot until expiry or
server restart. URLs contain independent random 256-bit capabilities, never the
server's main token. Unknown and expired capabilities return 404. A download
capability cannot authorize MCP operations or choose another filesystem path.

Use HTTPS for remote access. Treat download URLs as temporary bearer credentials.
The bundled server launcher disables HTTP access logs so capabilities are not
recorded there. Configure proxies and alternative launchers to omit or redact
download URLs too.

Any upstream gateway authentication still applies. A private deployment may
require the downloader to supply its gateway credential; the link cannot bypass
that gateway. Do not put the sandbox's main token in a download URL. A browser
download works only when the gateway permits that browser's request.

## Snapshots, limits, and cleanup

The server copies a regular file into a private cache under SANDBOX_ROOT/exports,
using bounded chunks and calculating its checksum. Native writes, edits, and
snapshot creation share the same per-file queue; different files can be exported
concurrently. Absolute paths, traversal, escaping symlinks, directories, and
special files are rejected. Use exec to ZIP a directory before exporting it.

Shell commands do not join native file locks. Stop shell writers before export.
If the source size or timestamps change during the copy, file_export fails with
file_changed; retry after the writer finishes. A successful snapshot is independent
of later source edits or workspace deletion.

The default limit is 256 MiB per file and 16 retained exports, including copies
in progress. Each export uses its own snapshot, even for repeated exports of the
same source. MAX_EXPORT_BYTES and MAX_EXPORTS bound the cache size. New exports
fail with export_limit when capacity is full; they never evict valid links.

Links expire 15 minutes after the copy completes by default. Expiry rejects new
requests immediately. A download admitted before expiry may finish; cleanup waits
for it to release the snapshot. Expired idle snapshots are removed within 30
seconds, and before new exports are admitted. The URL can be used more than once
before expiry, including for HEAD and range requests.

Exports and capabilities are process-local. Clean shutdown removes snapshots;
startup removes abandoned cache files after a crash. Persisting SANDBOX_ROOT
retains workspaces, but does not preserve export links. Run exactly one server
process per data directory.
