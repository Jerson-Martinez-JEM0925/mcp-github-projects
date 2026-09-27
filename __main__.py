"""Allow running the MCP server as `python .` from the repository root."""

from core.config import load_settings_or_exit

if __name__ == "__main__":
    # Validate configuration before importing server, whose tool registration
    # reads settings at import time: misconfiguration exits cleanly (#3).
    load_settings_or_exit()

    from server import main

    main()
