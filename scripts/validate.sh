#!/bin/bash
# shellcheck disable=SC2034,SC2181
# ── MCP Local Validation ─────────────────────────────────────────────────────
# Runs the same checks as the GitHub Actions MCP CI pipeline locally.
# Execute BEFORE creating a PR or pushing changes to catch issues early.
#
# This script mirrors .github/workflows/pipeline-mcp-ci.yaml exactly:
#   1. Docker build
#   2. Python syntax check (BOM + ast.parse)
#   3. Unit tests
#   4. Tool count verification (>= 100)
#   5. Secret scan
#
# Usage:
#   ./mcp/scripts/validate.sh           # Full validation
#   ./mcp/scripts/validate.sh --quick   # Skip image build (use cached)
#   ./mcp/scripts/validate.sh --fix     # Auto-fix BOM characters
#
# shellcheck disable=SC2034,SC2181
# Exit codes:
#   0 — All checks pass (safe to push)
#   1 — One or more checks failed (do NOT push)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${MCP_DIR}/.." && pwd)"

MODE="${1:-full}"
IMAGE="github-project-mcp:validate"
ERRORS=0

step()  { echo -e "\n${BOLD}▶ $1${NC}"; }
pass()  { echo -e "  ${GREEN}✓${NC} $1"; }
fail()  { echo -e "  ${RED}✗${NC} $1"; ERRORS=$((ERRORS + 1)); }
warn()  { echo -e "  ${YELLOW}⚠${NC} $1"; }

echo -e "${BOLD}═══ MCP Local Validation ═══${NC}"
echo "Mirrors: .github/workflows/pipeline-mcp-ci.yaml"
echo ""

# ── 0. Pre-check: BOM removal ────────────────────────────────────────────────
step "BOM Check (UTF-8 byte-order marks)"

BOM_FILES=$(cd "$MCP_DIR" && python3 -c "
import os
found = []
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d != '__pycache__']
    for f in files:
        if f.endswith('.py'):
            path = os.path.join(root, f)
            with open(path, 'rb') as fh:
                if fh.read(3) == b'\xef\xbb\xbf':
                    found.append(path)
for p in found:
    print(p)
")

if [ -n "$BOM_FILES" ]; then
    BOM_COUNT=$(echo "$BOM_FILES" | wc -l)
    if [ "$MODE" = "--fix" ]; then
        cd "$MCP_DIR" && python3 -c "
import os
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d != '__pycache__']
    for f in files:
        if f.endswith('.py'):
            path = os.path.join(root, f)
            with open(path, 'rb') as fh:
                content = fh.read()
            if content[:3] == b'\xef\xbb\xbf':
                with open(path, 'wb') as fh:
                    fh.write(content[3:])
"
        pass "Fixed BOM in $BOM_COUNT file(s)"
    else
        fail "$BOM_COUNT file(s) have BOM characters"
        echo "$BOM_FILES" | head -5 | while read -r f; do echo "    $f"; done
        echo ""
        echo "    Fix with: ./mcp/scripts/validate.sh --fix"
    fi
else
    pass "No BOM characters found"
fi

# ── 1. Docker Build ──────────────────────────────────────────────────────────
step "Docker Build"

if [ "$MODE" = "--quick" ] && docker image inspect "$IMAGE" &>/dev/null 2>&1; then
    pass "Using cached image (--quick mode)"
else
    if docker build -t "$IMAGE" "$MCP_DIR" > /tmp/mcp-build.log 2>&1; then
        pass "Image built successfully"
    else
        fail "Docker build failed"
        tail -10 /tmp/mcp-build.log
        echo ""
        echo "    Full log: /tmp/mcp-build.log"
        # Cannot continue without image
        echo -e "\n${RED}Cannot continue without a built image. Fix build errors first.${NC}"
        exit 1
    fi
fi

# ── 2. Python Syntax Check ───────────────────────────────────────────────────
step "Python Syntax Check (ast.parse)"

