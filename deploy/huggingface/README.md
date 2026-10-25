---
title: Runlet
emoji: 🧰
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 8000
---

# Runlet on Hugging Face

This is an optional deployment template for the general-purpose container.

1. Publish a release from [lovellyn/runlet](https://github.com/lovellyn/runlet)
   and make its GHCR package public. Confirm that it can be pulled without credentials.
2. The adjacent Dockerfile defaults to ghcr.io/lovellyn/runlet:0.3.0.
   Select a published version or immutable digest before deploying.
3. Create a Docker Space and copy this README.md and Dockerfile into its root.
4. Add SANDBOX_TOKEN as a Space secret.
5. Set ALLOWED_HOSTS to the Space's actual hostname, for example
   your-user-your-space.hf.space. No automatic SPACE_HOST detection is used.
6. For browser clients, set ALLOWED_ORIGINS to the corresponding HTTPS origin.
7. Optionally configure GITHUB_TOKEN, GIT_USER_NAME, and GIT_USER_EMAIL.
8. Connect to https://YOUR-SPACE-HOST.hf.space/mcp with X-Sandbox-Token.

For a private Space, the client additionally supplies its Hugging Face read
credential in Authorization: Bearer <HF_TOKEN>. The platform checks that
credential; the sandbox independently checks X-Sandbox-Token.

To enable file_export, set PUBLIC_BASE_URL=https://YOUR-SPACE-HOST.hf.space
without /mcp. A private Space still requires its platform authentication for
downloads; the temporary link cannot bypass that requirement. See the main
repository's docs/exports.md for limits, link access, and harness integration.

The generic example client accepts GATEWAY_TOKEN for this upstream credential:

~~~bash
export SANDBOX_URL=https://YOUR-SPACE-HOST.hf.space/mcp
export SANDBOX_TOKEN=your-sandbox-token
export GATEWAY_TOKEN=your-hugging-face-read-token
python examples/client.py
~~~

The example client is run from the full source repository, not these two files.

The shared image inherits Ubuntu's default root user.
This deployment wrapper configures UID/GID 1000 and HOME=/home/user for Hugging
Face. Those settings are confined to this deployment. It uses /data and port 8000.
A Space's ordinary local filesystem is ephemeral. If persistent storage is available,
configure SANDBOX_ROOT to a writable directory on that storage. Push Git progress
explicitly or back up files before losing ephemeral storage.

Updating this Dockerfile to a new version/digest and rebuilding selects a new
container release. Changes to a mutable remote tag do not reliably trigger a
Space rebuild.

## Validate locally

After building the shared image as runlet:ci, run this check from the
source repository root:

~~~bash
bash deploy/huggingface/smoke.sh
~~~

The check builds the wrapper against the local image, verifies its configured
runtime IDs, and exercises the normal MCP smoke test. BASE_IMAGE is also a Docker
build argument for selecting the parent image when building this wrapper.
Only README.md and Dockerfile need to be copied to the Space.

References:
- [Docker Spaces](https://huggingface.co/docs/hub/spaces-sdks-docker)
- [Private endpoint authentication](https://huggingface.co/docs/hub/spaces-api-endpoints)
- [GHCR package visibility](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
