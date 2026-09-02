"""Turning a platform group message into the text a model reads.

Attribution has to live in the message CONTENT rather than in metadata because
:func:`app.utils.common.convert_db_messages_to_history` hands the model only
``role`` and ``content`` — a speaker recorded anywhere else is a speaker the
model never learns about. In a room of several people that is the difference
between answering a question and answering the room.

The mention marker is here for a related reason. On Telegram and Slack being
addressed is visible in the text (``@ops_bot``), but on WhatsApp and Zalo a
mention is a structured annotation the text does not contain, and a reply-to is
not in the text anywhere. Without the marker the agent would be woken with no
idea why.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

# Appended when the platform says we were addressed but the text does not show
# it. Deliberately the same shape as the routing notes in Cremind's own rooms —
# a bracketed annotation on its own line reads as "the system is telling me
# something", which is what it is.
ADDRESSED_MARKER = "[addressed to you]"

# How much of one older message the relevance judge is shown. Enough to see what
# a thread is about; short enough that eight of them stay cheap.
_JUDGE_ROW_MAX_CHARS = 400


def render_attributed(
    display_name: Optional[str],
    username: Optional[str],
    text: str,
    *,
    mentioned: bool = False,
    mention_in_text: bool = True,
) -> str:
    """The line the agent receives: ``Alexa Nguyen (@alexa): status?``.

    Falls back through display name → handle → nothing, because platforms differ
    in which they give: a WhatsApp participant is often only a phone number,
    while a Slack user is a name and a handle. The caller passes whatever it has
    and the shape stays readable either way.

    ``mention_in_text`` is False when the platform reported a mention that the
    text itself does not contain (a structured mention, a reply-to); only then is
    the marker appended, so a message that already reads "@ops_bot status?" is
    not annotated with something the room cannot see.
    """
    name = (display_name or "").strip()
    handle = (username or "").strip()
    if handle and not handle.startswith("@"):
        handle = f"@{handle}"
    if name and handle:
        prefix = f"{name} ({handle})"
    else:
        prefix = name or handle or "Someone"
    body = f"{prefix}: {text}"
    if mentioned and not mention_in_text:
        body = f"{body}\n{ADDRESSED_MARKER}"
    return body


def render_recent_for_judge(
    rows: Sequence[Dict[str, Any]], *, agent_name: str, account_name: str = "",
) -> list[str]:
    """The last few messages, as the relevance judge should read them.

    User rows are already attributed (they were stored that way), so they go
    through as they are. The agent's own turns are labelled ``(you)`` — without
    that the judge cannot tell an exchange the agent is already in from two other
    people talking, which is most of what it is being asked to decide.

    The label uses the account name the group actually sees where there is one,
    so the judge can match it against a message that addresses the agent by that
    name.

    Both role spellings are accepted. The database stores an agent turn as
    ``"agent"`` and only the model-facing conversion renames it to
    ``"assistant"``; testing for one spelling silently un-labelled every real
    row, which is exactly the signal this function exists to provide.

    Rows the agent stayed silent on are skipped: they are messages it chose not
    to answer, and showing them as its own speech would be a lie. So is the
    trigger block of an automation result — machine-written text the agent never
    said. It is stored as an agent row (that is how the transcript renders it),
    and it shares ``metadata.source`` with the agent's real answer to the same
    turn, so the two are told apart by the ``trigger`` marker.
    """
    label = account_name.strip() or agent_name
    out: list[str] = []
    for row in rows or ():
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        metadata = row.get("metadata") or {}
        stamp = metadata.get("channel_group") or {}
        if row.get("role") in {"agent", "assistant"}:
            if stamp.get("kind") in {"silent", "empty"}:
                continue
            if (
                metadata.get("source") == "event_task_result"
                and metadata.get("trigger")
            ):
                continue
            content = f"{label} (you): {content}"
        if len(content) > _JUDGE_ROW_MAX_CHARS:
            content = content[:_JUDGE_ROW_MAX_CHARS].rstrip() + "…"
        out.append(content)
    return out
