"""File-watcher subtools for the System File built-in tool.

Defines the three file-watcher functions — :class:`RegisterFileWatcherTool`,
:class:`ListFileWatchersTool`, :class:`DeleteFileWatcherTool` — that are folded
into the ``system_file`` tool group's ``get_tools()`` factory. This module is
no longer a standalone built-in tool (it has no ``TOOL_CONFIG`` / ``SERVER_NAME``
/ ``get_tools`` and is not listed in ``_BUILTIN_MODULE_NAMES``); it is a pure
implementation home for those subtools.

``RegisterFileWatcherTool`` records a (conversation, root_path, event_types,
filters, action) tuple so that whenever the watchdog ``Observer`` mounted at
``root_path`` reports a matching filesystem event, the reasoning agent re-runs
``action`` with a synthetic trigger payload describing the event — and streams
the result into the conversation.

The routing guidance that used to live in the standalone tool's
``system_prompt`` now lives in each class's ``description`` (the System File
group has no group-level ``system_prompt``, so per-function descriptions drive
both the child-LLM routing and the parent agent's Sub-Tools listing).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from app.config.settings import get_user_working_directory
from app.events import get_file_watcher_manager
from app.storage import get_file_watcher_storage
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.utils.logger import logger


_VALID_EVENT_TYPES: tuple[str, ...] = ("created", "modified", "deleted", "moved")
_VALID_TARGET_KINDS: tuple[str, ...] = ("file", "folder", "any")


def _normalize_string_list(raw: Any, *, lower: bool = False) -> List[str]:
    """Coerce a string-list argument into a deduplicated list of trimmed values."""
    if raw is None:
        return []
    if isinstance(raw, str):
        items: List[Any] = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if lower:
            s = s.lower()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _normalize_extensions(raw: Any) -> List[str]:
    """Lowercase and ensure a leading dot for each extension entry."""
    out: List[str] = []
    seen: set[str] = set()
    for item in _normalize_string_list(raw, lower=True):
        ext = item if item.startswith(".") else f".{item}"
        if ext in seen:
            continue
        seen.add(ext)
        out.append(ext)
    return out


def _resolve_path(raw_path: Optional[str]) -> tuple[str, bool]:
    """Resolve user-provided ``path`` against ``CREMIND_USER_WORKING_DIR``.

    Returns ``(absolute_normalized_path, was_relative)``. Relative paths
    (or empty/None) are joined with the user working directory; absolute
    paths are returned verbatim after normalization.
    """
    base = get_user_working_directory()
    if not raw_path or not str(raw_path).strip():
        resolved = base
        was_relative = True
    else:
        candidate = str(raw_path).strip()
        if candidate.startswith("~"):
            candidate = os.path.expanduser(candidate)
        if os.path.isabs(candidate):
            resolved = os.path.normpath(candidate)
            was_relative = False
        else:
            resolved = os.path.normpath(os.path.join(base, candidate))
            was_relative = True
    return os.path.abspath(resolved), was_relative


def _is_under(path: str, parent: str) -> bool:
    """True iff ``path`` is the same as or below ``parent`` (case-insensitive)."""
    try:
        normalized_path = os.path.normcase(os.path.abspath(path))
        normalized_parent = os.path.normcase(os.path.abspath(parent))
        common = os.path.commonpath([normalized_path, normalized_parent])
        return common == normalized_parent
    except ValueError:
        # Different drives on Windows
        return False


def _auto_name(root_path: str, extensions: List[str], target_kind: str) -> str:
    base = os.path.basename(root_path.rstrip(os.sep)) or root_path
    if extensions:
        ext_part = ",".join(extensions)
    elif target_kind != "any":
        ext_part = target_kind
    else:
        ext_part = "all"
    return f"{base}-{ext_part}"


class RegisterFileWatcherTool(BuiltInTool):
    name: str = "register_file_watcher"
    description: str = (
        "Register a watcher on a directory that re-runs an action (or notifies "
        "the user) whenever files or folders inside it are created, modified, "
        "deleted, or moved. Use this when the user asks to be notified or to "
        "take action whenever something happens to a file or folder on disk. "
        "Requires at minimum `path` and `action`. Defaults: `triggers` = all "
        "four events (created, modified, deleted, moved); `target_kind` = any "
        "(file / folder / any); `recursive` = true; `extensions` = none "
        "(match all). `action` is a natural-language instruction the assistant "
        "executes when the watcher fires — do not embed event metadata into it; "
        "the runtime appends a structured Content block describing the event "
        "automatically. Example: 'when a python file changes in MyDocs, notify "
        "me' → path='MyDocs', triggers=['modified','created'], "
        "target_kind='file', extensions=['.py'], action='notify the user about "
        "the change'."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Directory to watch. Relative paths are resolved against "
                    "the user's working directory (CREMIND_USER_WORKING_DIR). "
                    "Absolute paths are used as-is. Leave empty to watch the "
                    "user working directory itself."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Optional short label for this watch (e.g. 'py-only'). "
                    "Auto-generated from path + extensions if omitted."
                ),
            },
            "triggers": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": list(_VALID_EVENT_TYPES),
                },
                "uniqueItems": True,
                "description": (
                    "Event types to listen for. Defaults to all four "
                    "(created, modified, deleted, moved) when omitted."
                ),
            },
            "target_kind": {
                "type": "string",
                "enum": list(_VALID_TARGET_KINDS),
                "description": (
                    "Restrict to file events, folder events, or both. "
                    "Defaults to 'any'."
                ),
            },
            "extensions": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of file extensions (with or without "
                    "leading dot, e.g. '.py' or 'md'). When provided, "
                    "only file events whose extension matches will fire. "
                    "Ignored for folder events."
                ),
            },
            "recursive": {
                "type": "boolean",
                "description": (
                    "Whether to watch subdirectories recursively. "
                    "Defaults to true."
                ),
            },
            "action": {
                "type": "string",
                "description": (
                    "Natural-language instruction for the assistant to run "
                    "when the watcher fires. The runtime appends a "
                    "structured Content block describing the event."
                ),
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        profile: Optional[str] = arguments.get("_profile")
        context_id: Optional[str] = arguments.get("_context_id")
        raw_path = arguments.get("path")
        raw_name = arguments.get("name")
        triggers_raw = arguments.get("triggers")
        target_kind_raw = arguments.get("target_kind") or "any"
        extensions_raw = arguments.get("extensions")
        recursive = arguments.get("recursive")
        action: str = (arguments.get("action") or "").strip()

        if not profile:
            return _err(
                "Internal error: profile not provided to register_file_watcher."
            )
        if not context_id:
            return _err(
                "Internal error: context_id not provided to register_file_watcher."
            )
        if not action:
            return _err("action is required.")

        target_kind = str(target_kind_raw).strip().lower()
        if target_kind not in _VALID_TARGET_KINDS:
            return _err(
                f"target_kind must be one of {list(_VALID_TARGET_KINDS)}, "
                f"got {target_kind!r}."
            )

        triggers = _normalize_string_list(triggers_raw, lower=True)
        if not triggers:
            triggers = list(_VALID_EVENT_TYPES)
        invalid = [t for t in triggers if t not in _VALID_EVENT_TYPES]
        if invalid:
            return _err(
                f"triggers contains unknown values: {invalid}. "
                f"Valid: {list(_VALID_EVENT_TYPES)}."
            )

        extensions = _normalize_extensions(extensions_raw)

        if recursive is None:
            recursive_flag = True
        else:
            recursive_flag = bool(recursive)

        resolved_path, was_relative = _resolve_path(raw_path if isinstance(raw_path, str) else None)

        # Block ../ traversal escapes for relative paths only. Absolute paths
        # are an explicit user opt-in to watch outside the working dir.
        if was_relative:
            base = get_user_working_directory()
            if not _is_under(resolved_path, base):
                return _err(
                    f"Relative path {raw_path!r} resolves outside the user "
                    f"working directory ({base}); refusing to watch."
                )

        if not os.path.exists(resolved_path):
            return _err(
                f"Path does not exist: {resolved_path}. "
                f"Create the directory first, then re-register the watcher."
            )
        if not os.path.isdir(resolved_path):
            return _err(
                f"Path is not a directory: {resolved_path}. "
                f"File Watcher targets directories, not individual files."
            )

        name = (raw_name or "").strip() if isinstance(raw_name, str) else ""
        if not name:
            name = _auto_name(resolved_path, extensions, target_kind)

        # Resolve (or create) the conversation row eagerly so the FK target
        # exists. Same idea as register_skill_event.
        from app.storage import get_conversation_storage

        conv_storage = get_conversation_storage()
        try:
            conv = await conv_storage.get_or_create_conversation(
                profile=profile, context_id=context_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "register_file_watcher: get_or_create_conversation failed"
            )
            return _err(f"Could not resolve the active conversation: {exc}")
        if conv is None:
            return _err("Could not resolve the active conversation.")
        conversation_id = conv["id"]

        # Persist subscription row.
        store = get_file_watcher_storage()
        try:
            row = store.insert(
                conversation_id=conversation_id,
                profile=profile,
                name=name,
                root_path=resolved_path,
                recursive=recursive_flag,
                target_kind=target_kind,
                event_types=",".join(triggers),
                extensions=",".join(extensions),
                action=action,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("register_file_watcher: insert failed")
            return _err(f"Failed to save file-watcher subscription: {exc}")

        # Arm the watchdog Observer. ``arm`` is idempotent + shares observers
        # across subscriptions on the same root.
        armed = False
        try:
            armed = get_file_watcher_manager().arm(row)
        except Exception as exc:  # noqa: BLE001
            logger.exception("register_file_watcher: arm failed")

        # Notify the events admin SSE so the UI lights up immediately.
        try:
            from app.api.file_watchers import publish_file_watchers_admin_changed
            publish_file_watchers_admin_changed(profile)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                f"register_file_watcher: admin-bus publish failed: {exc}"
            )

        triggers_list = ", ".join(triggers)
        ext_summary = ", ".join(extensions) if extensions else "all extensions"
        target_summary = (
            "files only" if target_kind == "file"
            else "folders only" if target_kind == "folder"
            else "files and folders"
        )
        recursive_summary = "recursively" if recursive_flag else "non-recursively"
        confirmation = (
            f"Registered file watcher '{name}' (ID: {row['id']}) on "
            f"{resolved_path} ({recursive_summary}). Triggers: {triggers_list}. "
            f"Watching {target_summary} ({ext_summary}). "
            f"When a matching event fires, I'll run: {action}."
        )
        if not armed:
            confirmation += (
                "\n\nNote: the subscription was saved but the watcher could "
                "not be armed (path may not be readable). It will be retried "
                "on the next server restart."
            )

        logger.info(
            f"register_file_watcher: conv={conversation_id} name={name} "
            f"path={resolved_path} triggers={triggers} target={target_kind} "
            f"extensions={extensions} recursive={recursive_flag} id={row['id']}"
        )
        return BuiltInToolResult(
            content=[{"type": "text", "text": confirmation}],
            structured_content={
                "id": row["id"],
                "name": name,
                "path": resolved_path,
                "recursive": recursive_flag,
                "target_kind": target_kind,
                "triggers": triggers,
                "extensions": extensions,
                "action": action,
                "armed": armed,
            },
        )


def _err(message: str) -> BuiltInToolResult:
    return BuiltInToolResult(content=[{"type": "text", "text": message}])


def _format_subscription_line(idx: int, sub: Dict[str, Any]) -> str:
    recursive_marker = " (recursive)" if sub.get("recursive") else " (non-recursive)"
    triggers = sub.get("event_types") or ""
    extensions = sub.get("extensions") or "all extensions"
    target_kind = sub.get("target_kind") or "any"
    armed_marker = "armed" if sub.get("armed") else "not armed"
    title = sub.get("conversation_title")
    title_part = f" [chat: {title}]" if title else ""
    return (
        f"{idx}. {sub.get('name')!r} (id: {sub.get('id')}){title_part}\n"
        f"   path: {sub.get('root_path')}{recursive_marker}\n"
        f"   triggers: {triggers}; target: {target_kind}; "
        f"extensions: {extensions}; status: {armed_marker}\n"
        f"   action: {sub.get('action')}"
    )


class ListFileWatchersTool(BuiltInTool):
    name: str = "list_file_watchers"
    description: str = (
        "List the registered file watchers. Use scope='profile' (default) for "
        "every watcher owned by the user, or scope='conversation' to narrow to "
        "watchers registered in the current chat."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["profile", "conversation"],
                "description": (
                    "'profile' (default) lists every file watcher owned by "
                    "the current user. 'conversation' narrows to watchers "
                    "registered in the current chat only."
                ),
            },
        },
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        profile: Optional[str] = arguments.get("_profile")
        context_id: Optional[str] = arguments.get("_context_id")
        scope_raw = arguments.get("scope") or "profile"
        scope = str(scope_raw).strip().lower()

        if not profile:
            return _err(
                "Internal error: profile not provided to list_file_watchers."
            )
        if scope not in ("profile", "conversation"):
            return _err(
                f"scope must be 'profile' or 'conversation', got {scope!r}."
            )
        if scope == "conversation" and not context_id:
            return _err(
                "Internal error: context_id not provided to list_file_watchers."
            )

        store = get_file_watcher_storage()
        from app.storage import get_conversation_storage

        conv_storage = get_conversation_storage()

        if scope == "conversation":
            try:
                conv = await conv_storage.get_or_create_conversation(
                    profile=profile, context_id=context_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "list_file_watchers: get_or_create_conversation failed"
                )
                return _err(f"Could not resolve the active conversation: {exc}")
            if conv is None:
                return _err("Could not resolve the active conversation.")
            try:
                subs = store.list_by_conversation(conv["id"])
            except Exception as exc:  # noqa: BLE001
                logger.exception("list_file_watchers: list_by_conversation failed")
                return _err(f"Failed to load file watchers: {exc}")
        else:
            try:
                subs = store.list_by_profile(profile)
            except Exception as exc:  # noqa: BLE001
                logger.exception("list_file_watchers: list_by_profile failed")
                return _err(f"Failed to load file watchers: {exc}")

        manager = get_file_watcher_manager()
        title_cache: Dict[str, str] = {}
        enriched: List[Dict[str, Any]] = []
        for sub in subs:
            cid = sub["conversation_id"]
            if cid not in title_cache:
                try:
                    conv_row = await conv_storage.get_conversation(cid)
                except Exception:  # noqa: BLE001
                    conv_row = None
                title_cache[cid] = (conv_row or {}).get("title") or "Untitled Chat"
            enriched.append(
                {
                    **sub,
                    "armed": manager.is_armed(sub),
                    "conversation_title": title_cache[cid],
                }
            )

        if not enriched:
            summary = (
                "No file watchers registered in this conversation."
                if scope == "conversation"
                else "No file watchers registered."
            )
        else:
            scope_label = (
                "current conversation" if scope == "conversation" else "your profile"
            )
            header = (
                f"{len(enriched)} file watcher"
                f"{'s' if len(enriched) != 1 else ''} for {scope_label}:"
            )
            lines = [_format_subscription_line(i + 1, s) for i, s in enumerate(enriched)]
            summary = header + "\n" + "\n".join(lines)

        return BuiltInToolResult(
            content=[{"type": "text", "text": summary}],
            structured_content={
                "count": len(enriched),
                "scope": scope,
                "subscriptions": enriched,
            },
        )


class DeleteFileWatcherTool(BuiltInTool):
    name: str = "delete_file_watcher"
    description: str = (
        "Stop and remove a file watcher by the `id` returned from a prior "
        "register_file_watcher or list_file_watchers call."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": (
                    "Subscription ID returned by register_file_watcher or "
                    "list_file_watchers."
                ),
            },
        },
        "required": ["id"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        profile: Optional[str] = arguments.get("_profile")
        sub_id_raw = arguments.get("id")
        sub_id = str(sub_id_raw).strip() if isinstance(sub_id_raw, str) else ""

        if not profile:
            return _err(
                "Internal error: profile not provided to delete_file_watcher."
            )
        if not sub_id:
            return _err("id is required.")

        store = get_file_watcher_storage()
        try:
            existing = store.get(sub_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("delete_file_watcher: store.get failed")
            return _err(f"Failed to look up file watcher: {exc}")
        if existing is None:
            return _err(f"No file watcher with id {sub_id!r}.")
        if existing["profile"] != profile:
            return _err("That file watcher belongs to a different user.")

        try:
            store.delete(sub_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("delete_file_watcher: store.delete failed")
            return _err(f"Failed to delete file watcher: {exc}")

        try:
            get_file_watcher_manager().disarm(existing)
        except Exception:  # noqa: BLE001
            logger.exception(
                "delete_file_watcher: disarm failed (subscription was deleted)"
            )

        try:
            from app.api.file_watchers import publish_file_watchers_admin_changed
            publish_file_watchers_admin_changed(profile)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                f"delete_file_watcher: admin-bus publish failed: {exc}"
            )

        logger.info(
            f"delete_file_watcher: deleted id={sub_id} name={existing['name']!r} "
            f"path={existing['root_path']!r} profile={profile}"
        )

        confirmation = (
            f"Deleted file watcher '{existing['name']}' "
            f"(id: {sub_id}) on {existing['root_path']}."
        )
        return BuiltInToolResult(
            content=[{"type": "text", "text": confirmation}],
            structured_content={
                "id": sub_id,
                "deleted": True,
                "name": existing["name"],
                "path": existing["root_path"],
            },
        )
