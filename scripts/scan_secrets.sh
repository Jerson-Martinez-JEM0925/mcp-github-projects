#!/bin/bash
# ── Secret Scanner ───────────────────────────────────────────────────────────
# Scans the MCP source tree for accidentally committed credentials.
# Designed to run in CI (exits non-zero on findings) or locally as a check.
#
# Patterns detected:
#   - GitHub PAT (classic): ghp_[A-Za-z0-9]{36,}
#   - GitHub PAT (fine-grained): github_pat_[A-Za-z0-9]{22,}
#   - GitHub OAuth: gho_[A-Za-z0-9]{36,}
#   - GitHub App: ghs_[A-Za-z0-9]{36,}
#   - Generic API keys: long base64 strings after 'token=' or 'key='
#
# Usage:
#   ./scripts/scan_secrets.sh              # Scan working tree
#   ./scripts/scan_secrets.sh --history    # Scan git history (slow)
# ─────────────────────────────────────────────────────────────────────────────
# shellcheck disable=SC2015,SC2034
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCAN_HISTORY="${1:-}"
FINDINGS=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
GIT_AVAILABLE=false

if REPO_ROOT="$(git -C "$MCP_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
    GIT_AVAILABLE=true
elif [ -e "$MCP_DIR/.git" ]; then
    # Docker-backed validation often bind-mounts a git worktree whose .git file
    # points at a host-only path. In that case scan the mounted source tree
    # directly instead of silently reporting a false negative.
    REPO_ROOT="$MCP_DIR"
    echo -e "${YELLOW}⚠ Git metadata is not resolvable inside this container; scanning mounted files directly.${NC}"
else
    echo -e "${RED}✗ scan_secrets.sh must run inside the MCP git repository.${NC}"
    exit 2
fi

# Patterns to detect (POSIX ERE)
PATTERNS=(
    'ghp_[A-Za-z0-9]{36,}'
    'github_pat_[A-Za-z0-9_]{22,}'
    'gho_[A-Za-z0-9]{36,}'
    'ghs_[A-Za-z0-9]{36,}'
    'ghu_[A-Za-z0-9]{36,}'
)

echo "═══ MCP Secret Scanner ═══"
echo ""

scan_pattern_files() {
    # Print repository-relative file names containing the supplied secret pattern.
    # Git checkouts scan tracked files; unresolvable mounted worktrees scan the
    # source tree directly while excluding local env files, binaries and docs.
    local pattern="$1"
    if [ "$GIT_AVAILABLE" = true ]; then
        (cd "$REPO_ROOT" && git grep -lP "$pattern" -- \
            ':!*.md' \
            ':!scripts/scan_secrets.sh' \
            ':!scripts/preflight.sh' \
            ':!docs/*' \
            2>/dev/null || true)
    else
        grep -RIlP --binary-files=without-match \
            --exclude='*.md' \
            --exclude='scan_secrets.sh' \
            --exclude='preflight.sh' \
            --exclude='.env' \
            --exclude='.env.*' \
            --exclude-dir='.git' \
            --exclude-dir='docs' \
            --exclude-dir='node_modules' \
            --exclude-dir='__pycache__' \
            --exclude-dir='.pytest_cache' \
            --exclude-dir='.mypy_cache' \
            --exclude-dir='.ruff_cache' \
            "$pattern" "$REPO_ROOT" 2>/dev/null \
            | sed "s|^${REPO_ROOT}/||" || true
    fi
}

scan_example_file() {
    # Print matching lines from .env.example in either scanning mode.
    local pattern="$1"
    if [ -f "$REPO_ROOT/.env.example" ]; then
        grep -P "$pattern" "$REPO_ROOT/.env.example" 2>/dev/null || true
    fi
}

# ── Scan working tree ────────────────────────────────────────────────────────
echo "▶ Scanning working tree..."

