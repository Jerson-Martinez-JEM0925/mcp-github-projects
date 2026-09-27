"""Board structure tools: single-select options and views (issue #26).

The public Project V2 GraphQL API now exposes ``updateProjectV2Field`` (with
``singleSelectOptions``) and ``createProjectV2View`` / ``updateProjectV2View``
— verified by schema introspection on 2026-09-27. Earlier they were missing,
which is why native Status columns could not be provisioned programmatically.

Safety model for options (the Status field drives the board's columns):

* ``set_field_options`` is a **plan by default** (``dry_run: true``) and
  returns the exact before → after option list.
* Existing options are matched by exact name, then by name without a leading
  emoji (``"Done"`` ↔ ``"✅ Done"``), and are sent back with their ``id`` so
  every item keeps its value, even when the option is renamed or recoloured.
* Existing options NOT in the requested list are **kept** (appended) unless
  ``remove_missing: true`` — and removing requires ``confirm: true``, because
  items holding a removed option lose that value.

All tools act on the configured project (``GH_PROJECT_*``); there is no
project override, so ``GH_PROJECT_SCOPE_LOCK`` needs no extra check.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field

from core.error_handling import build_error_response, handle_tool_error
from core.factory import get_service_factory
from graphql.mutations import (
    CREATE_PROJECT_VIEW_MUTATION,
    UPDATE_PROJECT_VIEW_FILTER_MUTATION,
    UPDATE_SINGLE_SELECT_OPTIONS_MUTATION,
)
from graphql.queries import PROJECT_VIEWS_QUERY, SINGLE_SELECT_FIELD_OPTIONS_QUERY
from models.responses import ToolSuccess
from services.project_service import bare_option_name as _bare_option_name

logger = logging.getLogger(__name__)

OptionColor = Literal["GRAY", "BLUE", "GREEN", "YELLOW", "ORANGE", "RED", "PINK", "PURPLE"]
_LAYOUTS = {"BOARD": "BOARD_LAYOUT", "TABLE": "TABLE_LAYOUT", "ROADMAP": "ROADMAP_LAYOUT"}


class OptionSpec(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Option name, e.g. '🛠 In Progress'.")
    color: Optional[OptionColor] = Field(default=None, description="Colour; keeps the current one (or GRAY) when omitted.")
    description: Optional[str] = Field(default=None, max_length=500, description="Description; keeps the current one when omitted.")
    match: Optional[str] = Field(
        default=None,
        description="Name of the EXISTING option this one replaces (to rename it). Defaults to `name`.",
    )


class SetFieldOptionsInput(BaseModel):
    field_name: str = Field(default="Status", min_length=1, description="Single-select field to change (default Status).")
    options: list[OptionSpec] = Field(..., min_length=1, max_length=50, description="Desired options, in board order.")
    remove_missing: bool = Field(
        default=False,
        description="Remove existing options not listed. Items holding them lose the value; needs confirm=true.",
    )
    dry_run: bool = Field(default=True, description="Return the plan without changing anything (default true).")
    confirm: bool = Field(default=False, description="Required with remove_missing=true when dry_run=false.")


class ListProjectViewsInput(BaseModel):
    pass


class CreateProjectViewInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="View name, e.g. 'Sprint board'.")
    layout: Literal["BOARD", "TABLE", "ROADMAP"] = Field(default="BOARD", description="View layout.")
    filter: Optional[str] = Field(default=None, max_length=1_000, description="Optional view filter, e.g. 'is:open label:bug'.")


async def _metadata():
    graphql_client = await get_service_factory().graphql()
    discovery = await get_service_factory().discovery_service(graphql_client)
    return graphql_client, discovery, await discovery.get_cached_or_discover()


def _plan(current: list[dict], params: SetFieldOptionsInput) -> tuple[list[dict], list[dict], list[str]]:
    """Return (final option inputs, removed options, problems)."""
    by_exact = {o["name"]: o for o in current}
    used: set[str] = set()
    final: list[dict] = []
    problems: list[str] = []
    for spec in params.options:
        key = spec.match or spec.name
        existing = by_exact.get(key)
        if existing is None:
            bare = [o for o in current if _bare_option_name(o["name"]) == _bare_option_name(key)]
            existing = bare[0] if len(bare) == 1 else None
        if existing is not None and existing["id"] in used:
            problems.append(f"option '{existing['name']}' is matched by more than one requested option")
            continue
        entry = {
            "name": spec.name,
            "color": spec.color or (existing or {}).get("color") or "GRAY",
            "description": spec.description if spec.description is not None else (existing or {}).get("description") or "",
        }
        if existing is not None:
            entry["id"] = existing["id"]
            used.add(existing["id"])
        final.append(entry)
    leftovers = [o for o in current if o["id"] not in used]
    removed: list[dict] = []
    if params.remove_missing:
        removed = leftovers
    else:
        final += [
            {"id": o["id"], "name": o["name"], "color": o.get("color") or "GRAY", "description": o.get("description") or ""}
            for o in leftovers
        ]
    # Compare without emoji/case too: "Done" and "✅ Done" side by side would
    # make bare-name matching (move_to_done) ambiguous.
    names = [_bare_option_name(o["name"]) for o in final]
    duplicates = sorted({o["name"] for o in final if names.count(_bare_option_name(o["name"])) > 1})
    if duplicates:
        problems.append(f"duplicate option names: {', '.join(duplicates)}")
    return final, removed, problems


async def set_field_options(params: SetFieldOptionsInput) -> dict:
    """Plan or apply a single-select field's options (e.g. the Status columns), preserving item values."""
    try:
        graphql_client, discovery, metadata = await _metadata()
        field = metadata.fields.get(params.field_name)
        if field is None or field.data_type != "SINGLE_SELECT":
            return build_error_response(
                error_type="validation",
                message=f"'{params.field_name}' is not a single-select field on this project.",
                suggestion="Run discover_ids to list the project's fields and their types.",
            )
        response = await graphql_client.execute_with_retry(
            SINGLE_SELECT_FIELD_OPTIONS_QUERY, {"fieldId": field.id}
        )
        current = ((response.get("data") or {}).get("node") or {}).get("options") or []
        final, removed, problems = _plan(current, params)
        if problems:
            return build_error_response(
                error_type="validation",
                message="Cannot build the option list: " + "; ".join(problems) + ".",
                suggestion="Give each option a unique name; use `match` to rename a specific existing option.",
            )
        plan = {
            "field": params.field_name,
            "before": [o["name"] for o in current],
            "after": [o["name"] for o in final],
            "kept": sum(1 for o in final if "id" in o),
            "added": [o["name"] for o in final if "id" not in o],
            "removed": [o["name"] for o in removed],
        }
        if params.dry_run:
            return ToolSuccess(
                data={**plan, "dry_run": True, "message": "Plan only; re-run with dry_run=false to apply."}
            ).model_dump()
        if removed and not params.confirm:
            return build_error_response(
                error_type="validation",
                message=(
                    f"Removing {len(removed)} option(s) ({', '.join(plan['removed'])}) clears that value "
                    "from every item holding it; this cannot be undone."
                ),
                suggestion="Re-run with confirm=true, or with remove_missing=false to keep them.",
            )
        result = await graphql_client.execute_with_retry(
            UPDATE_SINGLE_SELECT_OPTIONS_MUTATION,
            {"fieldId": field.id, "options": final},
            is_mutation=True,
        )
        updated = ((result.get("data") or {}).get("updateProjectV2Field") or {}).get("projectV2Field") or {}
        await discovery.discover(force=True)  # refresh cached option ids
        return ToolSuccess(
            data={
                **plan,
                "dry_run": False,
                "options": updated.get("options", []),
                "message": f"'{params.field_name}' now has {len(updated.get('options', final))} options.",
            }
        ).model_dump()
    except Exception as exc:  # noqa: BLE001
        logger.error("Error in set_field_options: %s", exc)
        return handle_tool_error(exc, context="Set field options failed")


