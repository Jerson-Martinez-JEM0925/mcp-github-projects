# syntax=docker/dockerfile:1
# ── GitHub Project Management MCP Server ─────────────────────────────────────
# Generic MCP server for GitHub Projects V2 with gh CLI.

FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

LABEL org.opencontainers.image.title="GitHub Project MCP Server" \
      org.opencontainers.image.description="Generic MCP server for GitHub Projects V2 management" \
      org.opencontainers.image.source="https://github.com/jersonmartinez/mcp-github-projects"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install a pinned gh CLI release and verify its published SHA-256 checksum.
# The GitHub CLI apt repository does not retain every historical package build,
# so apt pinning alone is not reproducible. Release archives plus checksums are.
ARG TARGETARCH
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates libzstd1=1.5.7+dfsg-1+deb13u1 \
    && case "${TARGETARCH:-amd64}" in \
         amd64) gh_arch=amd64; gh_sha256=79e7ab5db738c48777b696e374f8b428f0647debfb424f44d2f426370407fe2f ;; \
         arm64) gh_arch=arm64; gh_sha256=b1278c75107423d818f1937fd92cafc9e5e9b606631568f68d7fb816f5885a02 ;; \
         *) echo "Unsupported gh architecture: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/cli/cli/releases/download/v2.105.0/gh_2.105.0_linux_${gh_arch}.tar.gz" -o /tmp/gh.tar.gz \
    && echo "${gh_sha256}  /tmp/gh.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/gh.tar.gz -C /tmp \
    && install -m 0755 "/tmp/gh_2.105.0_linux_${gh_arch}/bin/gh" /usr/local/bin/gh \
    && rm -rf /tmp/gh.tar.gz "/tmp/gh_2.105.0_linux_${gh_arch}" \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/false --uid 1000 mcp \
    && mkdir -p /home/mcp/.cache/github-project-mcp \
    && chown -R mcp:mcp /home/mcp/.cache

WORKDIR /app

COPY requirements.txt .
COPY requirements-dev.txt .
COPY requirements-runtime.lock .
COPY requirements-dev.lock .
ARG INSTALL_TEST_DEPS=false
RUN printf '%s\n' 'pip==26.3 --hash=sha256:f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13 --hash=sha256:6c36ae7da8695be67e3cd9f7d87919667ced53f57ce2ede75d89b5912ed2bc4c' > /tmp/pip-bootstrap.lock \
    && pip install --no-cache-dir --require-hashes -r /tmp/pip-bootstrap.lock \
    && rm /tmp/pip-bootstrap.lock \
    && pip install --no-cache-dir --require-hashes -r requirements-runtime.lock \
    && if [ "$INSTALL_TEST_DEPS" = "true" ]; then \
         pip install --no-cache-dir --require-hashes -r requirements-dev.lock; \
       else \
         site_packages="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
         && rm -rf "$site_packages/pip" "$site_packages"/pip-*.dist-info \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12; \
       fi

COPY --chown=mcp:mcp . .

USER mcp

# The MCP server runs with stdio transport
CMD ["python", "server.py"]
