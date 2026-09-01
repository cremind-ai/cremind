"""A message arriving from a platform group, and what becomes of it.

Most of what a group produces is not for the agent, so most of this module is
about not answering. In order:

1. **Is this channel in group chats at all?** Off is off: nothing is stored, no
   notification is raised, the agent never learns the message existed. A bot can
   be added to any group by anyone, and a feature that quietly started recording
   those would be a surprise nobody asked for.
2. **Did we write it?** Our own posts come back to us on the userbot transports
   and on anything with more than one id per account. Re-ingesting one would turn
   every answer into a new question.
3. **Have we seen it?** Two adapters can receive one message; a legacy Telegram
   group even numbers the copies differently (see :mod:`.keys`).
4. **Do we know this group?** An unknown one becomes a ``pending`` row and a
   notification, and that is all that happens until a human approves it.
5. **May we answer this person?** The group's member policy decides. A denied
   member's message is dropped rather than stored — somebody being blocked
   should not be able to fill the agent's context either.
6. **Should we answer THIS message?** Mentioned → yes. Not mentioned → the
   relevance judge, which fails closed. Either way the message is stored; only
   the turn is conditional.

Two brakes sit above all of that, because a group can contain other automated
accounts — including other Cremind profiles' agents, which is a supported way to
use this. A per-minute post cap and a consecutive-bot-messages cap stop two
assistants being endlessly helpful at each other. Both keep storing messages; a
braked agent is quiet, not blind.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.channels.groups import policy as group_policy
from app.channels.groups import roster
from app.channels.groups.constants import (
    DECISION_BRAKE_BOTS,
    DECISION_BRAKE_RATE,
    DECISION_JUDGE_IRRELEVANT,
    DECISION_JUDGE_RELEVANT,
    DECISION_MENTIONED,
    DECISION_NOT_MENTIONED,
    DISCOVERED_VIA_JOIN,
    DISCOVERED_VIA_MESSAGE,
    JUDGE_HISTORY_ROWS,
    NOTIFY_GROUP_BRAKE,
    NOTIFY_GROUP_REQUEST,
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_PENDING,
)
from app.channels.groups.keys import candidate_ids, ids_overlap, platform_key
from app.utils.logger import logger


@dataclass
class GroupInbound:
    """One message as an adapter reports it.

    ``files`` are :class:`app.channels.attachments.IncomingFile` descriptors —
    unfetched. The pipeline stages them only for an approved group and an
    allowed member; every earlier drop discards them unfetched.
    """

    chat_id: str
    sender_id: str
    text: str
    chat_title: Optional[str] = None
    chat_type: Optional[str] = None
    sender_username: Optional[str] = None
    sender_alt_ids: List[str] = field(default_factory=list)
    display_name: Optional[str] = None
    platform_message_id: Optional[str] = None
    sender_is_bot: bool = False
    platform_message_date: Optional[float] = None
    mentioned: bool = False
    files: List[Any] = field(default_factory=list)


async def handle_group_message(adapter: Any, msg: GroupInbound) -> None:
    """Run one inbound group message through the pipeline. Never raises."""
    from app.channels.attachments import discard_incoming_files

    try:
        await _handle(adapter, msg)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[channel_group] {adapter.channel_type}: failed to handle a message "
            f"in chat {msg.chat_id}"
        )
        # Safety net for sidecar-spooled payloads; staged/removed files are
        # tolerated (discard callbacks swallow their own errors).
        await discard_incoming_files(msg.files)


async def _handle(adapter: Any, msg: GroupInbound) -> None:
    from app.channels.attachments import discard_incoming_files, placeholder_text

    if not adapter.groups_enabled():
        await discard_incoming_files(msg.files)
        return
    body = (msg.text or "").strip()
    if not body and not msg.files:
        return
    if not body and msg.files:
        # A file-only message must still reach the room's transcript (and
        # possibly a turn); this stands in for the caption nobody wrote.
        body = placeholder_text([f.name for f in msg.files])
        msg.text = body

    identity = adapter.self_identity()
    sender_ids = candidate_ids(msg.sender_id, msg.sender_alt_ids)
    own_ids = [str(identity.get("user_id") or ""), *(identity.get("alt_ids") or ())]
    if ids_overlap(own_ids, sender_ids):
        await discard_incoming_files(msg.files)
        return

    key = platform_key(
        channel_type=adapter.channel_type,
        chat_id=str(msg.chat_id),
        sender_id=str(msg.sender_id),
        platform_message_id=msg.platform_message_id,
        chat_type=msg.chat_type,
        text=body,
        platform_message_date=msg.platform_message_date,
    )
    if adapter.groups.seen_recently(key):
        await discard_incoming_files(msg.files)
        return

    from app.storage import get_channel_group_storage

    storage = get_channel_group_storage()

    async with adapter.groups.lock(str(msg.chat_id)):
        group = await storage.get_group_by_chat(adapter.channel_id, str(msg.chat_id))
        if group is None:
            group = await _discover(
                adapter,
                storage,
                chat_id=str(msg.chat_id),
                chat_title=msg.chat_title,
                chat_type=msg.chat_type,
                discovered_via=DISCOVERED_VIA_MESSAGE,
            )
            await discard_incoming_files(msg.files)
            return

        group = await _refresh_title(storage, group, msg.chat_title, adapter)
        group_id = group["id"]

        # A name typed by hand counts as being addressed even where the platform
        # reports no structured mention — which is every mention on Zalo, and a
        # hand-typed one on WhatsApp.
        mentioned = bool(msg.mentioned) or _text_mentions_self(identity, body)
        msg.mentioned = mentioned

        if adapter.groups.should_write_seen(group_id, str(msg.sender_id)):
            await roster.note_seen_member(
                group_id,
                member_id=str(msg.sender_id),
                alt_ids=msg.sender_alt_ids,
                display_name=msg.display_name,
                username=msg.sender_username,
                is_bot=msg.sender_is_bot,
            )
        if adapter.groups.roster_stale(group):
            await roster.refresh_roster(adapter, group)

        if group.get("status") != STATUS_APPROVED:
            logger.debug(
                f"[channel_group] {group_id} is {group.get('status')}; "
                "the message is not delivered"
            )
            await discard_incoming_files(msg.files)
            return

        settings = _settings(group)
        if not group_policy.member_allowed(
            settings, str(msg.sender_id), msg.sender_alt_ids,
        ):
            logger.debug(
                f"[channel_group] {group_id}: {msg.sender_id} is not answered "
                "under this group's member policy"
            )
            await discard_incoming_files(msg.files)
            return

        adapter.groups.note_inbound_author(group_id, msg.sender_is_bot)

        from app.channels.groups import dispatch

        conversation_id = await dispatch.ensure_group_conversation(adapter, group)
        if not conversation_id:
            logger.error(
                f"[channel_group] {group_id}: no conversation; message dropped"
            )
            await discard_incoming_files(msg.files)
            return

        # The group is approved, the member is allowed, and the conversation
        # is known — only now do attachment bytes move.
        attachments = None
        if msg.files:
            from app.channels.attachments import stage_incoming_files

            attachments = await stage_incoming_files(
                adapter, conversation_id, msg.files,
            ) or None

        start_turn, decision = await _decide(
            adapter,
            group=group,
            settings=settings,
            conversation_id=conversation_id,
            msg=msg,
            body=body,
        )

        from app.channels.groups.render import render_attributed

        rendered = render_attributed(
            msg.display_name,
            msg.sender_username,
            body,
            mentioned=msg.mentioned,
            mention_in_text=_mention_visible(identity, body),
        )
        metadata = {
            "source": "channel_group",
            "channel_id": adapter.channel_id,
            "channel_type": adapter.channel_type,
            "channel_group": {
                "group_id": group_id,
                "platform_chat_id": str(msg.chat_id),
                "sender_id": str(msg.sender_id),
                "sender_username": msg.sender_username or "",
                "display_name": msg.display_name or "",
                "sender_is_bot": bool(msg.sender_is_bot),
                "mentioned": bool(msg.mentioned),
                "platform_message_id": msg.platform_message_id,
                "platform_key": key,
                "decision": decision,
                "quiet": not start_turn,
            },
        }

        await dispatch.deliver_to_group(
            adapter, group, conversation_id, rendered, metadata,
            start_turn=start_turn,
            attachments=attachments,
        )
        await storage.update_group(group_id, last_message_at=time.time() * 1000)


async def _decide(
    adapter: Any,
    *,
    group: Dict[str, Any],
    settings: Dict[str, Any],
    conversation_id: str,
    msg: GroupInbound,
    body: str,
) -> tuple[bool, str]:
    """``(start a turn?, why)`` for one message in an approved group."""
    group_id = group["id"]
    title = group.get("title") or group.get("platform_chat_id") or ""

    posts = adapter.groups.agent_posts_last_minute(group_id)
    rate_cap = group_policy.max_agent_posts_per_minute(settings)
    if posts >= rate_cap:
        _note_brake(adapter, group, "rate")
        logger.warning(
            f"[channel_group] {group_id}: {posts} posts in the last minute "
            f"(cap {rate_cap}); staying quiet until it settles"
        )
        return False, DECISION_BRAKE_RATE

    streak = adapter.groups.bot_streak(group_id)
    bot_cap = group_policy.max_consecutive_bot_messages(settings)
    if streak >= bot_cap:
        _note_brake(adapter, group, "bots")
        logger.warning(
            f"[channel_group] {group_id}: {streak} bot messages since a person "
            f"spoke (cap {bot_cap}); staying quiet until one does"
        )
        return False, DECISION_BRAKE_BOTS

    if msg.mentioned:
        return True, DECISION_MENTIONED

    if not group_policy.responds_without_mention(settings):
        return False, DECISION_NOT_MENTIONED

    from app.channels.groups.judge import judge_relevance
    from app.channels.groups.origin import _channel_display_name, visible_members
    from app.channels.groups.render import render_attributed, render_recent_for_judge
    from app.utils.agent_name import read_agent_name

    agent_name = read_agent_name(adapter.profile)
    recent_rows = await _recent_rows(adapter, conversation_id)
    identity = adapter.self_identity()
    handle = identity.get("mention") or (
        f"@{identity['username']}" if identity.get("username") else ""
    )
    account_name = str(identity.get("display_name") or "")
    relevant = await judge_relevance(
        profile=adapter.profile,
        agent_name=agent_name,
        agent_handle=handle,
        account_name=account_name,
        platform_name=_channel_display_name(adapter.channel_type),
        group_title=title,
        members=[m["name"] for m in visible_members(group, identity)],
        recent=render_recent_for_judge(
            recent_rows, agent_name=agent_name, account_name=account_name,
        ),
        message=render_attributed(msg.display_name, msg.sender_username, body),
        conversation_id=conversation_id,
    )
    return (
        (True, DECISION_JUDGE_RELEVANT) if relevant
        else (False, DECISION_JUDGE_IRRELEVANT)
    )


async def _recent_rows(adapter: Any, conversation_id: str) -> List[Dict[str, Any]]:
    try:
        return await adapter.storage.get_messages_after(
            conversation_id, -1, limit=JUDGE_HISTORY_ROWS, newest_first=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[channel_group] could not read recent rows for the judge", exc_info=True,
        )
        return []


async def handle_group_joined(
    adapter: Any,
    *,
    chat_id: str,
    chat_title: Optional[str] = None,
    chat_type: Optional[str] = None,
    discovered_via: str = DISCOVERED_VIA_JOIN,
) -> None:
    """The account was added to a group (or joined one). Never raises.

    Only some platforms report this. Where none arrives, the group is discovered
    by its first message instead and the outcome is identical — a pending row and
    one notification.

    ``discovered_via`` is how we came to hear about it, which the operator sees:
    a live join event, or the reconcile sweep noticing one that happened while
    Cremind was not running.
    """
    try:
        if not adapter.groups_enabled():
            return
        from app.storage import get_channel_group_storage

        storage = get_channel_group_storage()
        async with adapter.groups.lock(str(chat_id)):
            group = await storage.get_group_by_chat(adapter.channel_id, str(chat_id))
            if group is None:
                await _discover(
                    adapter,
                    storage,
                    chat_id=str(chat_id),
                    chat_title=chat_title,
                    chat_type=chat_type,
                    discovered_via=discovered_via,
                )
                return
            if group.get("status") == STATUS_BLOCKED:
                return
            await _refresh_title(storage, group, chat_title, adapter)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[channel_group] {adapter.channel_type}: failed to record joining "
            f"chat {chat_id}"
        )


async def notify_server_joined(adapter: Any, *, server_name: str) -> None:
    """Point the operator at the picker after joining a multi-channel space.

    Discord's unit of joining is a server, not a channel, so there is no one
    group to approve — the bot arrives in every channel at once. Raising a
    pending row per channel would bury the operator; one notification that opens
    the picker asks the same question once.
    """
    platform = _platform_name(adapter.channel_type)
    _push_notification(
        adapter,
        kind=NOTIFY_GROUP_REQUEST,
        priority="high",
        conversation_title=f"Added to {server_name}",
        preview=(
            f"Your {platform} account joined \"{server_name}\". Choose which of "
            "its channels the agent may take part in."
        ),
        group={},
        extra={"pick": True},
    )


async def handle_group_left(adapter: Any, *, chat_id: str) -> None:
    """The account was removed from a group, or left one. Never raises.

    Only the volatile state goes: the row, its status and its transcript stay.
    Being removed from a group is not a decision about that group — somebody
    may add the account back in a minute, and an operator who approved it once
    should not have to approve it again. What must stop is anything still in
    flight, because a forwarder waiting on a run would otherwise post an answer
    into a room the account is no longer in.
    """
    try:
        from app.storage import get_channel_group_storage

        group = await get_channel_group_storage().get_group_by_chat(
            adapter.channel_id, str(chat_id),
        )
        if group is None:
            return
        adapter.forget_group(group["id"], str(chat_id))
        logger.info(
            f"[channel_group] no longer in {group.get('title') or chat_id}; "
            "its conversation and approval are kept"
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[channel_group] {adapter.channel_type}: failed to record leaving "
            f"chat {chat_id}"
        )


async def _discover(
    adapter: Any,
    storage: Any,
    *,
    chat_id: str,
    chat_title: Optional[str],
    chat_type: Optional[str],
    discovered_via: str,
) -> Optional[Dict[str, Any]]:
    """Record a new group as pending and ask the operator about it.

    The row is the durable "already asked" marker — an in-memory set would ask
    again after every restart, and a notification per restart for a group nobody
    has got round to is how a notification list stops being read.
    """
    group = await storage.create_group(
        channel_id=adapter.channel_id,
        profile=adapter.profile,
        platform_chat_id=chat_id,
        chat_type=chat_type,
        title=chat_title,
        discovered_via=discovered_via,
        status=STATUS_PENDING,
    )
    if not group:
        return None
    # Only the creation notifies. ``create_group`` returns the existing row when
    # it loses the unique race, and that row's discovery already asked.
    if group.get("status") == STATUS_PENDING and group.get(
        "discovered_via"
    ) == discovered_via:
        _notify_request(adapter, group)
    try:
        await roster.refresh_roster(adapter, group)
    except Exception:  # noqa: BLE001
        logger.debug("[channel_group] roster refresh on discovery failed", exc_info=True)
    return group


async def _refresh_title(
    storage: Any, group: Dict[str, Any], chat_title: Optional[str], adapter: Any,
) -> Dict[str, Any]:
    """Keep the stored title current — platforms let people rename groups."""
    title = (chat_title or "").strip()
    if not title or title == (group.get("title") or ""):
        return group
    updated = await storage.update_group(group["id"], title=title)
    group = updated or group
    conversation_id = group.get("conversation_id")
    if conversation_id:
        try:
            await adapter.storage.update_conversation(conversation_id, title=title[:256])
        except Exception:  # noqa: BLE001
            logger.debug(
                "[channel_group] could not rename the group conversation",
                exc_info=True,
            )
    return group


def _settings(group: Dict[str, Any]) -> Dict[str, Any]:
    """The group's normalised settings, defaulted on anything unreadable.

    A stored blob that will not normalise must not silence a room: the defaults
    answer everyone, which is the documented behaviour of a group nobody has
    configured.
    """
    try:
        return group_policy.normalize_settings(group.get("settings"))
    except ValueError:
        logger.warning(
            f"[channel_group] {group.get('id')} has unreadable settings; "
            "using the defaults"
        )
        return group_policy.default_settings()


def _mention_visible(identity: Dict[str, Any], body: str) -> bool:
    """Whether the mention token is actually in the text the agent will read.

    Telegram and Slack put it there; WhatsApp and Zalo carry mentions as
    structured annotations, and a reply-to is nowhere in the text at all. When
    it is absent the renderer appends a marker, so the agent is never woken
    without knowing why.
    """
    haystack = (body or "").lower()
    tokens = (
        identity.get("mention"),
        identity.get("username"),
        identity.get("display_name"),
    )
    for token in tokens:
        text = str(token or "").strip().lstrip("@").lower()
        if text and text in haystack:
            return True
    return False


def _text_mentions_self(identity: Dict[str, Any], body: str) -> bool:
    """Whether the text addresses this account by name, mention markup aside.

    On Zalo a typed ``@Lý Nguyen`` is not a structured mention — the platform
    reports nothing — but it is unmistakably an address, and treating it as one
    is the difference between answering and ignoring somebody who thinks they
    just asked. WhatsApp behaves the same way for a name typed by hand.

    Deliberately narrow: an ``@`` prefix anywhere, or the account name at the
    very start of the message ("Rex, can you…"). A bare mention of the name
    mid-sentence is left to the relevance judge, which has the context to tell
    "ask Rex about it" from "Rex, look at this".
    """
    text = (body or "").strip()
    if not text:
        return False
    lowered = text.lower()
    for raw in (identity.get("display_name"), identity.get("username")):
        name = str(raw or "").strip().lstrip("@").lower()
        if not name:
            continue
        if f"@{name}" in lowered:
            return True
        if lowered.startswith(name):
            rest = lowered[len(name):].lstrip()
            # "Rex, …" / "Rex: …" / "Rex …" — but not "Rexford said…".
            if not rest or rest[0] in ",:;?!-" or lowered[len(name)] == " ":
                return True
    return False


def _notify_request(adapter: Any, group: Dict[str, Any]) -> None:
    """Tell the operator a group is waiting for a decision."""
    title = group.get("title") or group.get("platform_chat_id") or "a group"
    platform = _platform_name(adapter.channel_type)
    _push_notification(
        adapter,
        kind=NOTIFY_GROUP_REQUEST,
        priority="high",
        conversation_title=f"Group request: {title}",
        preview=(
            f"Your {platform} account was added to \"{title}\". Approve it on the "
            "Channels page before the agent replies there."
        ),
        group=group,
    )


def _note_brake(adapter: Any, group: Dict[str, Any], brake: str) -> None:
    """One notification per braking episode, not one per suppressed message."""
    if not adapter.groups.note_brake_engaged(group["id"]):
        return
    title = group.get("title") or group.get("platform_chat_id") or "a group"
    reason = (
        "it is posting too fast" if brake == "rate"
        else "only automated accounts have spoken for a while"
    )
    _push_notification(
        adapter,
        kind=NOTIFY_GROUP_BRAKE,
        priority="normal",
        conversation_title=f"Paused in {title}",
        preview=(
            f"The agent stopped replying in \"{title}\" because {reason}. "
            "It will pick up again on its own."
        ),
        group=group,
        extra={"brake": brake},
    )


def _push_notification(
    adapter: Any,
    *,
    kind: str,
    priority: str,
    conversation_title: str,
    preview: str,
    group: Dict[str, Any],
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        from app.events import get_event_notifications

        get_event_notifications().push(
            profile=adapter.profile,
            # Deliberately empty: the UI routes this kind to the Channels page,
            # and a conversation id here would send a click to a transcript
            # instead of to the decision the notification is asking for.
            conversation_id="",
            conversation_title=conversation_title,
            message_preview=preview,
            kind=kind,
            priority=priority,
            extra={
                "channel_id": adapter.channel_id,
                "channel_type": adapter.channel_type,
                "group_id": group.get("id"),
                "group_title": group.get("title") or "",
                "platform_chat_id": group.get("platform_chat_id"),
                "status": group.get("status"),
                "discovered_via": group.get("discovered_via"),
                **(extra or {}),
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception(f"[channel_group] could not push the {kind} notification")


def _platform_name(channel_type: str) -> str:
    from app.channels.groups.origin import _channel_display_name

    return _channel_display_name(channel_type)
