# Workspaces, Git ownership, and recovery

## Persistent data

Mount SANDBOX_ROOT on persistent storage to retain local files, workspace metadata,
and dependency environments across server replacement. Existing registrations are
loaded at startup. Back up the volume or explicitly push Git progress before
losing that storage. Running jobs and their records end with the server process.

## Commit identity

Set GIT_USER_NAME and GIT_USER_EMAIL in the server environment. For example,
use `Sandbox Agent` as the name and your verified GitHub email or the exact noreply address shown in your GitHub email settings. These
are commit metadata, separate from GITHUB_TOKEN, which authenticates GitHub access.

When creating a workspace, the sandbox runs `git config --global` for each
nonempty identity variable, using that workspace's own HOME. This initializes
author and committer identity in the workspace's `.gitconfig`, outside the project
checkout. Each workspace has independent global Git settings.

Reopening a workspace or restarting the server preserves its existing config.
Changing the server environment variables affects workspaces created after the server restarts.
Configure these environment variables again when recreating the deployment.
A newly created workspace receives the configured identity. Agent customizations must be reapplied
when a workspace is recreated.

Agents can override the defaults with normal Git configuration:

~~~bash
git config user.name "Project Agent"
git config user.email "agent@example.invalid"
~~~

Repository-local settings override the workspace-global identity. Agents can also
change their workspace's global settings with `git config --global`, use command
options such as `git -c user.name=...`, or supply explicit GIT_AUTHOR_* /
GIT_COMMITTER_* environment values. When the server environment variables are unset, the sandbox
supplies no identity defaults. Existing workspaces are never backfilled, including
ones created before identity variables were configured. The server does not change
repository config, create commits, or push anything.

## Recover pushed work

Only files explicitly committed and successfully pushed by the agent can be
reconstructed from GitHub after a server loses its local filesystem. A local
commit, workspace ID, or process ID is not a backup.

The agent should retain this outside this server:

~~~json
{
  "workspace_id": "project-task",
  "repo_url": "https://github.com/your-org/your-project.git",
  "ref": "task/my-work"
}
~~~

After losing the data directory:

1. Check server_info. A different boot_id identifies a new server lifetime.
2. Call workspace_create with the saved ID, repository URL, and desired ref.
3. Reinstall dependencies from committed project files.
4. Inspect saved progress and outputs, then decide which commands to run.

A branch restores its current remote tip; a commit SHA restores an exact
revision. When changing the intended recovery branch, update the external
recovery record. Job history and process memory end at server shutdown. Losing the data directory
also loses unpushed files and dependency installations. Old jobs report job_not_found, not fabricated
completion or exit codes. Interrupted commands may have external side effects;
the agent must decide whether retrying is appropriate.

The server stores its local bookkeeping outside the working checkout. Agents
may commit progress notes or selected logs themselves, with their normal Git
workflow. Startup restores existing local workspace registrations only when
the local filesystem is still present; it never automatically clones remotes.
