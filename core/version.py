"""Server release version (SemVer).

Single source of truth for the version: ``server_info`` reports it, the MCP
handshake advertises it, and ``scripts/release_notes.py`` refuses a release
tag that does not match it. Bump it in the release PR (see docs/RELEASING.md).
"""

VERSION = "1.1.1"
