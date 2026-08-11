"""Send-channel-message built-in tool.

Lets the agent message **specific people** on a connected channel — one client
or a whole list — addressed by platform sender id or phone number. "Send a
thank-you to every customer in this spreadsheet" is the shape of request this
exists for. The sibling ``send_notification`` tool is a different thing: it
pushes a message outward to the *owner's* own notification channels; this one
writes to their customers.

Like ``send_notification`` this is a **hidden** tool with no Settings toggle —
``ReasoningAgent.__init__`` withholds it unless the profile has a live channel
(:func:`app.channels.registry.has_any_channel`). Being hidden also keeps it out
of the BUILT-IN TOOLS catalogue in the system prompt, so the leaf
``description`` below is the model's only cue and has to carry the whole
contract, including the two-step confirmation flow.

Whether a send needs the user's approval is a **setting**, not a law — see
:mod:`app.channels.send_policy`. The recipient list usually comes from somewhere
the model can misread (a spreadsheet column, a half-remembered conversation) and
the messages land on real customers' phones where they cannot be recalled, so
asking first is the default. But an unattended automation has nobody to ask, so
the profile can switch it off and individual clients can be exempted; a
recipient nobody has messaged before is always confirmed regardless.

Consequently an omitted ``dry_run`` means "let the policy decide", not
"preview": the service resolves everyone, and holds the entire batch as a
preview if any of them still needs approval. ``confirm=true`` says the user has
approved this exact list — parsed strictly, because a model emitting the string
``"false"`` must not be read as approval. ``dry_run=true`` always previews.
Because the contract differs by profile, :func:`get_prepare_tools` swaps the
leaf description to match; the tool instance itself is a process-wide singleton
and must never be mutated.

Everything else (resolution, pacing, auto-registering people we've never
messaged, recording each send in that client's conversation) lives in
:mod:`app.channels.direct_send`, shared with the REST and CLI entry points.
"""

from __future__ import annotations

from typing import Any, Dict

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger


SERVER_NAME = "Send Channel Message"

_AFFIRMATIVE = frozenset({"true", "1", "yes", "on"})


def _truthy(value: Any) -> bool:
    """Plain truthiness, for a flag whose misreadings are harmless."""
    return bool(value)


