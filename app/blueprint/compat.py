"""Reading older component documents.

A blueprint authored by an older build carries component documents at older
versions (``{"component", "version", "data"}``). The appliers and the plan
builders only ever see the CURRENT shape: :func:`upgrade_component` lifts an
older ``data`` blob to it, one version step at a time, right where
:func:`app.blueprint.plan.load_component` reads the document. A newer version
than this build writes never reaches here — :func:`app.blueprint.manifest.check_importable`
skips that component.

Pure data transforms (no storage, no server imports), so it works wherever a
component document can be read.
"""

from __future__ import annotations

from typing import Any

# tools v1 → v2: the two document searches swapped ids (the same swap the
# ``20260928_search_tool_ids`` migration applies to the database). Applied as
# ONE lookup per entry, never as sequential replaces — chained, the second
# rename would catch the first one's output and land Cremind's manual-search
# settings on the personal-document search.
TOOLS_V1_TO_V2_IDS: dict[str, str] = {
    "documentation_search": "cremind_documentation_search",
    "user_documents": "documentation_search",
}

# Only built-in entries were renamed. A v1 entry with no ``kind`` predates the
# field and was a built-in (a2a/mcp entries always carried theirs).
_BUILTIN_KINDS = (None, "builtin")


def _tools_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    tools = []
    for entry in data.get("tools") or []:
        if isinstance(entry, dict) and entry.get("kind") in _BUILTIN_KINDS:
            old = entry.get("tool_id")
            new = TOOLS_V1_TO_V2_IDS.get(old) if isinstance(old, str) else None
            if new is not None:
                # ``legacy_tool_id`` lets the applier still find secrets a
                # client keyed by the id printed in the old manifest.
                entry = {**entry, "tool_id": new, "legacy_tool_id": old}
        tools.append(entry)
    return {**data, "tools": tools}


# (component, from_version) → the step to from_version + 1.
_STEPS = {
    ("tools", 1): _tools_v1_to_v2,
}


def upgrade_component(key: str, version: int | None, data: dict[str, Any]) -> dict[str, Any]:
    """``data`` of a ``key`` document at ``version``, lifted to the current
    shape. A document without a version is version 1."""
    from app.blueprint.manifest import SUPPORTED_COMPONENT_VERSIONS

    try:
        current = int(version or 1)
    except (TypeError, ValueError):
        current = 1
    target = SUPPORTED_COMPONENT_VERSIONS.get(key, current)
    while current < target:
        step = _STEPS.get((key, current))
        if step is not None:
            data = step(data)
        current += 1
    return data


__all__ = ["TOOLS_V1_TO_V2_IDS", "upgrade_component"]
