# MCP tools

| Tool | Purpose |
| --- | --- |
| server_info | Boot ID, execution limits, and recovery contract |
| workspace_create | Create a scratch workspace or clone GitHub repo/ref; reuse a local ID |
| workspace_list | List local workspaces and checkout state |
| workspace_get | Inspect a workspace and retained jobs |
| workspace_delete | Explicitly remove an idle workspace, including unpushed files |
| exec | Start a shell command; return its result or background job ID |
| job_status | Incremental output, completion status, and optional long polling |
| job_cancel | Stop a job and its descendants, including detached sessions |
| job_write_stdin | Send UTF-8 input or pipe EOF to a running job, with optional input-offset checking |
| job_resize | Resize a running PTY and notify its foreground process |
| image_read | Return a validated screenshot/image as native MCP image content |
| file_read | Read bounded UTF-8 or base64 file chunks |
| file_write | Atomically create or replace UTF-8 or base64 files |
| file_edit | Replace exact text in an existing file without resending the whole file |
| file_export | Snapshot one file and return metadata plus an expiring HTTP download URL |

### Export a finished file

~~~json
{"workspace_id": "project-task", "path": "dist/result.zip"}
~~~

file_export requires PUBLIC_BASE_URL and returns export_id, filename, mime_type,
size_bytes, sha256, expires_at, and download_url. The URL grants access to only
that snapshot without X-Sandbox-Token; it expires after EXPORT_TTL_SECONDS or
server restart. The HTTP route supports GET, HEAD, and byte ranges. Creation
shares the native per-file queue, and exports of different files run in parallel.
The snapshot survives source edits and workspace deletion. Stop shell writers
before exporting; package directories with shell tools first.

No file bytes are embedded in the MCP response. See [file exports](exports.md)
for limits, deployment routing, and harness integration.

### Open and reuse a workspace

Tool arguments:

~~~json
{
  "repo_url": "https://github.com/your-org/your-project.git",
  "ref": "main",
  "workspace_id": "project-task"
}
~~~

repo_url accepts HTTPS github.com owner/repository URLs without embedded
credentials. ref can be a branch, tag, or commit SHA. If omitted, a new clone
uses the remote's default branch. File paths are rejected as refs. Omit
workspace_id to generate an ID; an explicit empty ID is invalid.

Calling workspace_create with an existing ID returns the current checkout.
Uncommitted edits and untracked files stay in place. Conflicting supplied
repository/ref arguments return an error; they never trigger a pull, reset,
or checkout. Inspect the workspace and use exec for any desired Git operation.

Calling workspace_create with no arguments creates a new scratch workspace.
A supplied ID that is missing requires repo_url: ID-only reopening reports
workspace_not_found instead of silently creating an empty directory.

### Execute and retrieve output

~~~json
{
  "workspace_id": "project-task",
  "command": "python analyze.py",
  "cwd": ".",
  "wait_seconds": 2,
  "timeout": 300,
  "request_id": "optional-client-generated-id"
}
~~~

Every exec starts a fresh Bash shell. Files and workspace-local packages persist
while the workspace exists; shell variables and cwd changes do not carry over.
Use cwd and env on each command as needed.

Short commands return their output directly. Longer commands return a job_id.
Call job_status with that job_id and the previous next_cursor to receive new
output. wait_seconds on job_status waits up to 20 seconds for output or completion.
Continue fetching while has_more is true, even after the command finishes.

stdout and stderr are merged. Cursors are byte offsets. Buffers are bounded;
truncated signals that earlier output has fallen out of the retained buffer.
Use command redirection or tee when a full log should become a project file.
Use base64 for exact file transfer when UTF-8 chunks could split a character.

Jobs continue across MCP disconnects. They stop on server restart,
cancellation, timeout, or normal completion. Descendant processes are stopped
when a job ends; this API is not a detached daemon manager.

Cleanup includes background process groups and detached child sessions. Each
job has a Linux child supervisor that adopts orphaned descendants and reaps
them before the job finishes. The runtime must expose /proc. Cleanup and final
output draining have deadlines, so an inherited output pipe cannot keep a job
running indefinitely after timeout. Failed workspace setup also removes partial
files in a worker thread, allowing other tool calls to keep running.

### Interactive jobs

Every job starts with writable stdin. Pipe-mode output is suitable for scripts
and structured logs. For CLI prompts, REPLs or full-screen programs, set
tty=true when starting a job:

~~~json
{
  "workspace_id": "project-task",
  "command": "python -u -c \"name=input('Name: '); print('Hello', name)\"",
  "tty": true,
  "rows": 24,
  "cols": 100,
  "wait_seconds": 0,
  "timeout": 300
}
~~~

Use the returned job_id with job_write_stdin:

~~~json
{
  "job_id": "returned-job-id",
  "chars": "Ada\n",
  "expected_input_offset": 0,
  "cursor": 0,
  "wait_seconds": 2
}
~~~