def _affirmative(value: Any) -> bool:
    """Strict yes: a real ``True``, or a string that unambiguously says so.

    Used for the approval flag, where plain ``bool()`` is dangerous — a model
    emitting the string ``"false"`` for a boolean field would otherwise be
    granting approval the user never gave.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _AFFIRMATIVE
    return bool(value) if isinstance(value, (int, float)) else False


TOOL_CONFIG: ToolConfig = {
    "name": "send_channel_message",
    "display_name": SERVER_NAME,
    # Hidden: no Settings toggle. Availability == "profile has a live channel",
    # enforced by the gate in ReasoningAgent.__init__. Hidden tools are skipped
    # by the BUILT-IN TOOLS catalogue, so the leaf ``description`` is the only
    # thing the model sees — keep it strong.
    "hidden": True,
    "description": (
        "Send a message to specific people on a connected channel (WhatsApp, "
        "Telegram, Slack, ...) — one client or a bulk list, addressed by "
        "platform sender id or phone number. Use it for outreach to the user's "
        "own contacts/customers; use send_notification instead to notify the "
        "user themselves. Only offered when the profile has a live channel."
    ),
}


# The two shapes of the delivery contract. Only the middle paragraph differs —
# everything else about the tool is identical either way, and both tell the model
# to make one call and react to what comes back, so it behaves correctly even if
# the setting changes between the schema it saw and the call it makes.
_CONFIRM_STEPS = (
    "GET APPROVAL FIRST. Call it with the recipients and no confirm flag. "
    "Anyone who needs the user's approval is held back and listed under "
    "'needs_confirmation', and nothing at all is delivered in that case — you "
    "get back who each entry resolves to, who has never been messaged before, "
    "and which entries failed to resolve. Show that list to the user, get their "
    "approval, then call again with confirm=true to deliver. Never set "
    "confirm=true on the user's behalf.\n\n"
    "ALWAYS READ THE RESULT BEFORE YOU SPEAK. The user may have marked "
    "individual clients as 'send directly', and a call whose recipients are all "
    "marked that way delivers immediately — 'sent' will be non-zero and there "
    "will be no 'needs_confirmation'. Tell the user it was sent, and do NOT "
    "call again with confirm=true: that would deliver the message a second "
    "time.\n\n"
)

_DIRECT_STEPS = (
    "SEND DIRECTLY. This profile has turned off confirmation for messaging "
    "clients, so a single call delivers — you do not need to preview first, and "
    "in an unattended automation you should not stop to ask. Two kinds of "
    "recipient are still held back: a client the user marked 'always ask', and "
    "anyone who has never messaged this channel. If the result comes back with "
    "'needs_confirmation' then nothing was sent — show that list to the user "
    "and call again with confirm=true once they approve. Pass dry_run=true if "
    "you want a preview on purpose.\n\n"
)


class SendChannelMessageTool(BuiltInTool):
    # Named to match the group's tool_id so the exposed function collapses to a
    # clean ``send_channel_message`` (see ``make_leaf_name``).
    name: str = "send_channel_message"
    description: str = (
        "Send a message to specific individual people on a connected channel "
        "(WhatsApp, Telegram, Slack, Discord, ...). Handles one recipient or a "
        "bulk list, e.g. a thank-you to every customer in a spreadsheet. This "
        "is NOT send_notification: that one notifies the user themselves, this "
        "one messages their contacts.\n\n"
        + _CONFIRM_STEPS +
        "Recipients: 'to' is a platform sender id or a phone number. Phone "
        "numbers must be international (+84901234567); a leading-zero national "
        "number needs default_country_code. Only WhatsApp can message someone "
        "who has never written first — Telegram bots, Messenger and Zalo bots "
        "cannot start conversations, and those recipients come back as errors "
        "explaining what would work. Give 'channel' when a recipient could "
        "belong to more than one connected platform.\n\n"
        "Give a shared 'message' for everyone, and/or a per-recipient "
        "'message' to personalise (mail-merge from a sheet). Every delivered "
        "message is saved to that person's conversation, so later turns can "
        "see what was already sent to them. Max 100 recipients per call."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "recipients": {
                "type": "array",
                "minItems": 1,
                "description": (
                    "The people to message — one entry per person, mapping "
                    "naturally onto rows of a contact list."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "string",
                            "description": (
                                "Platform sender id (Telegram numeric id, "
                                "WhatsApp JID, Slack U…, Discord id, …) or a "
                                "phone number in international form."
                            ),
                        },
                        "message": {
                            "type": "string",
                            "description": (
                                "Optional personalised text for this recipient, "
                                "overriding the shared 'message'."
                            ),
                        },
                        "name": {
                            "type": "string",
                            "description": (
                                "Optional display name, saved when this send "
                                "creates a new contact."
                            ),
                        },
                        "channel": {
                            "type": "string",
                            "description": (
                                "Optional channel type for this recipient "
                                '(e.g. "whatsapp"), when the list mixes '
                                "platforms."
                            ),
                        },
                    },
                    "required": ["to"],
                    "additionalProperties": False,
                },
            },
            "message": {
                "type": "string",
                "description": (
                    "Text sent to every recipient that has no per-recipient "
                    "override. Required unless every recipient carries its own."
                ),
            },
            "channel": {
                "type": "string",
                "description": (
                    'Default channel type for all recipients (e.g. "whatsapp", '
                    '"telegram"). Omit to search every connected channel.'
                ),
            },
            "default_country_code": {
                "type": "string",
                "description": (
                    'Country code (e.g. "84") used to expand national-format '
                    "numbers that start with 0. Only supply it when you know "
                    "the contacts' country."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": (
                    "Set true to resolve and preview the recipients without "
                    "sending, whatever the profile's confirmation setting is. "
                    "Omit it to let that setting decide."
                ),
            },
            "confirm": {
                "type": "boolean",
                "description": (
                    "The user has approved this exact recipient list — delivers "
                    "even to recipients that would otherwise be held for "
                    "confirmation. Set it only when they really have approved; "
                    "never on their behalf."
                ),
            },
        },
        "required": ["recipients"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        from app.channels import direct_send

        profile = arguments.get("_profile")
        if not profile:
            return BuiltInToolResult(
                structured_content={
                    "error": "NoProfile",
                    "message": "Internal error: no profile is bound to this tool call.",
                }
            )

        try:
            recipients = direct_send.normalize_recipients(arguments.get("recipients"))
        except ValueError as exc:
            return BuiltInToolResult(
                structured_content={"error": "InvalidArgument", "message": str(exc)}
            )

        # An omitted dry_run now means "let the confirmation policy decide", not
        # "preview". The policy is applied after resolution (a per-client
        # exemption is only knowable once a recipient is matched to a client),
        # so the service holds the send and returns a preview when anyone in the
        # batch still needs approval. An explicit dry_run=true always previews.
        #
        # ``confirm`` is read strictly rather than with bare truthiness: tool
        # arguments are model output, and a model that emits the *string*
        # "false" for a boolean field would otherwise be handing out approval
        # nobody gave (``bool("false") is True``). Only an affirmative value
        # counts; anything else means "not approved". ``dry_run`` needs no such
        # care — misreading it errs toward previewing.
        dry_run = _truthy(arguments.get("dry_run"))
        confirm = _affirmative(arguments.get("confirm"))

        try:
            from app.channels.registry import get_channel_registry

            registry = get_channel_registry()
        except RuntimeError:
            return BuiltInToolResult(
                structured_content={
                    "error": "ChannelsUnavailable",
                    "message": (
                        "The channel subsystem is not initialized; no message "
                        "could be sent."
                    ),
                }
            )

        adapters = registry.adapters_for_profile(profile)
        if not adapters:
            return BuiltInToolResult(
                structured_content={
                    "error": "NoChannel",
                    "message": (
                        "This profile has no connected channel, so there is no "
                        "way to reach anyone. Ask the user to connect one under "
                        "Settings -> Channels."
                    ),
                }
            )

        available_types = sorted({a.channel_type for a in adapters})
        requested = str(arguments.get("channel") or "").strip()
        if requested and not any(
            direct_send.channel_matches(a, requested) for a in adapters
        ):
            return BuiltInToolResult(
                structured_content={
                    "error": "UnknownChannel",
                    "message": (
                        f"Unknown or unavailable channel: {requested}. "
                        f"Available: {', '.join(available_types) or '(none)'}."
                    ),
                    "available_channels": available_types,
                }
            )

        # Re-read the profile default here rather than trusting the schema the
        # model saw: the description is shaped per profile in prepare_tools, but
        # the setting can change between that and this call, and the safe stance
        # has to come from the live value (double-gating, as scheduler_actions does).
        from app.channels import send_policy

        profile_default = send_policy.confirm_before_send_default(profile)

        def _policy(sender: dict | None, cold: bool) -> bool:
            return send_policy.requires_confirmation(
                profile_default=profile_default, sender=sender, cold=cold,
            )

        try:
            summary = await direct_send.send_direct_messages(
                adapters=adapters,
                storage=registry.storage,
                recipients=recipients,
                message=arguments.get("message"),
                channel=requested or None,
                default_country_code=arguments.get("default_country_code"),
                dry_run=dry_run,
                initiated_by="send_channel_message",
                confirm=confirm,
                confirm_policy=_policy,
            )
        except ValueError as exc:
            return BuiltInToolResult(
                structured_content={"error": "InvalidArgument", "message": str(exc)}
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[send_channel_message] delivery run failed")
            return BuiltInToolResult(
                structured_content={"error": "SendFailed", "message": str(exc)}
            )

        summary["available_channels"] = available_types
        summary["confirmation_required_by_default"] = profile_default
        if summary.get("sent"):
            summary["note"] = (
                "Delivered messages were saved to each recipient's conversation. "
                "New contacts start unauthenticated, so on a channel with "
                "subscription auth their replies may need approval."
            )
        return BuiltInToolResult(structured_content=summary)


def _describe_for(profile: str | None) -> str:
    """The leaf description matching this profile's confirmation setting."""
    from app.channels import send_policy

    steps = (
        _CONFIRM_STEPS
        if send_policy.confirm_before_send_default(profile or "")
        else _DIRECT_STEPS
    )
    return SendChannelMessageTool.description.replace(_CONFIRM_STEPS, steps)


def get_prepare_tools():
    """Shape the leaf description to the profile's confirmation setting.

    The tool instance is a process-wide singleton shared by every profile, so
    ``self.description`` must never be mutated; ``prepare_tools`` is the hook
    that receives the active profile and may rewrite the per-request copy of the
    spec (same mechanism the scheduler uses to hide its action subtools). Telling
    a profile that has switched confirmation off to "ALWAYS preview first" would
    keep the pointless extra round trip the setting exists to remove.
    """

    def prepare_tools(query, tools, *, arguments=None, context_id=None, profile=None, **_):
        if not profile:
            return tools
        try:
            described = _describe_for(profile)
        except Exception:  # noqa: BLE001
            return tools
        for tool in tools:
            fn = tool.get("function") or {}
            if fn.get("name") == "send_channel_message":
                fn["description"] = described
                break
        return tools

    return prepare_tools


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [SendChannelMessageTool()]
