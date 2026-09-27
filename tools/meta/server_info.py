"""Diagnostics tool: server_info (issue #34).

Returns safe, credential-free server metadata so an MCP client can see the
effective access level and scope lock without probing. Mirrors the
``server_info`` tool in mcp-monday-projects (which reports its write policy and
workspace scope); here it reports ``MCP_ACCESS_LEVEL`` and
``GH_PROJECT_SCOPE_LOCK`` plus the configured target.

No token, header, or secret is ever included in the response.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from core.access import AccessLevel, level_counts, tools_for_level
from core.config import get_settings
from core.error_handling import handle_tool_error
from core.version import VERSION
from models.responses import ToolSuccess

logger = logging.getLogger(__name__)


class ServerInfoInput(BaseModel):
    """Input schema for server_info (no arguments)."""


async def server_info(params: ServerInfoInput) -> dict:
    """Return safe server metadata: access level, scope lock, and target.

    The response includes:
      - access_level: the effective MCP_ACCESS_LEVEL (read | write | full).
      - access_level_meaning: a one-line gloss of what it exposes.
      - tool_exposure: classified/exposed/hidden counts, including how many
        permanent-delete tools are hidden at this level.
      - scope_lock: whether GH_PROJECT_SCOPE_LOCK confines the server, plus the
        variable name and the target it is (or would be) locked to.
      - target: the configured org / repo / project (no credentials).
    """
    try:
        settings = get_settings()
        level = AccessLevel.parse(settings.access_level)

        meaning = {
            AccessLevel.READ: "Only read-only tools are exposed.",
            AccessLevel.WRITE: (
                "Read + write tools (create/update/close/archive); no "
                "permanent-delete tools."
            ),
            AccessLevel.FULL: (
                "All tools, including permanent-delete tools (each requires "
                "confirm=true)."
            ),
        }[level]

        counts = level_counts(level)

        target = {
            "org_name": settings.org_name,
            "repo_name": settings.repo_name,
            "repository": f"{settings.org_name}/{settings.repo_name}",
            "project_number": settings.project_number,
            "owner_type": settings.owner_type,
        }

        scope_lock = {
            "enabled": bool(settings.scope_lock),
            "variable": "GH_PROJECT_SCOPE_LOCK",
            "confined_to": target if settings.scope_lock else None,
            "meaning": (
                "Every tool is confined to the configured org/repo/project; "
                "other targets are refused."
                if settings.scope_lock
                else "Off — the server is as wide as the token allows."
            ),
        }

        return ToolSuccess(
            data={
                "server": "github-project-management",
                "version": VERSION,
                "access_level": level.value,
                "access_level_variable": "MCP_ACCESS_LEVEL",
                "access_level_meaning": meaning,
                "tool_exposure": counts,
                "exposed_tools": tools_for_level(level),
                "scope_lock": scope_lock,
                "target": target,
            },
        ).model_dump()

    except Exception as exc:
        logger.error("Error in server_info: %s", exc)
        return handle_tool_error(exc, context="server_info failed")
