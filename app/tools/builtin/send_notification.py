"""Send-notification built-in tool.

Relays a message OUT from the reasoning agent to the profile's enabled
notification-mode channels (Telegram, WhatsApp, Zalo, ...) — the agent's *push*
affordance. "calculate 1+1 and notify me on Telegram" → the agent computes the
answer, replies in chat, and ALSO calls ``send_notification`` to deliver it out.

This is a **hidden** tool: it has no Settings toggle. Its availability is driven
entirely by channel state — ``ReasoningAgent.__init__`` withholds it whenever the
profile has no enabled notification channel (see
:func:`app.channels.registry.has_notification_channel`), so a profile with none
never even sees it. ``run()`` re-reads the live registry anyway (the set can
change between agent construction and tool execution) and reports honestly if it
now finds none.

Delivery is a **direct** push via :meth:`NotificationDeliveryMixin.deliver_text`:
it bypasses each channel's ``NotificationFilter`` (the user explicitly asked for
this send) and reaches the union of the channel's configured ``target_chat_ids``
and its authenticated ``/start`` subscribers.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger


SERVER_NAME = "Send Notification"


TOOL_CONFIG: ToolConfig = {
    "name": "send_notification",
    "display_name": SERVER_NAME,
    # Hidden: no Settings toggle. Availability == "profile has a notification
    # channel", enforced by the gate in ReasoningAgent.__init__. Because it is
    # hidden the BUILT-IN TOOLS catalogue skips it, so the model's only cue is
    # the leaf ``description`` below — keep that strong.
    "hidden": True,
    "description": (
        "Push a message OUT to the user on their notification channels (e.g. "
        "Telegram). Use it when the user asks to be notified, texted, pinged, "
        "or sent a result on a channel: still answer in chat AND deliver the "
        "final answer through this tool. Only offered when the profile has at "
        "least one enabled notification channel."
    ),
}


class SendNotificationTool(BuiltInTool):
    # Named to match the group's tool_id so the exposed function collapses to a
    # clean ``send_notification`` (see ``make_leaf_name``).
    name: str = "send_notification"
    description: str = (
        "Send a message to the user's enabled notification channels (Telegram, "
        "etc.). 'message' (required) is the text to deliver. 'channels' "
        "(optional) restricts delivery to specific channel TYPES, e.g. "
        '["telegram"]; omit it to send to every enabled notification channel. '
        "Returns per-channel recipient counts and any failures. Still answer "
        "the user in chat as well — this tool only delivers the out-of-band copy."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message text to deliver to the user.",
            },
            "channels": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of channel TYPES to target (e.g. "
                    '"telegram", "slack"). Omit to send to every enabled '
                    "notification channel."
                ),
            },
        },
        "required": ["message"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        message = str(arguments.get("message") or "").strip()
        if not message:
            return BuiltInToolResult(
                structured_content={
                    "error": "InvalidArgument",
                    "message": "The 'message' argument is required and cannot be empty.",
                }
            )

        profile = arguments.get("_profile")
        if not profile:
            return BuiltInToolResult(
                structured_content={
                    "error": "NoProfile",
                    "message": "Internal error: no profile is bound to this tool call.",
                }
            )

        try:
            from app.channels.registry import get_channel_registry

            registry = get_channel_registry()
        except RuntimeError:
            return BuiltInToolResult(
                structured_content={
                    "error": "ChannelsUnavailable",
                    "message": (
                        "The channel subsystem is not initialized; no "
                        "notification could be sent."
                    ),
                }
            )

        adapters = registry.notification_adapters_for_profile(profile)
        if not adapters:
            return BuiltInToolResult(
                structured_content={
                    "error": "NoNotificationChannel",
                    "message": (
                        "This profile has no enabled notification channel, so the "
                        "message could not be delivered. Ask the user to add one "
                        "under Settings -> Channels (mode: Notification)."
                    ),
                }
            )

        available_types = sorted({a.channel_type for a in adapters})

        requested = arguments.get("channels")
        if requested:
            wanted = {str(c).strip().lower() for c in requested if str(c).strip()}
            unknown = sorted(w for w in wanted if w not in set(available_types))
            if unknown:
                return BuiltInToolResult(
                    structured_content={
                        "error": "UnknownChannel",
                        "message": (
                            "Unknown or unavailable notification channel(s): "
                            f"{', '.join(unknown)}. "
                            f"Available: {', '.join(available_types) or '(none)'}."
                        ),
                        "requested": sorted(wanted),
                        "available_channels": available_types,
                    }
                )
            adapters = [a for a in adapters if a.channel_type in wanted]

        results: List[Dict[str, Any]] = []
        total_recipients = 0
        any_success = False
        for adapter in adapters:
            entry: Dict[str, Any] = {
                "channel_type": adapter.channel_type,
                "channel_id": adapter.channel_id,
            }
            try:
                count = await adapter.deliver_text(message)
                entry["recipients"] = count
                total_recipients += count
                if count > 0:
                    entry["status"] = "sent"
                    any_success = True
                else:
                    entry["status"] = "no_recipients"
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    f"[send_notification] delivery failed on "
                    f"{adapter.channel_type}/{adapter.channel_id}"
                )
                entry["status"] = "error"
                entry["error"] = str(exc)
            results.append(entry)

        summary: Dict[str, Any] = {
            "delivered": any_success,
            "total_recipients": total_recipients,
            "channels": results,
        }
        if not any_success and total_recipients == 0 and not any(
            r["status"] == "error" for r in results
        ):
            summary["message"] = (
                "Nothing was delivered: the targeted notification channel(s) have "
                "no subscribers yet. Ask the user to open the channel and send "
                "/start to subscribe (or configure target chat IDs)."
            )
        return BuiltInToolResult(structured_content=summary)


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [SendNotificationTool()]
