"""Change Working Directory built-in tool.

Switches ``_working_directory`` for the current conversation only. The override
is held in :class:`app.utils.context_storage.ContextStorage` keyed by the
calling ``context_id`` and read back by :mod:`app.tools.builtin.adapter` (and
the reasoning agent's prompt) on every subsequent step.

Allowed targets:

- ``user_working`` -- the profile-global default (``get_user_working_directory()``).
  Selecting this clears any prior override so the conversation tracks the live
  default again.
- ``skills``       -- ``<CREMIND_SYSTEM_DIR>/<profile>/skills``.
- ``documents``    -- ``<CREMIND_SYSTEM_DIR>/<profile>/documents``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.config.settings import BaseConfig, get_user_working_directory
from app.events import get_event_stream_bus
from app.skills.sync import profile_skills_dir
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.context_storage import clear_context, get_context, set_context
from app.utils.logger import logger

OVERRIDE_KEY = "_working_directory_override"

# Must match the constant of the same name in ``app.agent.reasoning_agent``.
# The Reasoning Agent mirrors its in-memory ``_loaded_skill_ids`` set into
# ContextStorage under this key so the per-request ``prepare_tools`` callback
# below can read it without holding a reference to the agent instance.
LOADED_SKILLS_KEY = "_loaded_skill_ids"

SERVER_NAME = "Change Working Directory"

_TARGETS = ("user_working", "skills", "documents", "custom")


def _looks_like_skill_id(target: str) -> bool:
    """Skill IDs are ``<profile>__<slug>``; rule out the static targets first."""
    return target not in _TARGETS and "__" in target


def _get_skill_row(target: str) -> Optional[Dict[str, Any]]:
    """Fetch the tools row for a skill tool_id, or ``None`` if absent."""
    try:
        from app.storage.tool_storage import ToolStorage
        row = ToolStorage().get_tool(target)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"change_working_directory: skill lookup failed for {target}: {exc}")
        return None
    if not row or row.get("tool_type") != "skill":
        return None
    return row


def _resolve_skill_dir(target: str) -> Optional[Path]:
    """Look up a skill's on-disk source directory by its tool_id."""
    row = _get_skill_row(target)
    if not row:
        return None
    source = row.get("source")
    if not source:
        return None
    return Path(source)


def _resolve_target(
    target: str,
    profile: str,
    custom_path: Optional[str] = None,
) -> Optional[Path]:
    if target == "skills":
        return profile_skills_dir(profile)
    if target == "documents":
        return Path(BaseConfig.CREMIND_SYSTEM_DIR) / profile / "documents"
    if target == "user_working":
        return Path(get_user_working_directory())
    if target == "custom":
        if not custom_path:
            return None
        return Path(custom_path).expanduser().resolve()
    if _looks_like_skill_id(target):
        return _resolve_skill_dir(target)
    return None


TOOL_CONFIG: ToolConfig = {
    "name": "change_working_directory",
    "display_name": "Change Working Directory",
    "default_model_group": "low",
    "hidden": True,
    "llm_parameters": {
        "tool_instructions": (
            "Always switch the active working directory before executing any "
            "commands relevant to the user's files or skills. This ensures "
            "the agent is operating in the correct context.\n"
            "Supported targets are user working (the profile default), "
            "documents and skills, it can be skills or skill name "
            "strings (e.g. 'my weather skill' to switch to a "
            "specific skill's source dir). \n"
            "If the user names an arbitrary directory (e.g. 'switch to "
            "C:\\\\Code\\\\foo' or '~/projects/acme'), call with "
            "target='custom' and path=<absolute path>. The path must already "
            "exist; it will not be auto-created.\n"
            "E.g. 'change to user working directory', 'switch to documents folder', 'change to my weather skill' (if 'my weather skill' is a loaded skill), 'change to C:\\\\Users\\\\me\\\\projects\\\\acme'."
        ),
        "system_prompt": (
            "Don't answer any questions or provide any information. "
            "Always call the 'change_working_directory' tool with the requested target."
        ),
    },
}


