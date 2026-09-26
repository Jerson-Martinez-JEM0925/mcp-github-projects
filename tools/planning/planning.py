"""MCP tools for sprint planning and release management.

Exposes tools for:
- Sprint planning: auto-distribute unassigned issues between team members
- Release notes generation: compile closed issues into formatted release notes
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from clients.gh_cli_client import CLIError
from core.config import get_settings
from core.error_handling import build_error_response, handle_tool_error
from models.responses import ToolSuccess
from core.factory import get_service_factory

logger = logging.getLogger(__name__)


class SprintPlanningInput(BaseModel):
    """Input schema for the sprint_planning tool."""

    milestone_title: str = Field(description="Milestone/sprint to plan")
    team_members: list[str] = Field(description="GitHub usernames to distribute work between")
    strategy: str = Field(
        default="balanced",
        description="Strategy: 'balanced' (equal split), 'backend_frontend' (by label), 'round_robin'",
    )


class GenerateReleaseNotesInput(BaseModel):
    """Input schema for the generate_release_notes tool."""

    milestone_title: str = Field(description="Milestone to generate notes for")
    version: Optional[str] = Field(default=None, description="Version tag (e.g., 'v2.1.0')")
    include_contributors: bool = Field(default=True, description="Include contributor list")
    group_by_label: bool = Field(default=True, description="Group entries by label category")


async def sprint_planning(params: SprintPlanningInput) -> dict:
    """Auto-distribute sprint issues between team members.

    Strategies:
    - balanced: Equal number per person (load-aware)
    - backend_frontend: Backend labels → member[0], Frontend → member[1]
    - round_robin: Alternate assignment

    Args:
        params: Input with milestone, team members, and strategy.

    Returns:
        ToolSuccess with distribution plan and workload summary.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        result = await gh_client.run([
            "issue", "list", "--repo", repo, "--milestone", params.milestone_title,
            "--state", "open", "--limit", "100",
            "--json", "number,title,assignees,labels",
        ])
        issues = json.loads(result.stdout)

        if not issues:
            return build_error_response(
                error_type="not_found",
                message=f"No open issues in milestone '{params.milestone_title}'.",
                suggestion="Verify the milestone has open issues.",
            )

        assigned_issues = [i for i in issues if i.get("assignees")]
        unassigned_issues = [i for i in issues if not i.get("assignees")]

        workload: dict[str, list[dict]] = {m: [] for m in params.team_members}
        for issue in assigned_issues:
            for assignee in issue.get("assignees", []):
                login = assignee.get("login", "")
                if login in workload:
                    workload[login].append({"number": issue["number"], "title": issue["title"]})

        plan: dict[str, list[dict]] = {m: [] for m in params.team_members}

        if params.strategy == "backend_frontend":
            backend_labels = {"🖥️ BackEnd", "🏗️ Infrastructure", "🔒 Security"}
            frontend_labels = {"🎨 FrontEnd", "🎯 UX/Polish", "📱 Mobile"}
            be_member = params.team_members[0]
            fe_member = params.team_members[1] if len(params.team_members) > 1 else params.team_members[0]

            for issue in unassigned_issues:
                issue_labels = {l.get("name", "") for l in issue.get("labels", [])}
                entry = {"number": issue["number"], "title": issue["title"]}
                if issue_labels & backend_labels:
                    plan[be_member].append(entry)
                elif issue_labels & frontend_labels:
                    plan[fe_member].append(entry)
                else:
                    target = min(params.team_members, key=lambda m: len(workload[m]) + len(plan[m]))
                    plan[target].append(entry)

        elif params.strategy == "round_robin":
            for idx, issue in enumerate(unassigned_issues):
                member = params.team_members[idx % len(params.team_members)]
                plan[member].append({"number": issue["number"], "title": issue["title"]})

        else:  # balanced
            for issue in sorted(unassigned_issues, key=lambda i: i["number"]):
                target = min(params.team_members, key=lambda m: len(workload[m]) + len(plan[m]))
                plan[target].append({"number": issue["number"], "title": issue["title"]})

        summary: dict[str, dict] = {}
        for member in params.team_members:
            summary[member] = {
                "already_assigned": len(workload[member]),
                "newly_planned": len(plan[member]),
                "total": len(workload[member]) + len(plan[member]),
                "new_issues": plan[member],
            }

        return ToolSuccess(
            data={
                "milestone": params.milestone_title,
                "strategy": params.strategy,
                "total_issues": len(issues),
                "already_assigned": len(assigned_issues),
                "unassigned": len(unassigned_issues),
                "distribution": summary,
                "message": (
                    f"{len(unassigned_issues)} issues distributed across "
                    f"{len(params.team_members)} members ('{params.strategy}')."
                ),
                "next_step": "Use bulk_assign to apply the planned assignments.",
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in sprint_planning: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Sprint planning failed: {exc.stderr.strip()}",
            suggestion="Verify the milestone exists and has issues.",
        )
    except Exception as exc:
        logger.error("Error in sprint_planning: %s", exc)
        return handle_tool_error(exc, context="Sprint planning failed")


async def generate_release_notes(params: GenerateReleaseNotesInput) -> dict:
    """Generate formatted release notes from closed issues in a milestone.

    Groups issues by label category and includes contributors.

    Args:
        params: Input with milestone, version, and formatting options.

    Returns:
        ToolSuccess with markdown release notes.
    """
    try:
        await get_service_factory().ensure_auth()
        settings = get_settings()
        repo = f"{settings.org_name}/{settings.repo_name}"
        gh_client = get_service_factory().gh()

        result = await gh_client.run([
            "issue", "list", "--repo", repo, "--milestone", params.milestone_title,
            "--state", "closed", "--limit", "200",
            "--json", "number,title,labels,assignees,closedAt",
        ])
        issues = json.loads(result.stdout)

        if not issues:
            return build_error_response(
                error_type="not_found",
                message=f"No closed issues in milestone '{params.milestone_title}'.",
                suggestion="Ensure the milestone has closed issues.",
            )

        version = params.version or f"v{datetime.now(timezone.utc).strftime('%Y.%m.%d')}"

        categories: dict[str, list[dict]] = {
            "🚀 Features": [],
            "🩹 Bug Fixes": [],
            "⚡ Performance": [],
            "🔒 Security": [],
            "🏗️ Infrastructure": [],
            "📝 Documentation": [],
            "🧪 Testing": [],
            "Other": [],
        }

        label_map = {
            "🚀 Feature": "🚀 Features", "💡Ideas": "🚀 Features",
            "🎨 FrontEnd": "🚀 Features", "🖥️ BackEnd": "🚀 Features",
            "📱 Mobile": "🚀 Features", "🎯 UX/Polish": "🚀 Features",
            "👥 Community": "🚀 Features", "🤖 AI/Premium": "🚀 Features",
            "📊 Analytics": "🚀 Features",
            "🩹 Fix": "🩹 Bug Fixes",
            "⚡ Performance": "⚡ Performance",
            "🔒 Security": "🔒 Security",
            "🏗️ Infrastructure": "🏗️ Infrastructure",
            "📝 Documentation": "📝 Documentation",
            "🧪 Testing": "🧪 Testing",
        }

        contributors: set[str] = set()

        for issue in issues:
            issue_labels = [l.get("name", "") for l in issue.get("labels", [])]
            assignees = [a.get("login", "") for a in issue.get("assignees", [])]
            contributors.update(assignees)

            entry = {"number": issue["number"], "title": issue["title"], "assignees": assignees}

            categorized = False
            if params.group_by_label:
                for label in issue_labels:
                    if label in label_map:
                        categories[label_map[label]].append(entry)
                        categorized = True
                        break
            if not categorized:
                categories["Other"].append(entry)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = [
            f"# Release {version}",
            "", f"**Date:** {today}",
            f"**Milestone:** {params.milestone_title}",
            f"**Issues resolved:** {len(issues)}", "",
        ]

        for category, items in categories.items():
            if not items:
                continue
            lines.append(f"## {category}")
            lines.append("")
            for item in items:
                assignee_str = f" (@{', @'.join(item['assignees'])})" if item["assignees"] else ""
                lines.append(f"- {item['title']} (#{item['number']}){assignee_str}")
            lines.append("")

        if params.include_contributors and contributors:
            lines.extend(["## Contributors", ""])
            for c in sorted(contributors):
                lines.append(f"- @{c}")
            lines.append("")

        markdown = "\n".join(lines)

        return ToolSuccess(
            data={
                "version": version,
                "milestone": params.milestone_title,
                "issues_count": len(issues),
                "contributors": sorted(contributors),
                "categories_used": [k for k, v in categories.items() if v],
                "markdown": markdown,
            },
        ).model_dump()

    except CLIError as exc:
        logger.error("CLI error in generate_release_notes: %s", exc)
        return build_error_response(
            error_type="internal",
            message=f"Failed to generate release notes: {exc.stderr.strip()}",
            suggestion="Verify the milestone exists and has closed issues.",
        )
    except Exception as exc:
        logger.error("Error in generate_release_notes: %s", exc)
        return handle_tool_error(exc, context="Generate release notes failed")