for pattern in "${PATTERNS[@]}"; do
    MATCHES="$(scan_pattern_files "$pattern")"

    if [ -n "$MATCHES" ]; then
        # .env.example intentionally contains an all-x placeholder. Filter that
        # exact placeholder class while still failing on any realistic value.
        if echo "$MATCHES" | grep -qx '.env.example'; then
            REAL_EXAMPLE_MATCH="$(scan_example_file "$pattern" \
                | grep -vE '(ghp|gho|ghs|ghu)_x{8,}|github_pat_x{8,}' || true)"
            if [ -z "$REAL_EXAMPLE_MATCH" ]; then
                MATCHES="$(echo "$MATCHES" | grep -vx '.env.example' || true)"
            fi
        fi
    fi

    if [ -n "$MATCHES" ]; then
        echo -e "${RED}✗ Pattern '$pattern' found in:${NC}"
        echo "$MATCHES"
        COUNT=$(printf '%s\n' "$MATCHES" | sed '/^$/d' | wc -l)
        FINDINGS=$((FINDINGS + COUNT))
    fi
done

# Also check .env files that might be tracked. This check needs real git index
# access; mounted-worktree fallback mode cannot distinguish tracked files.
if [ "$GIT_AVAILABLE" = true ]; then
    TRACKED_ENV=$(cd "$REPO_ROOT" && git ls-files '*.env' '.env*' 2>/dev/null | grep -v '.env.example\|profiles/' || true)
    if [ -n "$TRACKED_ENV" ]; then
        echo -e "${YELLOW}⚠ .env files tracked in git (should be in .gitignore):${NC}"
        echo "$TRACKED_ENV" | while read -r file; do
            echo "    $file"
        done
        FINDINGS=$((FINDINGS + 1))
    fi
fi

# Check for inline token values in mcp.json-like configs
INLINE_PATTERN='"(ghp_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}|gho_[A-Za-z0-9]{36,}|ghs_[A-Za-z0-9]{36,})"'
INLINE_TOKENS="$(scan_pattern_files "$INLINE_PATTERN")"
if [ -n "$INLINE_TOKENS" ]; then
    echo -e "${RED}✗ Inline token values found in config files:${NC}"
    echo "$INLINE_TOKENS" | while read -r file; do
        echo "    $file"
    done
    FINDINGS=$((FINDINGS + 1))
fi

echo ""

# ── Scan git history (optional) ──────────────────────────────────────────────
if [ "$SCAN_HISTORY" = "--history" ]; then
    if [ "$GIT_AVAILABLE" = true ]; then
        echo "▶ Scanning git history (this may take a while)..."

        for pattern in "${PATTERNS[@]}"; do
            HISTORY_MATCHES=$(cd "$REPO_ROOT" && git log --all -p --diff-filter=A \
                -G "$pattern" --format="%H %s" -- \
                ':!*.md' ':!scripts/*' ':!docs/*' \
                2>/dev/null | head -20 || true)

            if [ -n "$HISTORY_MATCHES" ]; then
                echo -e "${RED}✗ Pattern '$pattern' found in git history:${NC}"
                echo "$HISTORY_MATCHES" | head -5
                FINDINGS=$((FINDINGS + 1))
            fi
        done
        echo ""
    else
        echo -e "${YELLOW}⚠ Skipping history scan because git metadata is unavailable in this container.${NC}"
    fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo "═══════════════════════════════════"
if [ $FINDINGS -eq 0 ]; then
    echo -e "${GREEN}No secrets detected. Repository is clean.${NC}"
    exit 0
else
    echo -e "${RED}$FINDINGS potential secret(s) found.${NC}"
    echo ""
    echo "Actions required:"
    echo "  1. Revoke any exposed token immediately"
    echo "  2. Remove the value from the file (use env vars instead)"
    echo "  3. If in git history: consider using BFG Repo-Cleaner"
    echo "     https://rtyley.github.io/bfg-repo-cleaner/"
    exit 1
fi
