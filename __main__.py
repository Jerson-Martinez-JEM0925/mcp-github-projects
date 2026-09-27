"""Allow running the MCP server as `python -m app.mcp.github_project`."""

from core.config import load_settings_or_exit

if __name__ == "__main__":
    # Validate configuration before importing server, whose tool registration
    # reads settings at import time: misconfiguration exits cleanly (#3).
    load_settings_or_exit()

    from server import main

    main()
