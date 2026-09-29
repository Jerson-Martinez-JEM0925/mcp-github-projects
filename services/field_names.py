"""Shared normalization for GitHub Project field titles.

GitHub Project V2 field titles are user-editable. In practice teams often
prefix canonical names with an emoji or symbol (for example, ``📊 Priority``).
All callers must use the same exact, case-insensitive, and unambiguous
resolution rules so validation, defaults, reads, and writes agree.
"""

from __future__ import annotations

import re

from models.metadata import ProjectField, ProjectMetadata

_LEADING_SYMBOLS = re.compile(r"^[\W_]+", re.UNICODE)


def bare_field_name(name: str) -> str:
    """Return a field title without a leading symbol prefix."""
    return _LEADING_SYMBOLS.sub("", name or "").strip().casefold()


def resolve_field_name(
    metadata: ProjectMetadata, requested: str
) -> str | None:
    """Resolve a requested title to one actual metadata field name.

    Exact and case-insensitive matches win. A leading emoji/symbol prefix is
    ignored only when it produces one unambiguous candidate; ambiguous
    matches return ``None`` instead of guessing.
    """
    requested = (requested or "").strip()
    if not requested:
        return None
    if requested in metadata.fields:
        return requested

    folded = requested.casefold()
    case_matches = [name for name in metadata.fields if name.casefold() == folded]
    if len(case_matches) == 1:
        return case_matches[0]

    target = bare_field_name(requested)
    if not target:
        return None
    prefix_matches = [
        name for name in metadata.fields if bare_field_name(name) == target
    ]
    return prefix_matches[0] if len(prefix_matches) == 1 else None


def resolve_field(
    metadata: ProjectMetadata, requested: str
) -> ProjectField | None:
    """Return the uniquely resolved field definition, if any."""
    name = resolve_field_name(metadata, requested)
    return metadata.fields.get(name) if name is not None else None
