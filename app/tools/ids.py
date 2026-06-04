"""Tool-ID generation utilities.

A ``tool_id`` is a globally unique, stable identifier derived from the
human-readable tool name. The reasoning agent uses tool_ids as the function
"Action" enum (so collisions between tool names from different sources can
never confuse dispatch).

Rules
-----
- ``slugify(name)`` lowercases, replaces non-alphanumeric runs with ``_``,
  trims leading/trailing underscores.
- ``allocate_unique_tool_id`` returns the slug as-is if available; otherwise
  appends ``_2``, ``_3`` ... until it lands on a free slot.
- ``allocate_fixed_tool_id`` raises :class:`ToolIdConflictError` on collision
  -- used for intrinsic and built-in tools whose names are defined in code.
"""

from __future__ import annotations

import re
from typing import Iterable


class ToolIdConflictError(RuntimeError):
    """Raised when a fixed (intrinsic/built-in) tool's slug collides."""


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Return a lowercase, ``_``-joined slug of ``name``.

    Examples
    --------
    >>> slugify("Markdown Converter")
    'markdown_converter'
    >>> slugify("HelloWorld!")
    'helloworld'
    >>> slugify("foo  bar//baz")
    'foo_bar_baz'
    """
    if not name:
        return "_"
    slug = _SLUG_RE.sub("_", name.lower()).strip("_")
    return slug or "_"


def allocate_fixed_tool_id(name: str, taken: Iterable[str]) -> str:
    """Return ``slugify(name)`` or raise if it is already taken.

    Used for intrinsic and built-in tools whose names are defined in source
    code -- a collision is a programmer error and must surface immediately.
    """
    slug = slugify(name)
    if slug in set(taken):
        raise ToolIdConflictError(
            f"Tool id '{slug}' (derived from '{name}') already registered. "
            "Intrinsic and built-in tool names must be globally unique."
        )
    return slug


def allocate_unique_tool_id(name: str, taken: Iterable[str]) -> str:
    """Return a unique slug for ``name``, suffixing ``_2``, ``_3`` ... if needed.

    Used for skills, MCP servers, and A2A agents whose names come from
    external sources we cannot control.
    """
    base = slugify(name)
    taken_set = set(taken)
    if base not in taken_set:
        return base
    n = 2
    while f"{base}_{n}" in taken_set:
        n += 1
    return f"{base}_{n}"
