"""Handing one Cremind agent's group post to Cremind's other agents there.

Telegram never delivers a bot's message to another bot. That is a platform
rule and not a privacy-mode setting — the Bot FAQ says so, and
:meth:`app.channels.adapters.telegram.TelegramAdapter._is_self` already states
it for the echo case. Put two of your own profiles' bots in one group and ask
them to introduce themselves: each answers the human exactly once and the
conversation dies, because neither is ever told the other replied and so
neither has anything to reply to. From inside each agent the room simply went
quiet.

The channel-groups feature promises the opposite — :mod:`.inbound` says a group
may contain other automated accounts, "including other Cremind profiles'
agents, which is a supported way to use this" — and it holds on Discord, Slack
and the userbot transports, which deliver bot posts like anyone else's. On the
Telegram *bot* transport it silently did not.

This module closes that gap in-process. When a channel posts into an approved
group, the post is handed to every OTHER live Cremind channel of the same
platform that has its own approved ``channel_groups`` row for that same chat
and runs on a transport that withholds bot posts (``receives_bot_posts`` is
False on its class). The sibling gets exactly what the platform would have
delivered had it not withheld it: a bot-authored
:class:`~app.channels.groups.inbound.GroupInbound` carrying the sending
account's identity, run through the ordinary pipeline — echo filter, dedupe,
approval, member policy, mention-or-judge, loop brakes. Nothing there is
bypassed, which is what keeps the existing brakes bounding a two-bot exchange
rather than this becoming an unbounded token loop.

Only Cremind's own channels relay to each other. A third-party bot in the room
stays exactly as invisible as the platform makes it: the only thing a relay can
hand a message to is an adapter running in this process, and there is none for
somebody else's bot.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, List, Optional

from app.utils.logger import logger


def relay_candidates(adapter: Any, target: Any) -> List[Any]:
    """The sibling adapters this post is worth relaying to, or ``[]``.

    Split out from :func:`relay_agent_post` and kept purely synchronous — no
    ``await``, no database — because the caller is the reply forwarder, on the
    hot path of every message the agent says out loud. For most installs the
    answer is "nobody": one channel of this type, or a platform that delivers
    bot posts already. Answering that here means the common case creates no
    asyncio task and issues no query at all, and that includes every unit test,
    which runs with no channel registry in the process.

    The checks in order:

    1. the destination is a room we know the ``channel_groups`` row for;
    2. we are posting as a *bot*. A real account's message is delivered to
       everyone in the room by the platform, siblings included, so relaying it
       would hand them a second copy of something they already have;
    3. there is a registry to ask — there is none before boot, nor in tests;
    4. the siblings that need the relay: same platform, not ourselves, on a
       transport that withholds bot posts, with group chats switched on.

    The ``receives_bot_posts`` flag is read off the CLASS, like every other
    transport capability: it is a property of the platform, not of one
    channel's configuration.
    """
    if not (
        getattr(target, "is_group", False) and target.address and target.group_id
    ):
        return []
    if not adapter.self_identity().get("is_bot"):
        return []

    from app.channels.registry import get_channel_registry

    try:
        registry = get_channel_registry()
    except RuntimeError:
        return []

    return [
        sibling
        for sibling in registry.adapters_for_channel_type(adapter.channel_type)
        if sibling.channel_id != adapter.channel_id
        and not type(sibling).receives_bot_posts
        and sibling.groups_enabled()
    ]


async def relay_agent_post(
    adapter: Any,
    target: Any,
    text: str,
    candidates: Optional[List[Any]] = None,
) -> int:
    """Give one post to the sibling channels in the same room. Never raises.

    Returns how many siblings were handed it. ``candidates`` is the list the
    caller already computed with :func:`relay_candidates`; passing it avoids
    asking the registry the same question twice, and omitting it recomputes the
    same answer.

    Never raises because the post has already gone out to the room: the message
    the people in it care about was delivered, and a relay that failed must cost
    a log line, not the caller's turn.
    """
    try:
        body = (text or "").strip()
        if not body:
            return 0
        siblings = (
            relay_candidates(adapter, target) if candidates is None else candidates
        )
        if not siblings:
            return 0

        from app.channels.groups.constants import STATUS_APPROVED
        from app.storage import get_channel_group_storage

        rows = await get_channel_group_storage().list_groups_by_platform_chat(
            str(target.address),
        )
        by_channel = {sibling.channel_id: sibling for sibling in siblings}

        # A sibling only hears about the room it has already been given. Having
        # no row of its own is not an oversight to correct: relaying to it would
        # push the message through the discovery path, manufacturing a phantom
        # *pending* group and an operator notification for a chat the platform
        # never told that channel about. Telegram's own ``my_chat_member`` event
        # creates the real row the moment the bot is actually added, and that is
        # the row an operator should be approving. A pending or blocked row is a
        # decision somebody made about this exact group, and honouring it is the
        # whole reason the approval gate exists.
        matches: List[tuple[Any, dict]] = []
        for row in rows:
            sibling = by_channel.get(row.get("channel_id"))
            if sibling is None or row.get("status") != STATUS_APPROVED:
                continue
            matches.append((sibling, row))
        if not matches:
            return 0

        identity = adapter.self_identity()
        # The platform's own send time is unknowable from here — the message went
        # out through the transport, which reports nothing back — so the relay
        # stamps the moment it happened. Close enough for the dedupe fingerprint,
        # which is what reads it.
        posted_at = time.time()
        deliveries = []
        for sibling, row in matches:
            task = asyncio.create_task(
                sibling._handle_group_inbound(  # noqa: SLF001
                    chat_id=str(target.address),
                    chat_title=(row.get("title") or None),
                    chat_type=row.get("chat_type"),
                    sender_id=str(identity.get("user_id") or ""),
                    sender_username=identity.get("username"),
                    sender_alt_ids=list(identity.get("alt_ids") or []),
                    display_name=identity.get("display_name"),
                    text=body,
                    # No id: there is no platform message the receiving account
                    # ever saw, and inventing one would be a lie the dedupe ring
                    # keys on. Dropping it falls back to the sender + text + time
                    # fingerprint, which is exactly the key
                    # :func:`app.channels.groups.keys.platform_key` uses for the
                    # transports that number nothing.
                    platform_message_id=None,
                    sender_is_bot=True,
                    platform_message_date=posted_at,
                    # Not "this addressed you", because we do not know: the
                    # receiving pipeline's own ``_text_mentions_self`` and its
                    # relevance judge decide that, from the same text a person
                    # would have read. Claiming a mention here would force a turn
                    # on every relayed line and turn two agents into a ping-pong
                    # that only the loop brakes could end.
                    mentioned=False,
                    files=None,
                ),
                name=(
                    f"channel-relay-in:{sibling.channel_type}:"
                    f"{sibling.channel_id}"
                ),
            )
            # A task of the SIBLING's, not of ours. What it runs is the sibling's
            # own inbound pipeline, inside the sibling's adapter and under the
            # sibling's per-chat lock, so the adapter that may cancel it is the
            # sibling — whose ``stop()`` drains this very set.
            sibling._relay_tasks.add(task)  # noqa: SLF001
            task.add_done_callback(sibling._relay_tasks.discard)  # noqa: SLF001
            deliveries.append(task)

        # Shielded, because the caller's cancellation is not the sibling's. A
        # relay runs as a task of the SENDING adapter, and that adapter is
        # stopped on its own account — an operator disabling or editing that one
        # channel, or its receive loop dying. Letting that cancellation travel
        # down the gather would abort another profile's live adapter mid-message,
        # somewhere between storing the row and enqueueing the turn, and nothing
        # would record it: the dedupe ring has already claimed the message so it
        # can never be reconsidered, and a half-registered run leaves a forwarder
        # subscribed to a turn that will never come. The shield lets the sender
        # go while the deliveries finish where they belong.
        results = await asyncio.shield(
            asyncio.gather(*deliveries, return_exceptions=True),
        )
        for (sibling, _row), result in zip(matches, results):
            # One sibling failing must not cost the others their copy, which is
            # what ``return_exceptions`` buys; the failure is still worth saying
            # out loud, because from the room it looks like that agent chose to
            # stay quiet.
            if isinstance(result, BaseException):
                logger.error(
                    f"[channel_group] relay to {sibling.channel_type} channel "
                    f"{sibling.channel_id} failed: {result!r}"
                )

        title = (matches[0][1].get("title") or "").strip() or str(target.address)
        logger.info(
            f"[channel_group] relayed an agent post in \"{title}\" "
            f"(chat {target.address}) to {len(matches)} other Cremind "
            f"{adapter.channel_type} channel(s)"
        )
        return len(matches)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[channel_group] could not relay an agent post to the other "
            f"{adapter.channel_type} channels in chat "
            f"{getattr(target, 'address', '')}"
        )
        return 0
