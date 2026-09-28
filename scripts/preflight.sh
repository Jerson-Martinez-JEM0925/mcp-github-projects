#!/bin/bash
# shellcheck disable=SC2181
# ── MCP Preflight Check ──────────────────────────────────────────────────────
# Validates prerequisites before starting the MCP server.
# Run this ONCE before first use, or after environment changes.
#
# Exit codes:
#   0 — All prerequisites met
#   1 — Missing prerequisites (with actionable guidance)
#
# Usage:
#   ./mcp/scripts/preflight.sh          # Check only
#   ./mcp/scripts/preflight.sh --fix    # Attempt automatic resolution
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# shellcheck disable=SC2181
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

FIX_MODE="${1:-}"
ERRORS=0

info()  { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
fail()  { echo -e "${RED}✗${NC} $1"; ERRORS=$((ERRORS + 1)); }

echo "═══ MCP Preflight Check ═══"
echo ""

# ── 1. Docker ────────────────────────────────────────────────────────────────
echo "▶ Docker"
if command -v docker &>/dev/null; then
    DOCKER_VERSION=$(docker --version | grep -oP '\d+\.\d+\.\d+' | head -1)
    info "Docker $DOCKER_VERSION found"
    if docker info &>/dev/null; then
        info "Docker daemon is running"
    else
        fail "Docker daemon is not running. Start Docker Desktop or the Docker service."
    fi
else
    fail "Docker not found. Install from https://docs.docker.com/get-docker/"
fi
echo ""

# ── 2. GitHub Token ──────────────────────────────────────────────────────────
echo "▶ GitHub Authentication"
TOKEN_SOURCE=""
if [ -n "${GITHUB_TOKEN:-}" ]; then
    TOKEN_SOURCE="GITHUB_TOKEN env var"
    info "Token found via $TOKEN_SOURCE"
elif [ -n "${GH_TOKEN:-}" ]; then
    TOKEN_SOURCE="GH_TOKEN env var"
    info "Token found via $TOKEN_SOURCE"
elif command -v gh &>/dev/null && gh auth status &>/dev/null 2>&1; then
    TOKEN_SOURCE="gh auth (CLI)"
    info "Token available via $TOKEN_SOURCE"
else
    fail "No GitHub token found."
    echo "    Resolution options (choose one):"
    echo ""
    echo "    A) Set environment variable (recommended for local dev):"
    echo "       export GITHUB_TOKEN=ghp_your_token_here"
    echo ""
    echo "    B) Authenticate via GitHub CLI:"
    echo "       gh auth login --scopes 'repo,project,read:org'"
    echo ""
    echo "    C) Add to .env file (never commit this file):"
    echo "       echo 'GITHUB_TOKEN=ghp_...' >> .env"
    echo ""

    if [ "$FIX_MODE" = "--fix" ]; then
        echo "    Attempting automatic fix via gh CLI..."
        if command -v gh &>/dev/null; then
            echo "    → gh is installed. Running 'gh auth login'..."
            gh auth login --scopes "repo,project,read:org"
            if gh auth status &>/dev/null 2>&1; then
                info "Authentication successful via gh CLI"
                ERRORS=$((ERRORS - 1))
            fi
        else
            warn "gh CLI not installed. Installing via Docker..."
            echo "    The MCP container includes gh CLI internally."
            echo "    For host-level auth, install gh: https://cli.github.com/"
            echo ""
            echo "    Quick install (Linux):"
            echo "      curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \\"
            echo "        | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg"
            echo "      echo 'deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \\"
            echo "        https://cli.github.com/packages stable main' \\"
            echo "        | sudo tee /etc/apt/sources.list.d/github-cli.list"
            echo "      sudo apt update && sudo apt install gh"
        fi
    fi
fi
echo ""

# ── 3. MCP Docker Image ─────────────────────────────────────────────────────
echo "▶ MCP Docker Image"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if docker image inspect github-project-mcp:latest &>/dev/null 2>&1; then
    info "Image github-project-mcp:latest exists"
elif docker image inspect factib_mcp:latest &>/dev/null 2>&1; then
    warn "Legacy image factib_mcp:latest found (rename recommended)"
    info "Using legacy image as fallback"
else
    fail "MCP image not built."
    if [ "$FIX_MODE" = "--fix" ]; then
        echo "    Building image..."
        docker build -t github-project-mcp:latest "$MCP_DIR"
        if [ $? -eq 0 ]; then
            info "Image built successfully"
            ERRORS=$((ERRORS - 1))
        fi
    else
        echo "    Build it with:"
        echo "      docker build -t github-project-mcp:latest ./mcp"
    fi
fi
echo ""

# ── 4. Target Configuration ─────────────────────────────────────────────────
echo "▶ Target Configuration"
if [ -n "${GH_PROJECT_ORG_NAME:-}" ] && [ -n "${GH_PROJECT_REPO_NAME:-}" ] && [ -n "${GH_PROJECT_PROJECT_NUMBER:-}" ]; then
    info "Target: ${GH_PROJECT_ORG_NAME}/${GH_PROJECT_REPO_NAME} #${GH_PROJECT_PROJECT_NUMBER}"
elif [ -f "$MCP_DIR/.env" ]; then
    info ".env file found in $MCP_DIR"
    # Validate it has the required fields
    if grep -q "GH_PROJECT_ORG_NAME" "$MCP_DIR/.env" && grep -q "GH_PROJECT_REPO_NAME" "$MCP_DIR/.env"; then
        info "Target configuration present in .env"
    else
        fail ".env exists but missing required fields (GH_PROJECT_ORG_NAME, GH_PROJECT_REPO_NAME, GH_PROJECT_PROJECT_NUMBER)"
    fi
else
    fail "No target configuration found."
    echo "    Set via environment or copy a profile:"
    echo "      cp mcp/profiles/example.env mcp/.env"
    echo "      # Edit with your org/repo/project values"
fi
echo ""

# ── 5. Token Scope Validation (optional, requires network) ──────────────────
echo "▶ Token Scope Validation"
if [ -n "${TOKEN_SOURCE:-}" ] && command -v curl &>/dev/null; then
    # Resolve the actual token value
    if [ -n "${GITHUB_TOKEN:-}" ]; then
        _TOKEN="$GITHUB_TOKEN"
    elif [ -n "${GH_TOKEN:-}" ]; then
        _TOKEN="$GH_TOKEN"
    elif command -v gh &>/dev/null; then
        _TOKEN=$(gh auth token 2>/dev/null || echo "")
    else
        _TOKEN=""
    fi

    if [ -n "$_TOKEN" ]; then
        SCOPES=$(curl -s -H "Authorization: Bearer $_TOKEN" https://api.github.com \
            -o /dev/null -w '' -D - 2>/dev/null | grep -i "x-oauth-scopes:" | sed 's/x-oauth-scopes: //i' | tr -d '\r')
        if [ -n "$SCOPES" ]; then
            info "Token scopes: $SCOPES"
            # Check for required scopes (or their parents)
            if echo "$SCOPES" | grep -qi "repo" && echo "$SCOPES" | grep -qi "project"; then
                info "Required scopes present (repo, project)"
            else
                warn "May be missing required scopes. Needed: repo, project, read:org (or admin:org)"
            fi
        else
            warn "Could not validate scopes (fine-grained tokens don't report scopes via header)"
        fi
    fi
else
    warn "Skipping scope validation (no token or curl unavailable)"
fi
echo ""

# ── Summary ──────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════"
if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN}All prerequisites met. MCP is ready to use.${NC}"
    echo ""
    echo "Start the MCP server:"
    echo "  docker run --rm -i -e GITHUB_TOKEN --env-file mcp/.env github-project-mcp:latest"
    exit 0
else
    echo -e "${RED}$ERRORS prerequisite(s) not met.${NC}"
    echo ""
    echo "Run with --fix to attempt automatic resolution:"
    echo "  ./mcp/scripts/preflight.sh --fix"
    exit 1
fi
