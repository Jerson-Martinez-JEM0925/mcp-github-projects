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
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && case "${TARGETARCH:-amd64}" in \
         amd64) gh_arch=amd64; gh_sha256=98cb101ad87dbbd3f6b2f2bfcbd43197ccd07643140121d8a9cfaa91eadbc24e ;; \
         arm64) gh_arch=arm64; gh_sha256=cb34cefc6f4e77b90731374a2b737516bb8f3033c6ca46aa949e5344f68069e3 ;; \
         *) echo "Unsupported gh architecture: ${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/cli/cli/releases/download/v2.104.0/gh_2.104.0_linux_${gh_arch}.tar.gz" -o /tmp/gh.tar.gz \
    && echo "${gh_sha256}  /tmp/gh.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/gh.tar.gz -C /tmp \
    && install -m 0755 "/tmp/gh_2.104.0_linux_${gh_arch}/bin/gh" /usr/local/bin/gh \
    && rm -rf /tmp/gh.tar.gz "/tmp/gh_2.104.0_linux_${gh_arch}" \
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
RUN printf '%s\n' 'pip==26.2.2 --hash=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 --hash=sha256:3a8e5c800492787f33a4114ef54444546c340df428974ebba17e0fd47e11af2f' > /tmp/pip-bootstrap.lock \
    && pip install --no-cache-dir --require-hashes -r /tmp/pip-bootstrap.lock \
    && rm /tmp/pip-bootstrap.lock \
    && pip install --no-cache-dir --require-hashes -r requirements-runtime.lock \
    && if [ "$INSTALL_TEST_DEPS" = "true" ]; then pip install --no-cache-dir --require-hashes -r requirements-dev.lock; fi

COPY --chown=mcp:mcp . .

USER mcp

# The MCP server runs with stdio transport
CMD ["python", "server.py"]
