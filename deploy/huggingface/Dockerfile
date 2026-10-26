# Use a published Runlet release from the canonical repository's GHCR package.
# A digest-pinned reference can be used instead of the version tag.
ARG BASE_IMAGE=ghcr.io/lovellyn/runlet:0.3.1
FROM ${BASE_IMAGE}

# Hugging Face requires UID 1000. Keep its numeric identity policy in this wrapper.
USER root
RUN if ! getent group 1000 > /dev/null; then groupadd --gid 1000 user; fi \
    && if getent passwd 1000 > /dev/null; then \
        usermod --home /home/user --shell /bin/bash --gid 1000 \
            "$(getent passwd 1000 | cut -d: -f1)"; \
    else \
        useradd --create-home --uid 1000 --gid 1000 \
            --home-dir /home/user --shell /bin/bash user; \
    fi \
    && mkdir -p /home/user /data \
    && chown 1000:1000 /home/user /data

ENV HOME=/home/user
USER 1000:1000