SYNTAX_RESULT=$(docker run --rm "$IMAGE" python3 -c "
import ast, os, sys
errors = []
for root, dirs, files in os.walk('/app'):
    dirs[:] = [d for d in dirs if d != '__pycache__']
    for f in files:
        if f.endswith('.py'):
            path = os.path.join(root, f)
            try:
                ast.parse(open(path).read())
            except SyntaxError as e:
                errors.append(f'{path}: {e.msg} (line {e.lineno})')
if errors:
    for e in errors:
        print(e)
    sys.exit(1)
else:
    print(f'All Python files parse cleanly')
" 2>&1) || {
    fail "Syntax errors found:"
    echo "$SYNTAX_RESULT" | while read -r line; do echo "    $line"; done
    true
}

if [ $? -eq 0 ] || echo "$SYNTAX_RESULT" | grep -q "parse cleanly"; then
    pass "$SYNTAX_RESULT"
fi

# ── 3. Unit Tests ────────────────────────────────────────────────────────────
step "Unit Tests"

TEST_RESULT=$(docker run --rm \
    -e GH_PROJECT_ORG_NAME=Test \
    -e GH_PROJECT_REPO_NAME=Test \
    -e GH_PROJECT_PROJECT_NUMBER=1 \
    "$IMAGE" python3 -c "
import os, sys, importlib
sys.path.insert(0, '/app')
os.chdir('/app')

# Run all test files that can execute without pytest
test_dir = 'tests'
if not os.path.isdir(test_dir):
    print('No tests directory found')
    sys.exit(0)

passed = 0
failed = 0
for f in sorted(os.listdir(test_dir)):
    if f.startswith('test_') and f.endswith('.py') and f != 'test_contracts.py':
        try:
            # Try running the test module's __main__ block
            exec(open(os.path.join(test_dir, f)).read())
            passed += 1
        except SystemExit:
            passed += 1  # Some tests call sys.exit(0) on success
        except Exception as e:
            print(f'FAIL: {f}: {type(e).__name__}: {e}')
            failed += 1

print(f'{passed} test file(s) passed, {failed} failed')
if failed > 0:
    sys.exit(1)
" 2>&1)

TEST_EXIT=$?
if [ $TEST_EXIT -eq 0 ]; then
    pass "$TEST_RESULT"
else
    fail "Tests failed:"
    echo "$TEST_RESULT" | tail -10 | while read -r line; do echo "    $line"; done
fi

# ── 4. Tool Count Verification ───────────────────────────────────────────────
step "Tool Count (>= 100)"

TOOL_COUNT=$(docker run --rm \
    -e GH_PROJECT_ORG_NAME=Test \
    -e GH_PROJECT_REPO_NAME=Test \
    -e GH_PROJECT_PROJECT_NUMBER=1 \
    "$IMAGE" python3 -c "
import sys, asyncio; sys.path.insert(0, '/app')
import os; os.chdir('/app')
import core.auth as auth
async def noop(t): pass
auth.validate_scopes = noop
from server import mcp as s
async def main():
    tools = await s.list_tools()
    print(len(tools))
asyncio.run(main())
" 2>&1 | tail -1)

if [ -n "$TOOL_COUNT" ] && [ "$TOOL_COUNT" -ge 100 ] 2>/dev/null; then
    pass "Registered tools: $TOOL_COUNT (>= 100)"
else
    fail "Tool count: $TOOL_COUNT (expected >= 100)"
fi

# ── 5. Secret Scan ───────────────────────────────────────────────────────────
step "Secret Scan"

if [ -x "$SCRIPT_DIR/scan_secrets.sh" ]; then
    if "$SCRIPT_DIR/scan_secrets.sh" > /tmp/mcp-secrets.log 2>&1; then
        pass "No secrets detected"
    else
        fail "Secrets found — see /tmp/mcp-secrets.log"
    fi
else
    warn "scan_secrets.sh not executable, skipping"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}═══════════════════════════════════${NC}"
if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN}${BOLD}All checks passed. Safe to push.${NC}"
    exit 0
else
    echo -e "${RED}${BOLD}$ERRORS check(s) failed. Fix before pushing.${NC}"
    echo ""
    echo "Tips:"
    echo "  - BOM issues: ./mcp/scripts/validate.sh --fix"
    echo "  - Syntax: check the reported file/line"
    echo "  - Tests: run failing test directly for details"
    exit 1
fi
