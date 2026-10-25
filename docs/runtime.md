# Runtime tools

The image uses Ubuntu 24.04 LTS and serves port 8000. These tools are installed
at build time and remain available after a restart:

| Purpose | Included tools |
| --- | --- |
| Python | Python 3.12, pip in virtual environments; Pillow for image previews |
| JavaScript | Node.js 24.22.1, npm/npx |
| Source control | Git with HTTPS transport |
| Search and data | ripgrep (`rg`), jq |
| Downloads and archives | curl, zip/unzip, tar/gzip from Ubuntu |
| Native builds | GCC/G++, make, pkg-config, Python development headers |
| Browser runtime | Headless Chromium system libraries, Liberation fonts |
| Media | FFmpeg and ffprobe |
| Shell and processes | Bash, Tini |

Python comes from Ubuntu's packages; the MCP server and its Python dependencies
are installed in `/opt/runlet`, a dedicated virtual environment, without
changing system Python packages. Node/npm are copied from the pinned official
Node image. Ubuntu tools receive the package versions available at build time.
The Docker build argument `NODE_VERSION` pins the Node release. Override it when
rebuilding to change that version. Add extra system tools to the Dockerfile when
needed.

The shared image inherits Ubuntu's default root user with `/root` as HOME.
It does not create an account or specify runtime UID/GID values. Tini, the MCP
server, and agent commands share the deployment's runtime identity. Root agents
can install OS packages directly; a deployment using a non-root account needs
additional OS packages baked into its image. Every workspace receives a
separate Python virtual environment, home directory, and temporary directory.
Project package installation is an explicit agent command.


### Python projects

Install project dependencies into the workspace's active virtual environment
with `python -m pip install -r requirements.txt` or `python -m pip install -e .`.
Keep project dependency declarations and lockfiles in Git; recreate the
environment after filesystem loss. Install any additional package manager in
the workspace only when the project requires it.

### Frontend projects

Agents can install and build frontend projects with Node.js and npm.
Install dependencies in the project checkout and keep the lockfile in Git. For
an existing npm project, run the scripts provided by its `package.json`, for example:

~~~bash
npm ci
npm run lint
npm run build
~~~

Each workspace keeps its own `node_modules` and npm cache (under its own `HOME`).
After filesystem loss, recreate the checkout and run `npm ci` again. Node.js and
npm are built into the container image and remain available after a restart.
Frontend frameworks and project dependencies are installed by the agent.

Building and testing projects works through the existing MCP tools. A dev server
can run alongside file edits and browser-test commands in the same workspace.
Keep the server job alive while testing, then cancel it when finished. External
preview routing is not provided by this scaffold.

### Headless browser tests

The image includes Chromium's Ubuntu 24.04 runtime libraries and a basic Latin
font, based on [Playwright 1.64.1's dependency list](https://github.com/microsoft/playwright/blob/v1.64.1/packages/playwright-core/src/server/registry/nativeDeps.ts).
Install Playwright and its matching Chromium headless shell inside the workspace:

~~~bash
npm install --save-dev @playwright/test
npx playwright install --only-shell chromium
npx playwright test
~~~

For a project that already declares Playwright, use its lockfile and package
manager to install dependencies. Browser downloads use the workspace's own
`HOME` cache and need no root access. Do not pass `--with-deps`; OS libraries are
already in the image. The Python equivalent is `python -m pip install playwright`, then
`python -m playwright install --only-shell chromium`.

This baseline targets headless Chromium. Other browsers, headed sessions, extra
fonts, or future Playwright versions may require additional image packages.

### Media processing

The image includes Ubuntu's FFmpeg package for audio/video conversion, frame or
audio extraction, and media inspection. `ffmpeg` and `ffprobe` are available to
agents under the selected runtime account. For example:

~~~bash
ffmpeg -i input.mp4 -vn audio.wav
ffprobe -v error -show_format -show_streams -of json input.mp4
~~~

This system installation is available to all workspaces. Playwright manages its
own FFmpeg download for its built-in video recorder.
