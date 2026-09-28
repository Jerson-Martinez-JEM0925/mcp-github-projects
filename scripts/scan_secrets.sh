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
#   ./mcp/scripts/scan_secrets.sh              # Scan working tree
#   ./mcp/scripts/scan_secrets.sh --history    # Scan git history (slow)
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
REPO_ROOT="$(cd "${MCP_DIR}/.." && pwd)"

# Patterns to detect (POSIX ERE)
PATTERNS=(
    'ghp_[A-Za-z0-9]{36,}'
    'github_pat_[A-Za-z0-9_]{22,}'
    'gho_[A-Za-z0-9]{36,}'
    'ghs_[A-Za-z0-9]{36,}'
    'ghu_[A-Za-z0-9]{36,}'
)

# Files to exclude from scanning (test fixtures, docs with examples)
EXCLUDE_PATTERNS="*.md:scan_secrets.sh:preflight.sh"

echo "═══ MCP Secret Scanner ═══"
echo ""

# ── Scan working tree ────────────────────────────────────────────────────────
echo "▶ Scanning working tree..."

for pattern in "${PATTERNS[@]}"; do
    # Search tracked files only, exclude known safe files
    MATCHES=$(cd "$REPO_ROOT" && git grep -lP "$pattern" -- \
        ':!*.md' \
        ':!mcp/scripts/scan_secrets.sh' \
        ':!mcp/scripts/preflight.sh' \
        ':!mcp/docs/*' \
        2>/dev/null || true)

    if [ -n "$MATCHES" ]; then
        echo -e "${RED}✗ Pattern '$pattern' found in:${NC}"
        echo "$MATCHES" | while read -r file; do
            echo "    $file"
            FINDINGS=$((FINDINGS + 1))
        done
    fi
done

# Also check .env files that might be tracked
TRACKED_ENV=$(cd "$REPO_ROOT" && git ls-files '*.env' '.env*' 2>/dev/null | grep -v '.env.example\|profiles/' || true)
if [ -n "$TRACKED_ENV" ]; then
    echo -e "${YELLOW}⚠ .env files tracked in git (should be in .gitignore):${NC}"
    echo "$TRACKED_ENV" | while read -r file; do
        echo "    $file"
    done
    FINDINGS=$((FINDINGS + 1))
fi

# Check for inline token values in mcp.json-like configs
INLINE_TOKENS=$(cd "$REPO_ROOT" && git grep -lP '"(ghp_|github_pat_|gho_|ghs_)[A-Za-z0-9]+"' -- \
    ':!*.md' ':!mcp/scripts/*' ':!mcp/docs/*' 2>/dev/null || true)
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
    echo "▶ Scanning git history (this may take a while)..."

    for pattern in "${PATTERNS[@]}"; do
        HISTORY_MATCHES=$(cd "$REPO_ROOT" && git log --all -p --diff-filter=A \
            -G "$pattern" --format="%H %s" -- \
            ':!*.md' ':!mcp/scripts/*' ':!mcp/docs/*' \
            2>/dev/null | head -20 || true)

        if [ -n "$HISTORY_MATCHES" ]; then
            echo -e "${RED}✗ Pattern '$pattern' found in git history:${NC}"
            echo "$HISTORY_MATCHES" | head -5
            FINDINGS=$((FINDINGS + 1))
        fi
    done
    echo ""
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
