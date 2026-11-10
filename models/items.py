"""Pydantic models for GitHub Project V2 items.

Contains the ProjectItem model representing a card on the project board,
backed by either an Issue or a DraftIssue, and the CreateItemInput model
for validated item creation requests.
"""

from pydantic import BaseModel, Field


class ProjectItem(BaseModel):
    """A single item on the GitHub Project V2 board.

    Represents either an Issue or DraftIssue with all project-level
    custom field values resolved.
    """

    node_id: str = Field(description="Project item node ID")
    content_type: str = Field(description="'Issue', 'PullRequest', or 'DraftIssue'")
    title: str = Field(
        min_length=1, max_length=256, description="Item title (1-256 characters)"
    )
    body: str = Field(
        default="", max_length=65536, description="Item body content (0-65536 chars)"
    )
    issue_number: int | None = Field(
        default=None, description="Issue number (None for DraftIssues)"
    )
    issue_url: str | None = Field(
        default=None, description="Issue URL (None for DraftIssues)"
    )
    status: str | None = Field(default=None, description="Status field value")
    priority: str | None = Field(default=None, description="Priority field value")
    milestone: str | None = Field(default=None, description="Milestone field value")
    due_date: str | None = Field(
        default=None, description="Due date in ISO 8601 format (YYYY-MM-DD)"
    )
    estimate: float | None = Field(
        default=None, description="Estimate value in hours"
    )
    assignees: list[str] = Field(
        default_factory=list, description="List of assigned GitHub usernames"
    )
    labels: list[str] = Field(default_factory=list, description="List of label names")
    content_state: str | None = Field(
        default=None,
        description="Content state of the linked Issue/PR: OPEN, CLOSED, or MERGED (None for DraftIssues)",
    )
    fields: dict[str, str | float | None] = Field(
        default_factory=dict,
        description=(
            "Every project field value set on the item, keyed by the field's display name "
            "(single-select option name, date as YYYY-MM-DD, number, text or milestone title). "
            "Includes custom fields that have no dedicated attribute."
        ),
    )


class CreateItemInput(BaseModel):
    """Validated input for creating a new project item.

    Enforces title length (1-256 chars) and body length (0-65536 chars)
    constraints as required by the GitHub API and spec requirements.
    """

    title: str = Field(
        min_length=1, max_length=256, description="Item title (1-256 characters)"
    )
    body: str = Field(
        default="",
        max_length=65536,
        description="Item body content (0-65536 characters)",
    )
    status: str | None = Field(default=None, description="Initial status value")
    priority: str | None = Field(default=None, description="Initial priority value")
    milestone: str | None = Field(default=None, description="Initial milestone value")
    due_date: str | None = Field(
        default=None, description="Initial due date (YYYY-MM-DD)"
    )
    assignees: list[str] = Field(
        default=[], description="Initial list of GitHub usernames to assign"
    )
    labels: list[str] = Field(
        default=[], description="Initial list of label names"
    )
