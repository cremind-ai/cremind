"""Reconciling what the platform says against what we track.

Join events are the fast path, and they are unreliable in two ways that matter.
They only fire while Cremind is running — an account added to a group overnight
generates nothing to catch up on — and several platforms do not send them at
all. This walks the account's actual group list every so often and closes both
gaps.

The interesting part is the FIRST walk on a channel. An account is typically
already in a pile of groups when the feature is switched on, and none of them
are news: nobody just added it, and raising a notification per group would open
with a wall of decisions the operator never asked to make. So the first walk
records what it finds as a *baseline* and says nothing; those groups are reached
through the picker instead (``fetch_joined_groups`` → the Channels page). Only a
group that appears *after* the baseline was taken is a genuine "you were added
to something" and gets the pending row and the notification.

The baseline lives on the channel row, so it survives restarts — an in-memory
one would make every boot look like a fresh install and re-ask about everything.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Sequence, Set

from app.channels.groups.constants import (
    DISCOVERED_VIA_SWEEP,
    STATE_GROUP_BASELINE,
    SWEEP_INITIAL_DELAY_SECONDS,
    SWEEP_INTERVAL_SECONDS,
)
from app.utils.logger import logger


async def reconcile_joined_groups(adapter: Any) -> Optional[int]:
    """Compare the platform's group list against ours. Returns new groups, or ``None``.

    ``None`` means there was nothing to compare — the feature is off, the
    platform cannot list groups, or the call failed. Never raises: this runs on
    a background timer and must not take the adapter down with it.
    """
    try:
        if not adapter.groups_enabled():
            return None
        if not getattr(type(adapter), "supports_group_listing", False):
            return None

        listed = await _list_groups(adapter)
        if listed is None:
            return None

        from app.storage import get_channel_group_storage

        storage = get_channel_group_storage()
        tracked = {
            str(g.get("platform_chat_id") or "")
            for g in await storage.list_groups(adapter.channel_id)
        }
        seen_ids = {str(g.get("platform_chat_id") or "") for g in listed}
        seen_ids.discard("")

        baseline = _read_baseline(adapter)
        if baseline is None:
            # First walk on this channel: everything the account is already in
            # is pre-existing by definition. Record it, tell nobody.
            await _write_baseline(adapter, seen_ids)
            logger.info(
                f"[channel_group] {adapter.channel_type}: {len(seen_ids)} existing "
                "group(s) recorded — pick the ones to enable on the Channels page"
            )
            return 0

        fresh = [
            g for g in listed
            if str(g.get("platform_chat_id") or "")
            and str(g["platform_chat_id"]) not in baseline
            and str(g["platform_chat_id"]) not in tracked
        ]
        if fresh:
            from app.channels.groups.inbound import handle_group_joined

            for group in fresh:
                await handle_group_joined(
                    adapter,
                    chat_id=str(group["platform_chat_id"]),
                    chat_title=group.get("title"),
                    chat_type=group.get("chat_type"),
                    discovered_via=DISCOVERED_VIA_SWEEP,
                )
            logger.info(
                f"[channel_group] {adapter.channel_type}: {len(fresh)} group(s) "
                "joined while we were not watching"
            )
        # The baseline tracks everything seen, so a group that has now been
        # asked about is not asked about again after a restart.
        await _write_baseline(adapter, baseline | seen_ids)
        return len(fresh)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[channel_group] {getattr(adapter, 'channel_type', '?')}: "
            "group reconcile failed"
        )
        return None


async def run_sweep_loop(adapter: Any) -> None:
    """Reconcile shortly after the adapter goes live, then periodically.

    Cancelled with the adapter. The initial delay lets the transport finish
    connecting — a listing call issued into a half-open session just fails and
    wastes the first pass.
    """
    try:
        await asyncio.sleep(SWEEP_INITIAL_DELAY_SECONDS)
        while True:
            await reconcile_joined_groups(adapter)
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[channel_group] {getattr(adapter, 'channel_type', '?')}: "
            "group sweep loop stopped"
        )


async def _list_groups(adapter: Any) -> Optional[List[Dict[str, Any]]]:
    try:
        listed = await adapter.fetch_joined_groups()
    except Exception:  # noqa: BLE001
        logger.warning(
            f"[channel_group] {adapter.channel_type}: could not list the "
            "account's groups",
            exc_info=True,
        )
        return None
    if listed is None:
        return None
    return [g for g in listed if str((g or {}).get("platform_chat_id") or "")]


def _read_baseline(adapter: Any) -> Optional[Set[str]]:
    """The pre-existing group ids, or ``None`` if this channel has no baseline yet.

    ``None`` and "empty set" mean different things — never seen versus seen and
    there were none — so this cannot collapse them.
    """
    state = (adapter.channel.get("state") or {})
    if STATE_GROUP_BASELINE not in state:
        return None
    stored = state.get(STATE_GROUP_BASELINE)
    if not isinstance(stored, (list, tuple, set)):
        return set()
    return {str(v) for v in stored if str(v or "")}


async def _write_baseline(adapter: Any, ids: Sequence[str] | Set[str]) -> None:
    state = dict(adapter.channel.get("state") or {})
    state[STATE_GROUP_BASELINE] = sorted({str(v) for v in ids if str(v or "")})
    try:
        updated = await adapter.storage.update_channel(
            adapter.channel_id, state=state,
        )
        adapter.channel = (
            updated if updated is not None else {**adapter.channel, "state": state}
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[channel_group] {adapter.channel_type}: could not record the "
            "group baseline"
        )