async def list_project_views(params: ListProjectViewsInput | None = None) -> dict:
    """List the project's views (number, name, layout, filter)."""
    try:
        graphql_client, _, metadata = await _metadata()
        response = await graphql_client.execute_with_retry(PROJECT_VIEWS_QUERY, {"projectId": metadata.project_id})
        node = (response.get("data") or {}).get("node") or {}
        views = (node.get("views") or {}).get("nodes") or []
        return ToolSuccess(
            data={
                "views": [
                    {**view, "layout": str(view.get("layout", "")).removesuffix("_LAYOUT")} for view in views
                ],
                "count": len(views),
                "project_url": node.get("url"),
            }
        ).model_dump()
    except Exception as exc:  # noqa: BLE001
        logger.error("Error in list_project_views: %s", exc)
        return handle_tool_error(exc, context="List project views failed")


async def create_project_view(params: CreateProjectViewInput) -> dict:
    """Create a board, table or roadmap view on the project, optionally with a filter."""
    try:
        graphql_client, _, metadata = await _metadata()
        result = await graphql_client.execute_with_retry(
            CREATE_PROJECT_VIEW_MUTATION,
            {"projectId": metadata.project_id, "name": params.name, "layout": _LAYOUTS[params.layout]},
            is_mutation=True,
        )
        view = ((result.get("data") or {}).get("createProjectV2View") or {}).get("projectV2View") or {}
        if params.filter and view.get("id"):
            updated = await graphql_client.execute_with_retry(
                UPDATE_PROJECT_VIEW_FILTER_MUTATION,
                {"viewId": view["id"], "filter": params.filter},
                is_mutation=True,
            )
            view = ((updated.get("data") or {}).get("updateProjectV2View") or {}).get("projectV2View") or view
        return ToolSuccess(
            data={
                **view,
                "layout": str(view.get("layout", "")).removesuffix("_LAYOUT"),
                "message": f"View '{params.name}' created.",
            }
        ).model_dump()
    except Exception as exc:  # noqa: BLE001
        logger.error("Error in create_project_view: %s", exc)
        return handle_tool_error(exc, context="Create project view failed")


BOARD_STRUCTURE_TOOLS = [set_field_options, list_project_views, create_project_view]
