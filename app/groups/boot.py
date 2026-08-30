"""Bringing group chats back up, and repairing what a crash interrupted.

Two sweeps, both idempotent, both cheap when nothing is wrong.

:func:`sweep_unposted_agent_rows` finds seat turns that finished but never
reached the posting hook — the process died between persisting the agent's
message and posting it — and posts them now. It looks only at each seat's newest
agent message, because the queue runs one turn at a time per conversation, so at
most one can be stranded. Re-posting is safe regardless:
``(source_message_id, segment)`` is unique, so a row that did make it through
simply refuses to be written twice.

:func:`sweep_undelivered_group_messages` finishes a fan-out that stopped
half-way, using the ``delivered_to`` list each post updates as it goes. What it
must not do is *change* the decisions the interrupted fan-out already made: a
capped message stays capped, and a member the router passed over is delivered
without a turn, read back off ``metadata.routing`` rather than re-classified.

The index and the seats are loaded before anything can post, because
``has_group_membership`` and the co-membership check in the processes API read
it and would report an empty room until it is there.
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.utils.logger import logger

# How far back the delivery sweep looks per group. A gap older than this is not
# worth re-delivering: the conversation has moved on.
_DELIVERY_SWEEP_WINDOW = 50


def _storages():
    from app.events import runner as event_runner
    from app.storage import get_group_chat_storage

    return get_group_chat_storage(), event_runner.get_conversation_storage()


async def load_index() -> None:
    from app.groups.index import get_group_index

    await get_group_index().refresh()


async def ensure_all_shadow_conversations() -> int:
    """Make sure every member of every group has a seat. Returns how many it made."""
    storage, conversation_storage = _storages()
    if storage is None or conversation_storage is None:
        return 0
    from app.groups.shadow import ensure_shadow_conversation

    created = 0
    for group in await storage.list_groups():
        for profile in group.get("members") or []:
            member = await storage.get_member(group["id"], profile)
            had_seat = bool((member or {}).get("shadow_conversation_id"))
            try:
                conv = await ensure_shadow_conversation(
                    conversation_storage, profile, group,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"[group] could not create {profile}'s seat in {group['id']}"
                )
                continue
            if conv is not None and not had_seat:
                created += 1
    if created:
        logger.info(f"[group] created {created} missing seat conversation(s)")
    return created


async def sweep_unposted_agent_rows() -> int:
    """Post seat turns a crash stranded between persisting and posting."""
    storage, conversation_storage = _storages()
    if storage is None or conversation_storage is None:
        return 0
    from app.groups.hooks import on_shadow_turn_complete

    swept = 0
    for group in await storage.list_groups():
        for member in await storage.list_members(group["id"]):
            conversation_id = member.get("shadow_conversation_id")
            profile = member.get("profile")
            if not conversation_id or not profile:
                continue
            try:
                row = await conversation_storage.get_latest_agent_message(
                    conversation_id
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"[group] sweep could not read {conversation_id}"
                )
                continue
            if row is None:
                continue
            metadata = row.get("metadata") or {}
            if "group" in metadata:
                continue  # already accounted for, whatever the outcome was
            if row.get("token_usage") is None:
                # No usage means no LLM call produced this row — it is an
                # event-trigger bubble or similar, not something the agent said.
                continue
            logger.info(
                f"[group] re-posting a stranded turn for {profile} "
                f"in {conversation_id}"
            )
            await on_shadow_turn_complete(
                conversation_storage=conversation_storage,
                conversation_id=conversation_id,
                profile=profile,
                # The turn's own run id when the row recorded one. A turn that
                # answered an interruption posted under it, and passing anything
                # else here would make the sweep repeat what the room already
                # heard.
                run_id=str(metadata.get("run_id") or "boot-sweep"),
                assistant_msg_id=row.get("id"),
                raw_text=row.get("content") or "",
                final_text=row.get("content") or "",
                mid_turn_breaks=(metadata.get("mid_turn_breaks") or []),
            )
            swept += 1
    if swept:
        logger.info(f"[group] boot sweep re-posted {swept} stranded turn(s)")
    return swept


async def sweep_undelivered_group_messages() -> int:
    """Finish fan-outs that stopped part-way through."""
    storage, conversation_storage = _storages()
    if storage is None or conversation_storage is None:
        return 0
    from app.groups.fanout import _deliver_to_member  # noqa: PLC2701 - same package
    from app.groups.render import render_attributed
    from app.groups.settings import max_agent_hops, normalize_settings

    delivered = 0
    for group in await storage.list_groups():
        members: List[str] = list(group.get("members") or [])
        if not members:
            continue
        settings = normalize_settings(group.get("settings"))
        # When each member joined. A member added to an existing room was never
        # owed the conversation that happened before it arrived: without this it
        # would look "undelivered" for every past message and the next restart
        # would dump the whole backlog into its seat, starting a turn per
        # message — a new member waking up to fifty stale questions.
        joined_at = {
            r["profile"]: float(r.get("joined_at") or 0)
            for r in (group.get("member_rows") or [])
        }
        rows = await storage.list_messages(
            group["id"], limit=_DELIVERY_SWEEP_WINDOW, newest_first=True,
        )
        for row in rows:
            already = set(row.get("delivered_to") or [])
            sender = row.get("sender_profile")
            created_at = float(row.get("created_at") or 0)
            missing = [
                m for m in members
                if m not in already
                and m != sender
                and created_at >= joined_at.get(m, 0.0)
            ]
            if not missing:
                continue
            rendered = render_attributed(
                row.get("sender_name") or "", row.get("sender_kind") or "user",
                row.get("content") or "",
            )
            capped = bool((row.get("metadata") or {}).get("quiet")) or (
                row.get("sender_kind") == "agent"
                and int(row.get("hop") or 0) >= max_agent_hops(settings)
            )
            for member in missing:
                routed_away = not capped and _was_routed_away(row, member)
                try:
                    await _deliver_to_member(
                        member=member,
                        group=group,
                        rendered=rendered,
                        row=row,
                        capped=capped or routed_away,
                        conversation_storage=conversation_storage,
                        routed_away=routed_away,
                    )
                    await storage.update_delivered_to(row["id"], [member])
                    delivered += 1
                except Exception:  # noqa: BLE001
                    logger.exception(
                        f"[group] sweep could not deliver {row['id']} to {member}"
                    )
    if delivered:
        logger.info(f"[group] boot sweep delivered {delivered} missed message(s)")
    return delivered


def _was_routed_away(row: Dict[str, Any], member: str) -> bool:
    """Whether the router declined to wake ``member`` for this post.

    Read back off the row instead of asking the model again: a second call costs
    another classification per swept message and could answer differently, so a
    restart would start exactly the turns the room already decided against.
    Anything unreadable — an older row with no stamp, a truncated one — falls
    open to "wake it", which is what the sweep did before routing existed.
    """
    from app.groups.routing import decision_from_stamp, should_start_turn

    decision = decision_from_stamp((row.get("metadata") or {}).get("routing"))
    if decision is None:
        return False
    return not should_start_turn(decision, member)


async def on_profile_deleted(profile: str) -> None:
    """Drop a deleted profile's seats from memory and reload the index.

    The membership rows cascade away in the database, but the queue workers,
    stream-bus entries and run bindings for that profile's seats do not.
    """
    from app.groups.index import get_group_index

    index = get_group_index()
    try:
        from app.events import queue as event_queue
        from app.events import task_result_inbox
        from app.events.stream_bus import get_event_stream_bus

        for group_id in index.groups_for_profile(profile):
            conversation_id = index.shadow_conversation(group_id, profile)
            if not conversation_id:
                continue
            event_queue.discard_queue(conversation_id)
            await get_event_stream_bus().discard(conversation_id)
            task_result_inbox.discard(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception(f"[group] could not release {profile}'s seat state")
    await load_index()


async def initialize() -> Dict[str, Any]:
    """Everything the server does for group chats at boot, in order."""
    await load_index()
    created = await ensure_all_shadow_conversations()
    posted = await sweep_unposted_agent_rows()
    delivered = await sweep_undelivered_group_messages()
    return {"seats_created": created, "turns_reposted": posted, "delivered": delivered}