class ChangeWorkingDirectoryTool(BuiltInTool):
    name: str = "change_working_directory"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "enum": list(_TARGETS),
                "description": (
                    "Which directory to switch to: 'user_working' (profile "
                    "default; clears any override), 'documents', 'skills', "
                    "or 'custom' for an arbitrary user-supplied directory."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Required when target='custom'. Absolute directory "
                    "path the agent should operate in. Must already exist."
                ),
            },
        },
        "required": ["target"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        target = arguments.get("target")
        if not isinstance(target, str) or not target:
            return BuiltInToolResult(
                content=[{"type": "text", "text": (
                    f"target must be one of {list(_TARGETS)} or a loaded skill name"
                )}]
            )

        context_id = arguments.get("_context_id") or ""
        if not context_id:
            return BuiltInToolResult(
                content=[{"type": "text", "text": (
                    "Cannot change working directory: missing conversation context."
                )}]
            )

        profile = arguments.get("_profile") or "default"
        custom_path = arguments.get("path")

        # Reject unknown skill IDs (defense in depth — even if the inner LLM
        # hallucinates a skill_id outside the dynamic enum, only currently
        # loaded skill IDs are allowed through).
        if target not in _TARGETS:
            loaded = get_context(context_id, LOADED_SKILLS_KEY) or []
            if target not in loaded:
                return BuiltInToolResult(
                    content=[{"type": "text", "text": (
                        f"target '{target}' is not a valid working directory option. "
                        f"Allowed: {list(_TARGETS) + list(loaded)}"
                    )}]
                )

        # Up-front validation for the custom branch: the LLM must supply an
        # absolute path that already exists. We deliberately do NOT mkdir
        # here — auto-creating arbitrary user-supplied paths would silently
        # materialise typos (`C:\Codee` instead of `C:\Code`).
        if target == "custom":
            if not isinstance(custom_path, str) or not custom_path.strip():
                return BuiltInToolResult(
                    content=[{"type": "text", "text": (
                        "target='custom' requires a non-empty 'path' argument."
                    )}]
                )
            expanded = os.path.expanduser(custom_path)
            if not os.path.isabs(expanded):
                return BuiltInToolResult(
                    content=[{"type": "text", "text": (
                        f"path '{custom_path}' must be an absolute directory path."
                    )}]
                )

        previous = (
            get_context(context_id, OVERRIDE_KEY)
            or get_user_working_directory()
        )
        new_path = _resolve_target(target, profile, custom_path=custom_path)
        if new_path is None:
            return BuiltInToolResult(
                content=[{"type": "text", "text": (
                    f"Could not resolve directory for target '{target}'."
                )}]
            )

        # ``persist_path`` is what we mirror to the durable
        # ``conversations.working_directory`` column. ``None`` for
        # ``user_working`` so reopening the conversation falls back to the
        # live profile default rather than pinning today's resolved value.
        persist_path: str | None
        if target == "user_working":
            clear_context(context_id, OVERRIDE_KEY)
            persist_path = None
        elif target == "custom":
            if not new_path.exists() or not new_path.is_dir():
                return BuiltInToolResult(
                    content=[{"type": "text", "text": (
                        f"Custom path '{new_path}' does not exist or is not a directory."
                    )}]
                )
            set_context(context_id, OVERRIDE_KEY, str(new_path))
            persist_path = str(new_path)
        else:
            new_path.mkdir(parents=True, exist_ok=True)
            set_context(context_id, OVERRIDE_KEY, str(new_path))
            persist_path = str(new_path)

        new_str = str(new_path)

        # Persist alongside the in-memory ContextStorage write so the
        # override survives server restart. Failures are logged but don't
        # abort the tool call — the in-memory value still drives the
        # current run.
        try:
            from app.events.runner import get_conversation_storage
            from app.utils.working_directory import persist_working_directory
            await persist_working_directory(
                context_id, persist_path, get_conversation_storage(),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to persist cwd override for %s", context_id
            )

        # Broadcast the cwd change on the conversation's SSE stream so any
        # subscribed UI (Vue file-tree pane, Go CLI tree) re-renders against
        # the new directory. ``context_id`` is the conversation_id here.
        try:
            await get_event_stream_bus().publish(
                context_id,
                "cwd",
                {"working_directory": new_str},
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to publish cwd change for %s", context_id)

        return BuiltInToolResult(
            content=[{
                "type": "text",
                "text": f"Working directory switched to {new_str}",
            }],
            structured_content={
                "target": target,
                "previous": str(previous),
                "current": new_str,
            },
        )


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [ChangeWorkingDirectoryTool()]


def create_prepare_tools() -> Callable:
    """Build a ``prepare_tools`` callback that injects loaded skill IDs into
    the ``target`` enum of the ``change_working_directory`` schema.

    The Reasoning Agent mirrors its current ``_loaded_skill_ids`` set into
    ContextStorage under ``LOADED_SKILLS_KEY``. We read that mirror keyed by
    the active ``context_id`` and append the IDs to the static enum so the
    inner adapter LLM can pick a skill-specific directory. When the loop ends
    (Final Answer / Casual Chat) the agent clears the mirror, so subsequent
    requests see only the static targets — i.e. the enum auto-resets.
    """

    def prepare_tools(
        query: str,  # noqa: ARG001
        tools: List[Dict[str, Any]],
        *,
        arguments: Optional[Dict[str, Any]] = None,  # noqa: ARG001
        context_id: Optional[str] = None,
        profile: Optional[str] = None,  # noqa: ARG001
        **_: Any,
    ) -> List[Dict[str, Any]]:
        if not context_id:
            return tools
        loaded = get_context(context_id, LOADED_SKILLS_KEY) or []
        if not loaded:
            return tools
        # Resolve each loaded skill_id → display name so the inner LLM can
        # match the user's free-form phrasing ("python app test") against the
        # skill_id it must emit ("admin__python_app_test").
        name_lines: List[str] = []
        for skill_id in loaded:
            row = _get_skill_row(skill_id)
            display_name = (row or {}).get("name") or skill_id
            name_lines.append(f"{display_name}: {skill_id}")
        for tool in tools:
            fn = tool.get("function") or {}
            if fn.get("name") != "change_working_directory":
                continue
            params = fn.get("parameters") or {}
            props = params.get("properties") or {}
            target_prop = props.get("target")
            if isinstance(target_prop, dict):
                # When skills are loaded, drop the generic "skills" option:
                # the agent should switch into a specific loaded skill's
                # directory rather than the parent skills folder.
                base_targets = [t for t in _TARGETS if t != "skills"]
                target_prop["enum"] = base_targets + list(loaded)
                base_desc = target_prop.get("description") or ""
                target_prop["description"] = (
                    f"{base_desc}\n\nLoaded skills (skill name: skill id):\n"
                    + "\n".join(name_lines)
                ).strip()
            break
        return tools

    return prepare_tools


def get_prepare_tools() -> Optional[Callable]:
    """Module hook auto-detected by ``register_builtin_tools``.

    Returns the per-request ``prepare_tools`` callback that injects the
    dynamic enum. No external services needed (no ``vector_store`` arg).
    """
    return create_prepare_tools()
