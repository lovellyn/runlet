# Waiting for jobs

Runlet uses ordinary MCP tool calls. exec and job_status accept the same
wait_for modes and a bounded wait_seconds. No protocol extension or background
subscription is required.

## Wait contract

| Argument | exec default | job_status default | Meaning |
| --- | --- | --- | --- |
| wait_for | completion | completion | completion or output |
| wait_seconds | 2 | 20 | Maximum wait, from 0 to 20 seconds |

Completion mode waits for the job to finish or the wait deadline. Log writes
do not wake it. This is useful for builds/tests when the agent needs the result
rather than individual log updates.

Output mode waits for new bytes and collects a short batch. The first observed
unread bytes start a fixed 200 ms window. Subsequent output never resets that
window. A full response page or completed job returns immediately; the request
deadline can cut the window short. This is useful for interactive processes,
PTY prompts, and a harness collecting live logs. Prompts need no newline.

wait_seconds=0 always reads immediately, without batching or waiting for the
process. A completed job also returns immediately in either mode.

The limit applies to waiting, not execution. Reaching a wait deadline returns a
snapshot with state=running; it does not stop the job or reset its execution
timeout. The exec timeout separately limits execution. Cancelling an MCP wait
also leaves the job running; job_cancel explicitly stops it.

server_info.waiting advertises the maximum wait, fixed output batch window, mode
default, and wait_seconds defaults. Waiting does not occupy an extra shell-job
slot or block other tools.

## Examples

Wait for a build:

~~~json
{
  "workspace_id": "project-task",
  "command": "npm run build",
  "wait_for": "completion",
  "wait_seconds": 2,
  "timeout": 300
}
~~~

If it is still running, call job_status:

~~~json
{
  "job_id": "BOOT-JOB",
  "cursor": 123,
  "wait_for": "completion",
  "wait_seconds": 20
}
~~~

Here cursor is the previous response's next_cursor. For live output or a prompt,
choose wait_for=output. To inspect status without waiting, use wait_seconds=0.

Positive waits on job_write_stdin always use output mode, because the caller is
interacting with a process. Its default remains wait_seconds=0. Its cursor and
input-offset replay protections are independent.

## Cursors and retained output

Each response returns bounded output starting at cursor, plus next_cursor,
has_more, and truncated. Pass next_cursor to the next read. Cursors count bytes,
not decoded characters. Waiting never advances a cursor without returning the
corresponding retained bytes.

Drain has_more with wait_seconds=0, including after the job has completed,
failed, been cancelled, or timed out. Completion is independent of log-page
draining.

The output buffer remains bounded. Completion waits may allow a noisy job to
overwrite old bytes before the next read; truncated explicitly reports this.
Use command redirection or tee to retain a full log in a workspace file.
Output-mode collection can drain more often but cannot guarantee retention
across a stalled or disconnected reader.

## Reusable harness loop

examples/client.py exports follow_job, an async iterator. It handles waiting
and cursor advancement in host code, drains available pages immediately, and
stops only after terminal state plus all retained output pages:

~~~python
from examples.client import data, follow_job

initial = data(await client.call_tool("exec", {
    "workspace_id": workspace_id,
    "command": "npm run build",
    "wait_seconds": 0,
}))

with open("collected.log", "w") as log:
    async for update in follow_job(client, initial, wait_for="output"):
        if update["truncated"]:
            log.write("[Earlier output was truncated]\n")
        log.write(update["output"])

# update now holds the final job snapshot.
~~~

Use the default completion mode when periodic/terminal results are sufficient.
Use output mode for prompt handling or frequent collection. The iterator keeps
no growing log buffer and preserves truncation flags for its consumer.

The harness can spool chunks or update its UI without invoking a model on each
response, and send a useful result to the model at completion or failure. The
helper does not summarize or discard fields, silently retry failed calls, replay
commands after a restart, or cancel the job when iteration stops. The caller owns
job cancellation, reconnection, full-log storage, and model-notification policy.

The helper requires a positive wait_seconds up to 20 to avoid a busy polling
loop; it uses immediate reads internally to drain available pages.
