# syntax=docker/dockerfile:1
ARG NODE_VERSION=24.22.1
FROM node:${NODE_VERSION}-bookworm-slim AS node_runtime

FROM ubuntu:26.04

LABEL org.opencontainers.image.title="Runlet" \
      org.opencontainers.image.description="Linux workspaces and development tools for trusted MCP agents" \
      org.opencontainers.image.source="https://github.com/lovellyn/runlet" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bash build-essential ca-certificates curl ffmpeg git jq libatomic1 pkg-config \
        python3 python3-dev python3-venv ripgrep tini unzip zip \
    && rm -rf /var/lib/apt/lists/*

# Headless Chromium libraries and fonts; browser downloads stay workspace-local.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        fonts-liberation libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 \
        libatspi2.0-0t64 libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 \
        libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 \
        libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
    && rm -rf /var/lib/apt/lists/*

# Browser executables and project dependencies are installed by agents per workspace.
RUN python3 -m venv /opt/runlet && mkdir -p /data
ENV PATH="/opt/runlet/bin:${PATH}"

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version && npx --version

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/root \
    SANDBOX_ROOT=/data \
    PORT=8000

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD curl --fail --silent "http://127.0.0.1:${PORT}/healthz" > /dev/null || exit 1
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["/opt/runlet/bin/runlet"]
