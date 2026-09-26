"""Count registered MCP tools and verify minimum threshold.

Tool registration is now gated by MCP_ACCESS_LEVEL (issue #34), so this
structural check seeds a dummy target and MCP_ACCESS_LEVEL=full before importing
the server. That makes the count deterministic and independent of any real
target/.env (CI runs this against the image with no env-file) and verifies the
FULL tool surface — the widest set the server can expose.
"""

import asyncio
import sys

sys.path.insert(0, "/app")
import os

os.chdir("/app")

# Seed a dummy target + widest access level so the count is deterministic and
# does not require a real .env. These are structural placeholders only; no
# GitHub API call is made by list_tools().
os.environ.setdefault("GH_PROJECT_ORG_NAME", "count-check")
os.environ.setdefault("GH_PROJECT_REPO_NAME", "count-check")
os.environ.setdefault("GH_PROJECT_PROJECT_NUMBER", "1")
os.environ["MCP_ACCESS_LEVEL"] = "full"

import core.auth as auth


async def noop(_: str) -> None:
    pass


# Bypass scope validation for structural check
auth.validate_scopes = noop

from server import mcp as server

MINIMUM_TOOLS = 100


async def main() -> None:
    tools = await server.list_tools()
    count = len(tools)
    print(f"Tools registered: {count}")

    if count >= MINIMUM_TOOLS:
        print(f"✅ Passed (>= {MINIMUM_TOOLS})")
    else:
        print(f"❌ Failed — expected >= {MINIMUM_TOOLS}, got {count}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