Input is sent exactly as supplied; include a newline to submit a terminal line.
Each call accepts up to 64 KiB of UTF-8 and input calls queue per job. For pipe
jobs, close_stdin=true closes stdin after writing and sends real EOF. A PTY
cannot half-close: send "\u0004" for the terminal's Ctrl-D behavior, or use
job_cancel. Send "\u0003" for Ctrl-C. Terminal control-key behavior depends
on the application and its terminal mode.

job_resize accepts job_id, rows, and cols (1–1000 each). PTYs start with
TERM=xterm-256color unless overridden. Their output includes input echo, ANSI
sequences, and CRLF. job_status remains the output-polling tool; its byte cursor
works in both pipe and PTY mode.

Job results report tty, terminal_size, stdin_closed, and input_offset.
The input offset counts UTF-8 bytes accepted for delivery, not bytes consumed
by the application. Supply expected_input_offset to reject stale or replayed
writes. After an interrupted input call, inspect the current offset before
retrying. Backpressure is limited to five seconds; a stdin_blocked error may
follow partial delivery. Invalid output cursors are rejected before input is sent.

Interactive jobs keep running across MCP client disconnects, subject to the
normal job timeout and server lifetime. Reconnect using the same job ID. A shell
started inside one job retains its own cwd and environment while that job is
alive; a separate exec starts a new shell. Cancel or finish terminal jobs when
done so their terminal descriptors and job slots are released.

### Inspect screenshots and images

image_read returns a metadata text block and a native MCP ImageContent block:

~~~json
{
  "workspace_id": "project-task",
  "path": "screenshots/home.png",
  "max_dimension": 1600
}
~~~

Harnesses must preserve the image block and pass it as an image input to the
vision model. The SDK's structured_content field is unset for this tool.
Use file_read when exact original bytes are needed.

PNG, JPEG, WebP and GIF are supported. EXIF orientation is applied and metadata
is stripped. Transparent images keep alpha; animated images show their first
frame. Previews are PNG, or JPEG for JPEG inputs.

| Image limit | Value |
| --- | --- |
| Source file | 10 MiB |
| Source dimensions | 25 megapixels |
| Preview dimensions | Default longest side 1600 pixels; requested maximum 4096 |
| Encoded preview | 2 MiB; reduced further if needed |
| Concurrent image decoding | 2; additional reads queue |

Image operations run alongside jobs and other file tools. Queued image reads
reserve the workspace's lifetime and prevent deletion until they finish.

### Parallel tool calls

Harnesses can issue concurrent tool calls within one workspace or across
workspaces. `workers=1` keeps coordination in one asynchronous server process;
it does not limit the server to one tool call or one shell job.

| Operations | Coordination |
| --- | --- |
| Multiple reads | Run concurrently |
| Native writes/edits to different files | Run concurrently |
| Native writes/edits to the same resolved path | Queue in arrival order; each edit checks the current content after acquiring its lock |
| Multiple shell jobs in one workspace | Run concurrently, subject to the global job limit |
| File tools alongside shell jobs | Run concurrently, including while a dev server is active |
| Workspace creation | Different IDs set up concurrently; same-ID requests queue and validate against the completed checkout |
| Workspace deletion | Rejected with `workspace_busy` while jobs, file operations or metadata inspections are active |

Native file writes and edits replace files atomically. Reads can observe the
complete version before or after a native replacement; separate reads are not
a shared snapshot. Queued file operations hold the workspace's lifetime so it
cannot be deleted while they wait or perform filesystem work.

Shell commands do not participate in native file locks. The harness must order
dependent commands and coordinate operations that change the same files, Git
checkout, or package environment. Use separate workspaces for independent
agents. `server_busy` means the global shell-job limit or retained-job capacity
is reached; retry after a job finishes. File operations do not consume shell-job
slots. `workspace_get` and `workspace_list` report `active_jobs`; `busy` is an
activity indicator, not a restriction on additional work.

request_id deduplicates an accepted command only while its job record remains
in the same server boot. Completed records are evicted oldest-first at the
configured record limit. This is not a durable exactly-once execution guarantee.

### Edit a file directly

Use file_edit for a targeted text change:

~~~json
{
  "workspace_id": "project-task",
  "path": "src/config.py",
  "old_text": "TIMEOUT = 30",
  "new_text": "TIMEOUT = 60"
}
~~~

The default requires exactly one match, including overlapping matches. Include
surrounding lines in old_text when the same text appears more than once.
Set replace_all to true to replace every non-overlapping occurrence.
An empty new_text deletes the matched text; old_text must be nonempty.
Identical old_text and new_text are rejected as a no-op.

Missing or ambiguous matches return edit_not_found or edit_ambiguous and leave
the file unchanged. Editing requires an existing UTF-8 file. The complete file,
edit arguments, and resulting file must fit MAX_FILE_BYTES. Other bytes,
line endings, and file permissions are preserved. The replacement is atomic
and shares a per-resolved-path queue with `file_write`. Concurrent edits to
different paths proceed independently. It does not run shell commands, commit,
or push.
