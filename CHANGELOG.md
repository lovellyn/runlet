# Changelog

## 0.3.0

- Name the project Runlet, with the runlet Python package, CLI, and container examples.
- Prepare a general-purpose Linux MCP server repository under the Apache License 2.0.
- Make Docker and source deployment independent of any hosting provider.
- Use port 8000 by default and /data for container workspace persistence.
- Configure public hosts and origins explicitly.
- Group monthly Dependabot version updates per ecosystem and limit each to one open PR.
- Retry connection resets and premature HTTP disconnects during container startup checks.
- Resolve Docker's current published port again after restarting the smoke-test container.
- Add bounded file snapshots with expiring download capabilities, streaming,
  HEAD/range support, checksums, and automatic cache cleanup.
- Add Python/package CI, Docker smoke validation, and stable-release GHCR publishing.
- Add a small Hugging Face deployment example that consumes the published image.
- Keep user/group setup in deployment wrappers; the shared image inherits
  Ubuntu's default root user.
- Retain MCP SDK 2.x, parallel jobs and file tools, PTY/stdin/resize, native image
  previews, detached-child cleanup, and the existing development toolchain.
