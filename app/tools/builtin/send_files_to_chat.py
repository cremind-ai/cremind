"""Send-files-to-chat built-in tool.

Delivers files into the chat this conversation is with — the private chat a
person wrote from on a channel, or the platform group (Telegram, Zalo,
WhatsApp, Discord, Slack) the agent is answering in — when somebody there asks
for them. A channel reply carries text only (see
:meth:`app.channels.base.BaseChannelAdapter._forward_reply`), so this is how a
person on a channel gets a file: by asking for it. Writing a file sends
nothing.

The destination is never an argument. It is the chat the turn is running in,
read from the live run binding, which is what makes "post it in the group"
safe to offer to everyone the agent answers there: nobody can use it to reach
a chat other than the one they are in. Reaching anyone else stays with
``send_channel_message`` and its approval rules.

Like its siblings ``send_notification`` and ``send_channel_message`` this is a
**hidden** tool with no Settings toggle: ``ReasoningAgent.__init__`` offers it
only in a channel conversation — private or group — while the profile has a
live channel. Hidden tools are skipped by the BUILT-IN TOOLS catalogue, so the
leaf ``description`` below is the model's whole briefing and carries the
contract, including the rule models most need: only on request.

The work — resolving the chat, checking every file before any is sent,
reporting each outcome, refusing to post the same file twice in a turn —
lives in :mod:`app.channels.chat_files`.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger
from app.utils.task_context import current_task_id_var

SERVER_NAME = "Send Files to Chat"


TOOL_CONFIG: ToolConfig = {
    "name": "send_files_to_chat",
    "display_name": SERVER_NAME,
    # Hidden: availability is "this is a channel chat and a channel is live",
    # enforced by the gate in ReasoningAgent.__init__, not by a toggle.
    "hidden": True,
    "description": (
        "Send files into the chat you are answering on a messaging channel — "
        "a person's private chat or a platform group — when somebody there "
        "asks for them."
    ),
}


class SendFilesToChatTool(BuiltInTool):
    # Named to match the group's tool_id so the exposed function collapses to a
    # clean ``send_files_to_chat`` (see ``make_leaf_name``).
    name: str = "send_files_to_chat"
    description: str = (
        "Send files into THIS chat — the private chat or the platform group "
        "you are answering right now — when somebody here asks to be sent "
        "them (\"send me the report\", \"post the photo here\"). They arrive "
        "at once, ahead of your written answer, so afterwards say in a line "
        "what you sent; do not paste their contents.\n\n"
        "ONLY ON REQUEST. Writing, converting, downloading or editing a file "
        "sends nothing, and your answer never carries files by itself. Call "
        "this only when someone here asked to receive the file — a report, "
        "chart or converted document they asked you to make counts; a file "
        "you merely read, edited or used along the way does not. In a group "
        "everyone can see what you send, so send nothing private, and decline "
        "what your operator would not want shared.\n\n"
        "'files' are paths exactly as a tool gave them — absolute is safest; "
        "a relative one is read from your working directory — at most 10. "
        "Only files in your own folders can go — never hidden files, "
        "credentials, keys or Cremind's own data. Every file is checked "
        "before any is sent: if one is refused, nothing is sent, and the "
        "result says why for each.\n\n"
        "READ THE RESULT. 'sent' files are in the chat — never call again for "
        "them. 'failed' ones did not arrive; tell them why. 'unconfirmed' ones "
        "may or may not have arrived — ask them to check rather than sending "
        "again.\n\n"
        "Only for the chat you are in. To send something to anyone else, use "
        "send_channel_message."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {"type": "string"},
                "description": (
                    "Paths of the files to send, exactly as produced or given "
                    "to you (absolute is safest)."
                ),
            },
        },
        "required": ["files"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        from app.channels import chat_files
        from app.events import task_result_inbox

        # The tool call's timeout runs from here, so the send budget does too.
        started = time.monotonic()
        profile = str(arguments.get("_profile") or "")
        if not profile:
            return _result({
                "error": "NoProfile",
                "message": "Internal error: no profile is bound to this tool call.",
            })

        try:
            paths = chat_files.normalize_paths(arguments.get("files"))
        except ValueError as exc:
            return _result({"error": "InvalidArgument", "message": str(exc)})

        try:
            from app.channels.registry import get_channel_registry

            registry = get_channel_registry()
        except RuntimeError:
            return _result({
                "error": "ChannelsUnavailable",
                "message": (
                    "The channel subsystem is not running, so nothing could be "
                    "sent."
                ),
            })

        run_id = current_task_id_var.get() or ""
        conversation_id = task_result_inbox.conversation_for_run(run_id)
        try:
            chat = await chat_files.resolve_chat(
                profile, conversation_id, registry=registry,
                context_id=arguments.get("_context_id"),
            )
            sendable, rejected = chat_files.check_files(
                profile, paths, chat=chat,
                working_directory=arguments.get("_working_directory"),
            )
        except chat_files.ChatFilesError as exc:
            return _result(exc.as_result())
        except Exception as exc:  # noqa: BLE001
            logger.exception("[send_files_to_chat] could not prepare the send")
            return _result({
                "error": "SendFailed",
                "message": f"Nothing was sent: {exc}",
            })

        if rejected:
            return _result({
                "error": "InvalidFile",
                "message": (
                    "Nothing was sent: "
                    + ("this file cannot" if len(rejected) == 1 else "these files cannot")
                    + " be sent (see 'rejected'). Drop or replace "
                    + ("it" if len(rejected) == 1 else "them")
                    + " and call again with the rest, or tell them why."
                ),
                "rejected": rejected,
                "chat": chat.describe(),
            })
        if not sendable:
            return _result({
                "error": "InvalidArgument",
                "message": "No file to send.",
            })

        try:
            summary = await chat_files.deliver(
                chat, sendable, run_id=run_id, started=started,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[send_files_to_chat] delivery failed")
            return _result({
                "error": "SendFailed",
                "message": (
                    f"The send stopped unexpectedly: {exc}. Some files may "
                    "have arrived — ask them to check before sending again."
                ),
            })
        return _result(summary)


def _result(payload: Dict[str, Any]) -> BuiltInToolResult:
    return BuiltInToolResult(structured_content=payload)


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [SendFilesToChatTool()]
