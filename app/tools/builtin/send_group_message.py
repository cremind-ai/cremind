"""Send-group-message built-in tool.

Lets an agent speak into a group chat it belongs to from OUTSIDE that group —
from its ordinary conversation with its own operator ("Dog, ask Cat in the group
to report today's status") or from a scheduled run ("every morning at 8, check in
with the others and write up the answers"). Inside its own group seat the tool is
withheld: there the agent's answer is already its post, and a tool call as well
would say everything twice.

Like its siblings ``send_notification`` and ``send_channel_message`` this is a
**hidden** tool with no Settings toggle — ``ReasoningAgent.__init__`` offers it
only when the profile belongs to at least one group
(:func:`app.groups.index.has_group_membership`). Hidden also means the BUILT-IN
TOOLS catalogue skips it, so the leaf ``description`` below is the model's only
briefing and has to carry the whole contract, including the part models most
often get wrong: replies come back in the GROUP, not here, so the agent must not
sit waiting for one.
"""

from __future__ import annotations

from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger
from app.utils.task_context import current_task_id_var

SERVER_NAME = "Send Group Message"


TOOL_CONFIG: ToolConfig = {
    "name": "send_group_message",
    "display_name": SERVER_NAME,
    # Hidden: availability is "this profile is in a group", enforced by the gate
    # in ReasoningAgent.__init__, not by a user-facing toggle.
    "hidden": True,
    "description": (
        "Post a message into a group chat this profile belongs to, where other "
        "profiles' agents and the people in the group will see it and may reply."
    ),
}


class SendGroupMessageTool(BuiltInTool):
    # Named to match the group's tool_id so the exposed function collapses to a
    # clean ``send_group_message`` (see ``make_leaf_name``).
    name: str = "send_group_message"
    description: str = (
        "Post a message into a Cremind group chat — a room shared with other "
        "profiles' agents and with the people who use them. Use it to ask "
        "another member for something ('@Chicken what is the server status?'), "
        "to hand over a result the room is waiting on, or when the user asks you "
        "to say something there.\n\n"
        "REPLIES DO NOT COME BACK HERE. Everything posted to a group is answered "
        "in the group, and your seat in that group is a separate conversation. So "
        "post and move on: do not wait, do not poll, and do not promise to relay "
        "an answer you will not see. If the user needs the answer in THIS "
        "conversation, tell them it will arrive in the group.\n\n"
        "'group' is the group's name or its id. Write the message the way you "
        "would say it out loud — your name is added automatically, so do not "
        "prefix it yourself."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "group": {
                "type": "string",
                "description": (
                    "Name or id of the group to post in. The name is matched "
                    "case-insensitively."
                ),
            },
            "message": {
                "type": "string",
                "description": "What to post, in full.",
            },
        },
        "required": ["group", "message"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        profile = str(arguments.get("_profile") or "")
        if not profile:
            return BuiltInToolResult(structured_content={
                "error": "NoProfile",
                "message": "Internal error: no profile is bound to this tool call.",
            })

        message = str(arguments.get("message") or "").strip()
        if not message:
            return BuiltInToolResult(structured_content={
                "error": "InvalidArgument",
                "message": "message is required and cannot be empty.",
            })

        reference = str(arguments.get("group") or "").strip()
        if not reference:
            return BuiltInToolResult(structured_content={
                "error": "InvalidArgument",
                "message": "group is required (a group name or id).",
            })

        from app.storage import get_group_chat_storage

        storage = get_group_chat_storage()
        matches = await storage.find_group(reference)
        if not matches:
            available = [
                g["name"] for g in await storage.list_groups(member=profile)
            ]
            return BuiltInToolResult(structured_content={
                "error": "UnknownGroup",
                "message": (
                    f"No group called {reference!r}. "
                    + (
                        f"This profile is in: {', '.join(available)}."
                        if available
                        else "This profile is not in any group."
                    )
                ),
                "available_groups": available,
            })
        if len(matches) > 1:
            return BuiltInToolResult(structured_content={
                "error": "AmbiguousGroup",
                "message": (
                    f"More than one group is called {reference!r}. Use the id "
                    "instead."
                ),
                "candidates": [
                    {"id": g["id"], "name": g["name"]} for g in matches
                ],
            })

        group = matches[0]
        if profile not in (group.get("members") or []):
            return BuiltInToolResult(structured_content={
                "error": "NotAMember",
                "message": (
                    f"This profile is not a member of {group['name']!r}, so it "
                    "cannot post there. An administrator adds members under "
                    "Group chat -> Settings."
                ),
            })

        # Which conversation this post came from, so the timeline can point back
        # at the reasoning behind it. Resolved through the live run binding for
        # the same reason ``get_event_task_results`` does: a channel-backed
        # conversation's context_id is the platform's chat id, not its own.
        from app.events import task_result_inbox

        source_conversation_id = task_result_inbox.conversation_for_run(
            current_task_id_var.get() or ""
        )

        from app.groups.fanout import post_message
        from app.utils.agent_name import read_agent_name

        try:
            row = await post_message(
                group_id=group["id"],
                sender_kind="agent",
                sender_profile=profile,
                sender_name=read_agent_name(profile),
                content=message,
                # Never 0: only a person resets the agent-to-agent hop chain, or
                # an agent could restart it at will from a scheduled run and talk
                # to the room forever.
                hop=1,
                source_conversation_id=source_conversation_id,
                originated_from_shadow_turn=False,
            )
        except ValueError as exc:
            return BuiltInToolResult(structured_content={
                "error": "NotAMember", "message": str(exc),
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("[send_group_message] post failed")
            return BuiltInToolResult(structured_content={
                "error": "PostFailed", "message": str(exc),
            })

        if row is None:
            return BuiltInToolResult(structured_content={
                "error": "NotPosted",
                "message": (
                    "The message was not posted — the group is gone, or this "
                    "exact message was already posted."
                ),
            })

        recipients = [
            m for m in (group.get("members") or []) if m != profile
        ]
        return BuiltInToolResult(structured_content={
            "group_id": group["id"],
            "group_name": group["name"],
            "message_id": row["id"],
            "delivered_to": recipients,
            "note": (
                "Posted. Any reply will appear in the group, not in this "
                "conversation — do not wait for one here."
            ),
        })


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [SendGroupMessageTool()]
