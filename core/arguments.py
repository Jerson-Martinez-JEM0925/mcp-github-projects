"""Uniform tool-argument convention (issue #4).

Historically most tools took a single ``params: <Model>`` argument, so callers
had to send ``{"params": {...}}``, while a handful took flat keyword arguments
and rejected the wrapper. Clients had to know, per tool, which shape to use.

The convention is now: **every tool accepts flat arguments**. For backward
compatibility a legacy ``{"params": {...}}`` wrapper is still accepted and
unwrapped, on every tool. The advertised JSON schema is the flat one (the
tool's real fields, with their real ``required`` list) plus an optional,
deprecated ``params`` object.

Validation still runs against the tool's original signature and models, so
field types, constraints and model validators are unchanged; only the
envelope is normalized. A validation failure raises
:class:`ToolArgumentError`, whose message names the expected shape.
"""

from __future__ import annotations

import copy
import inspect
from typing import Any, Callable

import types
import typing

from fastmcp.server.dependencies import without_injected_parameters
from fastmcp.utilities.types import get_cached_typeadapter
from pydantic import BaseModel, ValidationError

WRAPPER_KEY = "params"

_LEGACY_WRAPPER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Deprecated legacy wrapper. Pass the fields above directly (flat); "
        "a {\"params\": {...}} object is still accepted and unwrapped."
    ),
}


class ToolArgumentError(ValueError):
    """Arguments did not match the tool's schema (message names the shape)."""


def _wrapped_model(fn: Callable[..., Any]) -> type[BaseModel] | None:
    """Return the model when ``fn`` takes exactly one ``params: Model`` arg."""
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if len(params) != 1 or params[0].name != WRAPPER_KEY:
        return None
    try:
        hints = inspect.get_annotations(inspect.unwrap(fn), eval_str=True)
    except Exception:  # noqa: BLE001 - unresolvable hint: treat as flat
        return None
    annotation = hints.get(WRAPPER_KEY, params[0].annotation)
    # Accept both `params: Model` and `params: Model | None = None`.
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        members = [a for a in typing.get_args(annotation) if a is not type(None)]
        annotation = members[0] if len(members) == 1 else None
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _flat_schema(original: dict[str, Any], wrapped: bool) -> dict[str, Any]:
    """Advertised schema: the tool's own fields, flat, plus legacy ``params``."""
    schema = copy.deepcopy(original)
    if wrapped:
        inner = schema.get("properties", {}).get(WRAPPER_KEY, {})
        if "anyOf" in inner:  # Optional[Model]: keep the model branch
            inner = next((b for b in inner["anyOf"] if b.get("type") != "null"), {})
        defs = schema.get("$defs", {})
        ref = inner.get("$ref", "")
        if ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            inner = copy.deepcopy(defs.get(name, {}))
            # Drop the now-inlined definition unless something else uses it.
            rest = {k: v for k, v in defs.items() if k != name}
            if f"#/$defs/{name}" not in str(rest) and f"#/$defs/{name}" not in str(inner):
                defs = rest
        schema = {
            "type": "object",
            "properties": copy.deepcopy(inner.get("properties", {})),
        }
        if inner.get("required"):
            schema["required"] = list(inner["required"])
        if defs:
            schema["$defs"] = defs
        for key in ("additionalProperties", "description"):
            if key in inner:
                schema[key] = inner[key]
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema["properties"][WRAPPER_KEY] = dict(_LEGACY_WRAPPER_SCHEMA)
    return schema


def _error_message(tool: str, exc: ValidationError, fields: list[str]) -> str:
    problems = []
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ()) if p != WRAPPER_KEY]
        where = ".".join(loc) or "arguments"
        problems.append(f"{where}: {err.get('msg')}")
    example = ", ".join(f'"{f}": …' for f in fields[:3])
    return (
        f"Invalid arguments for {tool}: {'; '.join(problems)}. "
        f"Pass arguments flat, e.g. {{{example}}}; a legacy "
        f'{{"params": {{...}}}} wrapper is also accepted.'
    )


def accept_flat_or_wrapped(
    fn: Callable[..., Any], original_schema: dict[str, Any], tool: str
) -> tuple[Callable[..., Any], dict[str, Any]]:
    """Return ``(callable, advertised_schema)`` implementing the convention.

    ``original_schema`` is the input schema FastMCP derives from ``fn``. The
    returned callable has a synthetic keyword-only signature where every
    advertised field is optional at the transport level; the real
    required/typing rules are enforced by validating against ``fn`` itself.
    """
    model = _wrapped_model(fn)
    wrapped = model is not None
    schema = _flat_schema(original_schema, wrapped)
    field_names = [n for n in schema["properties"] if n != WRAPPER_KEY]
    adapter = get_cached_typeadapter(without_injected_parameters(fn))

    async def normalized(**kwargs: Any) -> Any:
        legacy = kwargs.pop(WRAPPER_KEY, None)
        flat = {k: v for k, v in kwargs.items() if v is not None}
        if legacy is not None and not isinstance(legacy, dict):
            raise ToolArgumentError(
                f"Invalid arguments for {tool}: 'params' must be an object "
                "when used; prefer passing the fields flat."
            )
        data = {**(legacy or {}), **flat}
        call = {WRAPPER_KEY: data} if wrapped else data
        try:
            result = adapter.validate_python(call)
        except ValidationError as exc:
            raise ToolArgumentError(_error_message(tool, exc, field_names)) from None
        if inspect.isawaitable(result):
            result = await result
        return result

    parameters = [
        inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Any)
        for name in [*field_names, WRAPPER_KEY]
    ]
    normalized.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    normalized.__annotations__ = {p.name: Any for p in parameters}
    normalized.__name__ = getattr(fn, "__name__", tool)
    normalized.__qualname__ = normalized.__name__
    normalized.__doc__ = fn.__doc__
    normalized.__module__ = getattr(fn, "__module__", __name__)
    return normalized, schema
