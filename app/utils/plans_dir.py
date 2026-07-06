"""Durable per-conversation plan directory for Plan mode.

Plan mode writes the agent's proposed plan as a Markdown file under the
profile's slice of ``CREMIND_SYSTEM_DIR``::

    <CREMIND_SYSTEM_DIR>/<profile>/plans/<conversation_id>/<filename>.md

Unlike ``uploads_tmp`` this tree is deliberately NOT wiped on boot or pruned on
idle: a plan awaiting approval — or being executed across several turns, or
resumed later after an interrupt — must survive a restart. The location already
sits inside an allowed root for the ``system_file`` tool group (the per-profile
slice of ``CREMIND_SYSTEM_DIR``, see ``system_file._allowed_roots``), so the
agent can re-read the plan with an absolute path during execution.

The directory is removed only when its conversation is deleted (best-effort, via
``_cleanup_conversation_dependents`` in ``app/api/conversations.py``).
"""

from __future__ import annotations

import os

from app.config.settings import BaseConfig

PLANS_DIRNAME = "plans"


def plans_root(profile: str) -> str:
    """Return ``<CREMIND_SYSTEM_DIR>/<profile>/plans`` (not created)."""
    return os.path.join(BaseConfig.CREMIND_SYSTEM_DIR, profile, PLANS_DIRNAME)


def _conversation_plans_path(profile: str, conversation_id: str) -> str:
    """Compute (without creating) the plans dir for one conversation.

    ``conversation_id`` is server-generated but never trusted: it is reduced to
    its basename and rejected if empty / ``.`` / ``..`` so a crafted value can
    never escape the profile's ``plans`` root.
    """
    if not profile:
        raise ValueError("profile is required for a plans directory")
    cid = os.path.basename((conversation_id or "").strip())
    if not cid or cid in (".", ".."):
        raise ValueError(f"invalid conversation_id for plans dir: {conversation_id!r}")
    return os.path.join(plans_root(profile), cid)


def conversation_plans_dir(profile: str, conversation_id: str) -> str:
    """Return (creating it) the plans dir for one conversation."""
    target = _conversation_plans_path(profile, conversation_id)
    os.makedirs(target, exist_ok=True)
    return target


def _sanitize_filename(filename: str) -> str:
    """Reduce a model-supplied filename to a safe ``*.md`` basename."""
    name = os.path.basename((filename or "").strip())
    if not name or name in (".", ".."):
        name = "plan.md"
    if not name.lower().endswith(".md"):
        name = f"{name}.md"
    return name


def plan_file_path(profile: str, conversation_id: str, filename: str) -> str:
    """Absolute path for a new plan file, creating the conversation dir.

    The filename is sanitized to a basename with a ``.md`` suffix; if it
    collides with an existing plan a ``-2``/``-3``/... suffix is appended so a
    second plan in the same conversation never clobbers the first.
    """
    directory = conversation_plans_dir(profile, conversation_id)
    name = _sanitize_filename(filename)
    candidate = os.path.join(directory, name)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(name)
    i = 2
    while True:
        candidate = os.path.join(directory, f"{stem}-{i}{ext}")
        if not os.path.exists(candidate):
            return candidate
        i += 1


def is_inside_conversation_plans(profile: str, conversation_id: str, abs_path: str) -> bool:
    """True iff ``abs_path`` resolves inside this conversation's plans dir."""
    if not abs_path:
        return False
    try:
        base = os.path.realpath(_conversation_plans_path(profile, conversation_id))
    except ValueError:
        return False
    target = os.path.realpath(abs_path)
    return target == base or target.startswith(base + os.sep)


def remove_conversation_plans(profile: str, conversation_id: str) -> None:
    """Best-effort removal of a conversation's plan directory (on delete)."""
    import shutil
    try:
        path = _conversation_plans_path(profile, conversation_id)
    except ValueError:
        return
    shutil.rmtree(path, ignore_errors=True)
