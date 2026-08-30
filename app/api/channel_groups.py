"""Channel group chats API: the platform groups one channel's agent is in.

Nested under ``/api/channels/{channel_id}/groups`` because that is what a group
here belongs to — one channel of one profile. Authorisation is therefore the
ordinary "your own row" rule the rest of the channels API uses, not the shared
resource rule :mod:`app.api.group_chats` needs for Cremind's own rooms.

The one route that carries real weight is the PATCH: approving a group is what
lets an agent start talking to real people on a real platform, so nothing is
ingested from a group until somebody clicks it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.channels.groups import policy as group_policy
from app.channels.groups.constants import (
    DISCOVERED_VIA_PICKED,
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUSES,
)
from app.storage import get_channel_group_storage
from app.storage.conversation_storage import ConversationStorage
from app.utils.logger import logger


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _adapter_for(channel_id: str):
    """The live adapter, or ``None`` when the channel is stopped.

    A stopped channel is a normal state, not an error: its groups are still
    listed and can still be approved or blocked. Only the two operations that
    need to talk to the platform (a roster refresh) require one.
    """
    try:
        from app.channels import get_channel_registry

        return get_channel_registry().get_adapter(channel_id)
    except Exception:  # noqa: BLE001
        return None


def _capabilities(channel_type: str, mode: str = "") -> Dict[str, bool]:
    """What this platform can actually do, read off the adapter CLASS.

    Surfaced so the UI can explain an empty member list ("this platform will not
    name them") rather than showing one that looks broken, and so it can hide a
    Refresh button that could never work. Class-level, so it answers the same
    whether or not the channel happens to be running.

    Keyed on the channel's **mode** as well as its type: a Zalo bot and a
    QR-paired Zalo account are different adapters with different answers, and
    resolving by type alone reports the bot's for both.
    """
    try:
        from app.channels.registry import adapter_class_for_channel_type

        cls = adapter_class_for_channel_type(channel_type, mode)
    except Exception:  # noqa: BLE001
        cls = None
    if cls is None:
        return {
            "roster": False, "join_events": False, "bot_flag": False,
            "listing": False,
        }
    return {
        "roster": bool(getattr(cls, "supports_group_roster", False)),
        "join_events": bool(getattr(cls, "supports_group_join_events", False)),
        "bot_flag": bool(getattr(cls, "reports_sender_is_bot", False)),
        "listing": bool(getattr(cls, "supports_group_listing", False)),
    }


def _decorate(group: Dict[str, Any], channel: Dict[str, Any]) -> Dict[str, Any]:
    """One group as the clients read it.

    ``settings`` is normalised on the way out so the UI never has to reason
    about a blob written before a knob existed, and each member carries the
    ``responds`` the runtime gate would compute for it — the toggle in the UI
    and the decision in :mod:`app.channels.groups.inbound` are then the same
    answer rather than two implementations of one rule.
    """
    try:
        settings = group_policy.normalize_settings(group.get("settings"))
    except ValueError:
        settings = group_policy.default_settings()
    members = [
        {**member, "responds": group_policy.member_responds(settings, member)}
        for member in group.get("members") or []
    ]
    return {
        **group,
        "settings": settings,
        "members": members,
        "member_count": len(members),
        "capabilities": _capabilities(
            channel.get("channel_type") or "", channel.get("mode") or "",
        ),
    }


def get_channel_group_routes(
    conversation_storage: ConversationStorage,
) -> list[Route]:
    async def _load(
        request: Request,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[JSONResponse]]:
        """``(channel, error)`` — the caller's own channel, or the refusal."""
        unauth = _require_auth(request)
        if unauth is not None:
            return None, unauth
        channel_id = request.path_params["channel_id"]
        channel = await conversation_storage.get_channel(channel_id)
        if not channel:
            return None, JSONResponse(
                {"error": "Channel not found"}, status_code=404,
            )
        if channel.get("profile") != _profile_from_request(request):
            return None, JSONResponse({"error": "Forbidden"}, status_code=403)
        return channel, None

    async def _load_group(
        request: Request,
    ) -> Tuple[
        Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[JSONResponse]
    ]:
        """``(channel, group, error)``, with the group pinned to the channel."""
        channel, err = await _load(request)
        if err is not None:
            return None, None, err
        group_id = request.path_params["group_id"]
        group = await get_channel_group_storage().get_group(group_id)
        # The channel check is not redundant with the 403 above: without it, a
        # group id from somebody else's channel would be reachable through a
        # channel of your own.
        if group is None or group.get("channel_id") != channel["id"]:
            return None, None, JSONResponse(
                {"error": "Group not found"}, status_code=404,
            )
        return channel, group, None

    async def handle_list_groups(request: Request) -> JSONResponse:
        """Every platform group this channel knows about."""
        channel, err = await _load(request)
        if err is not None:
            return err
        status = request.query_params.get("status")
        if status and status not in STATUSES:
            return JSONResponse(
                {"error": f"status must be one of: {', '.join(STATUSES)}"},
                status_code=400,
            )
        groups = await get_channel_group_storage().list_groups(
            channel["id"], status=status or None,
        )
        return JSONResponse({
            "groups": [_decorate(g, channel) for g in groups],
            "group_chats_enabled": bool(
                (channel.get("config") or {}).get("group_chats_enabled")
            ),
        })

    async def handle_available_groups(request: Request) -> JSONResponse:
        """Groups the account is already in, whether or not we track them.

        The way in for a group nobody was added to. A join event only fires for
        a group joined while Cremind is watching; an account is typically in a
        dozen from before the feature existed, and those would otherwise be
        reachable only by waiting for somebody to post in one.
        """
        channel, err = await _load(request)
        if err is not None:
            return err
        capabilities = _capabilities(
            channel.get("channel_type") or "", channel.get("mode") or "",
        )
        if not capabilities["listing"]:
            return JSONResponse({"supported": False, "groups": []})
        adapter = _adapter_for(channel["id"])
        if adapter is None:
            return JSONResponse(
                {
                    "error": (
                        "This channel is not running, so its platform cannot be "
                        "asked which groups the account is in. Enable the "
                        "channel and try again."
                    ),
                },
                status_code=409,
            )
        try:
            listed = await adapter.fetch_joined_groups()
        except Exception:  # noqa: BLE001
            logger.exception(
                f"[channel_group] could not list groups for {channel['id']}"
            )
            listed = None
        if listed is None:
            return JSONResponse({"supported": False, "groups": []})

        tracked = {
            str(g.get("platform_chat_id") or ""): g
            for g in await get_channel_group_storage().list_groups(channel["id"])
        }
        groups = []
        for entry in listed:
            chat_id = str((entry or {}).get("platform_chat_id") or "")
            if not chat_id:
                continue
            known = tracked.get(chat_id)
            groups.append({
                "platform_chat_id": chat_id,
                "title": entry.get("title") or "",
                "chat_type": entry.get("chat_type"),
                "member_count": entry.get("member_count"),
                # What the UI needs to say "already enabled" rather than
                # offering it again.
                "tracked": (
                    {"id": known["id"], "status": known.get("status")}
                    if known else None
                ),
            })
        return JSONResponse({"supported": True, "groups": groups})

    async def handle_add_group(request: Request) -> JSONResponse:
        """Enable a group the account is already in.

        Picking one IS approving it — the operator is looking at their own group
        list and choosing, which is the same decision the approve button asks
        for, so asking twice would be ceremony. No notification either: nothing
        happened, somebody made a choice.
        """
        channel, err = await _load(request)
        if err is not None:
            return err
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Body must be a JSON object"}, status_code=400,
            )
        chat_id = str(body.get("platform_chat_id") or "").strip()
        if not chat_id:
            return JSONResponse(
                {"error": "'platform_chat_id' is required"}, status_code=400,
            )

        storage = get_channel_group_storage()
        group = await storage.create_group(
            channel_id=channel["id"],
            profile=channel["profile"],
            platform_chat_id=chat_id,
            chat_type=str(body.get("chat_type") or "") or None,
            title=str(body.get("title") or "") or None,
            discovered_via=DISCOVERED_VIA_PICKED,
            status=STATUS_APPROVED,
        )
        if group is None:
            return JSONResponse(
                {"error": "Could not add this group"}, status_code=500,
            )
        # ``create_group`` returns the existing row when there already is one,
        # so a group that was sitting pending is approved by picking it.
        if group.get("status") != STATUS_APPROVED:
            group = await storage.update_group(
                group["id"], status=STATUS_APPROVED,
            ) or group

        adapter = _adapter_for(channel["id"])
        if adapter is not None:
            from app.channels.groups import dispatch, roster

            try:
                await dispatch.ensure_group_conversation(adapter, group)
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"[channel_group] could not open a conversation for "
                    f"{group['id']}"
                )
            try:
                await roster.refresh_roster(adapter, group)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[channel_group] roster refresh on pick failed", exc_info=True,
                )
            group = await storage.get_group(group["id"]) or group

        return JSONResponse({"group": _decorate(group, channel)})

    async def handle_update_group(request: Request) -> JSONResponse:
        """Approve, block or reconfigure one group.

        Approving is the moment the agent is allowed to speak there, so it also
        makes sure the group has a conversation to speak in and asks the
        platform who is in it — both things the operator would otherwise have to
        wait for the next message to trigger.
        """
        channel, group, err = await _load_group(request)
        if err is not None:
            return err
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Body must be a JSON object"}, status_code=400,
            )

        patch: Dict[str, Any] = {}

        if "status" in body:
            status = str(body.get("status") or "").strip().lower()
            if status not in STATUSES:
                return JSONResponse(
                    {"error": f"status must be one of: {', '.join(STATUSES)}"},
                    status_code=400,
                )
            patch["status"] = status

        if "title" in body:
            title = str(body.get("title") or "").strip()
            if not title:
                return JSONResponse(
                    {"error": "'title' cannot be empty"}, status_code=400,
                )
            patch["title"] = title[:256]

        if "settings" in body:
            try:
                patch["settings"] = group_policy.merge_settings(
                    group.get("settings"), body.get("settings"),
                )
            except ValueError as exc:
                return JSONResponse(
                    {"error": f"Invalid settings: {exc}"}, status_code=400,
                )

        if not patch:
            return JSONResponse(
                {"error": "Nothing to update"}, status_code=400,
            )

        storage = get_channel_group_storage()
        updated = await storage.update_group(group["id"], **patch) or group
        adapter = _adapter_for(channel["id"])

        if patch.get("status") == STATUS_APPROVED:
            if adapter is not None:
                from app.channels.groups import dispatch, roster

                try:
                    await dispatch.ensure_group_conversation(adapter, updated)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        f"[channel_group] could not open a conversation for "
                        f"{updated['id']}"
                    )
                try:
                    await roster.refresh_roster(adapter, updated)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "[channel_group] roster refresh on approval failed",
                        exc_info=True,
                    )
            updated = await storage.get_group(group["id"]) or updated
        elif patch.get("status") == STATUS_BLOCKED and adapter is not None:
            # Stop carrying anything already in flight: a forwarder still
            # waiting on a run would post its answer into a room that has just
            # been refused.
            try:
                adapter.forget_group(
                    updated["id"], str(updated.get("platform_chat_id") or ""),
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[channel_group] could not drop the blocked group's state",
                    exc_info=True,
                )

        return JSONResponse({"group": _decorate(updated, channel)})

    async def handle_delete_group(request: Request) -> JSONResponse:
        """Forget a group entirely — the row, and the transcript with it.

        Not the same as blocking. A blocked group is a decision on the record
        and stays; forgetting one puts things back as though the account had
        never been in it, so being added again asks afresh.

        Refuses with 409 while a run is in progress rather than deleting rows
        out from under a live turn — the same rule the sender endpoints use.
        """
        channel, group, err = await _load_group(request)
        if err is not None:
            return err
        profile = channel["profile"]
        conversation_id = group.get("conversation_id")

        if conversation_id:
            from app.events.stream_bus import get_event_stream_bus

            if get_event_stream_bus().is_active(conversation_id):
                return JSONResponse(
                    {
                        "error": (
                            "This group has a run in progress. Wait for it to "
                            "finish before forgetting the group."
                        ),
                    },
                    status_code=409,
                )

        adapter = _adapter_for(channel["id"])
        if adapter is not None:
            try:
                adapter.forget_group(
                    group["id"], str(group.get("platform_chat_id") or ""),
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[channel_group] could not drop the forgotten group's state",
                    exc_info=True,
                )

        if conversation_id:
            try:
                from app.reset._conversations import cleanup_conversation_dependents

                await cleanup_conversation_dependents(
                    conversation_storage, conversation_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"[channel_group] teardown failed for {conversation_id}"
                )
            try:
                await conversation_storage.delete_conversation(conversation_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"[channel_group] could not delete conversation "
                    f"{conversation_id}"
                )

        deleted = await get_channel_group_storage().delete_group(group["id"])

        try:
            from app.events.conversations_list_bus import (
                publish_conversations_changed,
            )

            publish_conversations_changed(profile)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[channel_group] could not nudge the sidebar", exc_info=True,
            )

        return JSONResponse({"deleted": bool(deleted)})

    async def handle_refresh_roster(request: Request) -> JSONResponse:
        """Ask the platform who is in this group, now.

        409 without a running adapter: the member list comes from the platform,
        and a stopped channel has nothing to ask.
        """
        channel, group, err = await _load_group(request)
        if err is not None:
            return err
        adapter = _adapter_for(channel["id"])
        if adapter is None:
            return JSONResponse(
                {
                    "error": (
                        "This channel is not running, so its platform cannot be "
                        "asked who is in the group. Enable the channel and try "
                        "again."
                    ),
                },
                status_code=409,
            )

        from app.channels.groups import roster

        written = await roster.refresh_roster(adapter, group)
        updated = await get_channel_group_storage().get_group(group["id"]) or group
        return JSONResponse({
            "group": _decorate(updated, channel),
            # ``None`` means the platform names nobody — a fact about the
            # platform, not a failure, and the UI says so rather than showing an
            # empty list that looks like a bug.
            "source": "roster" if written is not None else "unsupported",
        })

    async def handle_groups_dispatch(request: Request) -> JSONResponse:
        if request.method == "GET":
            return await handle_list_groups(request)
        if request.method == "POST":
            return await handle_add_group(request)
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    async def handle_group_detail_dispatch(request: Request) -> JSONResponse:
        if request.method == "PATCH":
            return await handle_update_group(request)
        if request.method == "DELETE":
            return await handle_delete_group(request)
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    return [
        Route(
            "/api/channels/{channel_id}/groups",
            endpoint=handle_groups_dispatch,
            methods=["GET", "POST"],
        ),
        # Before the ``{group_id}`` route: "available" would otherwise be read
        # as a group id.
        Route(
            "/api/channels/{channel_id}/groups/available",
            endpoint=handle_available_groups,
            methods=["GET"],
        ),
        Route(
            "/api/channels/{channel_id}/groups/{group_id}",
            endpoint=handle_group_detail_dispatch,
            methods=["PATCH", "DELETE"],
        ),
        Route(
            "/api/channels/{channel_id}/groups/{group_id}/roster",
            endpoint=handle_refresh_roster,
            methods=["POST"],
        ),
    ]
