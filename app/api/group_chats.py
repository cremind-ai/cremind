"""Group chats API: rooms several profiles share, and the timeline inside them.

A group is a system-wide resource with per-profile membership, so the auth rule
here is not the usual "your own row": **viewing** is open to the admin and to
the profiles that sit in the room, while **changing** one — who is in it and
what it is called — is admin-only. A member can speak in its room but cannot
decide who else is in it.

Posting is the one place where a member acts. ``POST /messages`` takes the
human's text and hands it to :func:`app.groups.fanout.post_message`, which
records it once and starts a turn in every *other* member's seat. The seats
themselves are never addressed through this API — the conversations endpoints
reject them with a 403 pointing back here — because a message that reached only
one agent would leave the room's history disagreeing with itself.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, Tuple

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.api._auth import is_admin, require_admin, require_auth
from app.storage import get_group_chat_storage
from app.storage.conversation_storage import ConversationStorage
from app.utils.logger import logger

_DEFAULT_MESSAGE_LIMIT = 200
_MAX_MESSAGE_LIMIT = 500

# Long enough that a quiet room does not spam the network, short enough that no
# proxy considers the connection idle. Same value as the conversation stream.
_KEEPALIVE_SECONDS = 15.0


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _may_view(request: Request, group: Dict[str, Any]) -> bool:
    """Members see their own room; the admin sees every room."""
    if is_admin(request):
        return True
    return _profile_from_request(request) in (group.get("members") or [])


def _parse_limit(raw: Optional[str]) -> int:
    """A page size the caller asked for, clamped to something a room can serve."""
    try:
        limit = int(raw) if raw not in (None, "") else _DEFAULT_MESSAGE_LIMIT
    except (TypeError, ValueError):
        limit = _DEFAULT_MESSAGE_LIMIT
    return max(1, min(limit, _MAX_MESSAGE_LIMIT))


def _thinking_profiles(group: Dict[str, Any]) -> List[str]:
    """Members whose seat currently has a turn running.

    A cheap in-memory read of the same binding the mid-turn machinery uses, so
    the room can show "Dog is thinking…" without asking the database.
    """
    from app.events import task_result_inbox

    out: List[str] = []
    for row in group.get("member_rows") or []:
        conversation_id = row.get("shadow_conversation_id")
        profile = row.get("profile")
        if not conversation_id or not profile:
            continue
        if task_result_inbox.bound_run_for(conversation_id):
            out.append(profile)
    return out


def _may_see_seat(viewer: str, viewer_is_admin: bool, profile: str) -> bool:
    """Whether ``viewer`` may watch ``profile``'s agent work.

    The same rule the trace endpoint enforces, applied to the live stream: a
    room is shared, a member's reasoning is not. Its frames carry that profile's
    tool calls — the arguments it passed, the file paths and query results that
    came back — so being in the room buys you what the others SAY, not how they
    work. Only the profile itself, and the admin who runs them all, see behind.
    """
    return bool(viewer_is_admin or (profile and profile == viewer))


def _normalized_settings(
    raw: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[JSONResponse]]:
    """``(settings, error)`` — the blob normalised, or the 400 explaining why not.

    :func:`app.groups.settings.normalize_settings` raises on anything it cannot
    use; the message it raises with is written for the person who typed the
    value, so it goes straight into the response body.
    """
    from app.groups.settings import normalize_settings

    try:
        return normalize_settings(raw), None
    except ValueError as exc:
        return None, JSONResponse(
            {"error": f"Invalid settings: {exc}"}, status_code=400,
        )


async def _refresh_index() -> None:
    """Reload the in-memory group index after a membership change."""
    try:
        from app.groups.index import get_group_index

        await get_group_index().refresh()
    except Exception:  # noqa: BLE001 - a stale index must not fail the request
        logger.exception("[group] could not refresh the index")


async def _publish(group_id: str, event_type: str, data: Any) -> None:
    try:
        from app.groups.bus import get_group_stream_bus

        await get_group_stream_bus().publish(group_id, event_type, data)
    except Exception:  # noqa: BLE001
        logger.debug(f"[group] failed to publish {event_type}", exc_info=True)


def get_group_chat_routes(conversation_storage: ConversationStorage) -> list[Route]:

    async def _load_for_view(
        request: Request,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[JSONResponse]]:
        """``(group, error)`` — 401 unauthenticated, 404 unknown, 403 outsider."""
        unauth = require_auth(request)
        if unauth is not None:
            return None, unauth
        group_id = request.path_params["group_id"]
        group = await get_group_chat_storage().get_group(group_id)
        if group is None:
            return None, JSONResponse({"error": "Group not found"}, status_code=404)
        if not _may_view(request, group):
            return None, JSONResponse({"error": "Forbidden"}, status_code=403)
        return group, None

    async def _load_for_admin(
        request: Request,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[JSONResponse]]:
        """``(group, error)`` for the mutating routes — admin only, then 404."""
        forbidden = require_admin(request)
        if forbidden is not None:
            return None, forbidden
        group_id = request.path_params["group_id"]
        group = await get_group_chat_storage().get_group(group_id)
        if group is None:
            return None, JSONResponse({"error": "Group not found"}, status_code=404)
        return group, None

    async def _validate_members(
        raw: Any,
    ) -> Tuple[Optional[List[str]], Optional[JSONResponse]]:
        """Members, de-duplicated, after checking every profile really exists.

        A typo would otherwise create a membership row for a profile that can
        never load — invisible until somebody wonders why the room is quiet.
        """
        if raw is None:
            return [], None
        if not isinstance(raw, list):
            return None, JSONResponse(
                {"error": "'members' must be a list of profile names"},
                status_code=400,
            )
        wanted = list(dict.fromkeys(
            str(m).strip() for m in raw if str(m or "").strip()
        ))
        if not wanted:
            return [], None
        known = {p["name"] for p in await conversation_storage.list_profiles()}
        unknown = [m for m in wanted if m not in known]
        if unknown:
            return None, JSONResponse(
                {
                    "error": "Unknown profile",
                    "message": f"No such profile: {', '.join(unknown)}",
                },
                status_code=400,
            )
        return wanted, None

    async def _create_seats(group: Dict[str, Any], profiles: List[str]) -> None:
        """Give each member its hidden conversation in this group.

        Best-effort: a seat that fails to appear now is created on first
        delivery, and the boot sweep fills any that are still missing.
        """
        from app.groups.shadow import ensure_shadow_conversation

        for profile in profiles:
            try:
                await ensure_shadow_conversation(conversation_storage, profile, group)
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"[group] could not create {profile}'s seat in {group['id']}"
                )

    async def _drop_seats(group_id: str, member_rows: List[Dict[str, Any]]) -> None:
        """Tear down the seats of members who left (or of a deleted group)."""
        from app.groups.shadow import delete_shadow_conversation

        for row in member_rows:
            profile = row.get("profile")
            if not profile:
                continue
            try:
                await delete_shadow_conversation(
                    conversation_storage, group_id, profile,
                    row.get("shadow_conversation_id"),
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"[group] could not remove {profile}'s seat in {group_id}"
                )

    async def _announce_group(group_id: str) -> None:
        """Re-read a group and tell every open client what it now looks like."""
        group = await get_group_chat_storage().get_group(group_id)
        if group is not None:
            await _publish(group_id, "group_updated", group)

    async def handle_list_groups(request: Request) -> JSONResponse:
        """Every group (admin) or the caller's own rooms."""
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        storage = get_group_chat_storage()
        if is_admin(request):
            groups = await storage.list_groups()
        else:
            groups = await storage.list_groups(
                member=_profile_from_request(request)
            )
        return JSONResponse({"groups": groups})

    async def handle_create_group(request: Request) -> JSONResponse:
        """Create a room, seat its members, and announce it."""
        forbidden = require_admin(request)
        if forbidden is not None:
            return forbidden
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)

        name = str(body.get("name") or "").strip()
        if not name:
            return JSONResponse(
                {"error": "Missing parameter", "message": "name is required"},
                status_code=400,
            )

        members, err = await _validate_members(body.get("members"))
        if err is not None:
            return err

        settings, settings_err = _normalized_settings(body.get("settings"))
        if settings_err is not None:
            return settings_err

        storage = get_group_chat_storage()
        group = await storage.create_group(
            name=name,
            settings=settings,
            created_by=_profile_from_request(request) or None,
            members=members or (),
        )
        await _create_seats(group, members or [])
        # Re-read so the response carries the seat ids the creation just wrote.
        group = await storage.get_group(group["id"]) or group
        await _refresh_index()
        await _publish(group["id"], "group_updated", group)
        return JSONResponse({"group": group}, status_code=201)

    async def _seat_working_directory(row: Dict[str, Any]) -> str:
        """Where one member's agent is currently working.

        Same precedence the agent itself reads on its next step — the in-memory
        override under the seat's ``context_id`` (this boot's
        ``change_working_directory``), then the persisted column, then the
        profile default — so the room's file panel opens on the directory the
        agent's own tools would use rather than on a stale one.
        """
        from app.config.settings import get_user_working_directory
        from app.utils.context_storage import get_context
        from app.utils.working_directory import WORKING_DIR_OVERRIDE_KEY

        conversation_id = row.get("shadow_conversation_id")
        conv: Optional[Dict[str, Any]] = None
        if conversation_id:
            try:
                conv = await conversation_storage.get_conversation(conversation_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    f"[group] could not read the seat {conversation_id}", exc_info=True,
                )
        if conv:
            override = get_context(
                conv.get("context_id") or conversation_id, WORKING_DIR_OVERRIDE_KEY,
            )
            if isinstance(override, str) and override:
                return override
            persisted = conv.get("working_directory")
            if isinstance(persisted, str) and persisted:
                return persisted
        return get_user_working_directory()

    async def handle_get_group(request: Request) -> JSONResponse:
        """One room, plus which of its members are mid-turn right now.

        Each member row the caller is allowed to look behind also carries that
        agent's working directory, so the room can seed a file tree per member
        without a round trip per seat — and the seats themselves are not
        addressable through the conversations API.

        Membership is enough to open this. Every settings key describes how the
        room BEHAVES, so there is nothing here a member may not read; only
        changing them is the admin's.
        """
        group, err = await _load_for_view(request)
        if err is not None:
            return err
        viewer, viewer_is_admin = _profile_from_request(request), is_admin(request)
        for row in group.get("member_rows") or []:
            if _may_see_seat(viewer, viewer_is_admin, row.get("profile") or ""):
                row["working_directory"] = await _seat_working_directory(row)
        return JSONResponse({"group": group, "thinking": _thinking_profiles(group)})

    async def handle_update_group(request: Request) -> JSONResponse:
        """Rename a room, replace its settings, or change who sits in it.

        ``settings`` is replaced whole rather than merged: the blob decides who
        the agents obey, and a half-applied patch is a worse outcome than a
        client that has to send the whole object back.
        """
        group, err = await _load_for_admin(request)
        if err is not None:
            return err
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)

        group_id = group["id"]
        storage = get_group_chat_storage()

        name: Optional[str] = None
        if "name" in body:
            name = str(body.get("name") or "").strip()
            if not name:
                return JSONResponse(
                    {"error": "'name' cannot be empty"}, status_code=400,
                )

        settings: Optional[Dict[str, Any]] = None
        if "settings" in body:
            settings, settings_err = _normalized_settings(body.get("settings"))
            if settings_err is not None:
                return settings_err

        members: Optional[List[str]] = None
        if "members" in body:
            members, member_err = await _validate_members(body.get("members"))
            if member_err is not None:
                return member_err

        if name is not None or settings is not None:
            await storage.update_group(group_id, name=name, settings=settings)

        if members is not None:
            added, removed = await storage.set_members(group_id, members)
            # Seat the new members against the post-change group so their seat
            # title matches the room they actually joined.
            refreshed = await storage.get_group(group_id) or group
            await _create_seats(refreshed, added)
            await _drop_seats(group_id, removed)

        group = await storage.get_group(group_id) or group
        await _refresh_index()
        await _publish(group_id, "group_updated", group)
        return JSONResponse({"group": group})

    async def handle_delete_group(request: Request) -> JSONResponse:
        """Delete a room and every seat in it."""
        group, err = await _load_for_admin(request)
        if err is not None:
            return err
        group_id = group["id"]

        await _drop_seats(group_id, list(group.get("member_rows") or []))
        # Told before the bus forgets the room, so open clients learn why their
        # stream is about to end instead of just seeing it stop.
        await _publish(group_id, "deleted", {"group_id": group_id})
        # Row first, bus second. A seat turn can still be running here, and its
        # closing status hook publishes only for a room that is still in the
        # database — discarding first would leave a window in which that hook
        # sees a live row and re-creates the state we are about to drop.
        await get_group_chat_storage().delete_group(group_id)
        try:
            from app.groups.bus import get_group_stream_bus

            get_group_stream_bus().discard(group_id, deleted=True)
        except Exception:  # noqa: BLE001
            logger.debug("[group] could not discard the stream bus", exc_info=True)

        await _refresh_index()
        return JSONResponse({"deleted": True})

    async def _attach_traces(
        rows: List[Dict[str, Any]], viewer: str, viewer_is_admin: bool,
    ) -> None:
        """Decorate agent rows with the reasoning of the turn that wrote them.

        A room row records what was SAID; the steps, the artefacts and the token
        usage belong to the seat message the post came out of. Reading them back
        here is what lets a reload render the thinking panel the way the
        two-party chat does, instead of one request per post.

        Three rules, all load-bearing:

        - **Per viewer, not per room.** A member's steps are that profile's own
          tool calls — the arguments it passed, the paths that came back — so
          being in the room buys you what the others said, not how they work.
          Same gate as the live seat frames and the trace endpoint.
        - **Once per turn.** A turn interrupted mid-flight posts several
          segments that all share one ``source_message_id``; the payload rides
          the LAST of them, which is the one the client renders the panel under.
          Sending it under each would repeat a large blob for one answer.
        - **Empty is an answer.** A row whose seat message is gone gets
          ``[]`` rather than nothing: a client that cannot tell "nothing there"
          from "not told" goes and asks the trace endpoint, which can only 404,
          once per visit.

        Mutates ``rows`` in place. Never raises — a timeline that cannot be
        decorated is still a timeline.
        """
        # The last segment of each turn, by source id. Rows arrive in reading
        # order, so the last one seen wins.
        last_of_turn: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            source_id = row.get("source_message_id")
            if not source_id or row.get("sender_kind") != "agent":
                continue
            if not _may_see_seat(
                viewer, viewer_is_admin, row.get("sender_profile") or "",
            ):
                continue
            previous = last_of_turn.get(source_id)
            if previous is None or int(row.get("segment") or 0) >= int(
                previous.get("segment") or 0
            ):
                last_of_turn[source_id] = row
        if not last_of_turn:
            return

        try:
            # One IN query for the whole page: a busy room references a few
            # hundred turns, and a lookup each is a page load made of hundreds
            # of round trips.
            seats = await conversation_storage.get_messages_by_ids(
                list(last_of_turn),
            )
        except Exception:  # noqa: BLE001
            logger.exception("[group] could not read the seat messages for a timeline")
            return

        for source_id, row in last_of_turn.items():
            seat = seats.get(source_id) or {}
            # ``llm_messages`` is deliberately never copied: the raw provider
            # trace carries another profile's tool arguments and results
            # verbatim, and everyone in the room can read this response.
            row["thinking_steps"] = seat.get("thinking_steps") or []
            row["source_parts"] = seat.get("parts") or []
            # Explicitly ``None`` rather than absent, for the same reason the
            # steps answer an empty list: a client that cannot tell "there is
            # nothing" from "you were not told" goes and asks the trace
            # endpoint, which can only 404.
            row["source_token_usage"] = seat.get("token_usage")

    async def handle_list_messages(request: Request) -> JSONResponse:
        """A slice of the room's timeline.

        Without ``after`` this returns the NEWEST ``limit`` rows (in reading
        order) — what a client opening a long-running room wants. With
        ``after=<ordering>`` it returns what happened since, which is how a
        stream client fills the gap left by a disconnect.

        Agent rows the caller may look behind also carry the reasoning steps of
        the turn that wrote them, so a reload renders the thinking process
        without a request per post (see :func:`_attach_traces`).
        """
        group, err = await _load_for_view(request)
        if err is not None:
            return err
        params = request.query_params
        limit = _parse_limit(params.get("limit"))
        after_raw = params.get("after")
        storage = get_group_chat_storage()
        if after_raw in (None, ""):
            rows = await storage.list_messages(
                group["id"], limit=limit, newest_first=True,
            )
        else:
            try:
                after = int(after_raw)
            except (TypeError, ValueError):
                return JSONResponse(
                    {"error": "'after' must be a whole number"}, status_code=400,
                )
            rows = await storage.list_messages(group["id"], after=after, limit=limit)
        await _attach_traces(
            rows, _profile_from_request(request), is_admin(request),
        )
        return JSONResponse({"messages": rows})

    async def handle_post_message(request: Request) -> JSONResponse:
        """Say something in the room.

        Returns ``202`` as soon as the timeline row exists: the member turns it
        starts are runs of their own, watched through the stream, not awaited
        here. ``as_profile`` posts as that member agent instead of as the
        person — the admin's way to speak for an agent, and a member's way to
        speak as itself from outside its seat.
        """
        group, err = await _load_for_view(request)
        if err is not None:
            return err
        profile = _profile_from_request(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)

        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse(
                {"error": "Missing parameter", "message": "text is required"},
                status_code=400,
            )

        as_profile = str(body.get("as_profile") or "").strip()
        if as_profile:
            if as_profile not in (group.get("members") or []):
                return JSONResponse(
                    {
                        "error": "Not a member",
                        "message": f"{as_profile!r} is not a member of this group.",
                    },
                    status_code=400,
                )
            if not (is_admin(request) or as_profile == profile):
                return JSONResponse(
                    {
                        "error": "Forbidden",
                        "message": "Only the admin can post as another member.",
                    },
                    status_code=403,
                )

        from app.groups import fanout

        if as_profile:
            from app.utils.agent_name import read_agent_name

            post_kwargs: Dict[str, Any] = {
                "sender_kind": "agent",
                "sender_name": read_agent_name(as_profile),
                "sender_profile": as_profile,
                # Never hop 0: only a human resets the loop guard, and this post
                # did not come from one.
                "hop": 1,
            }
        else:
            from app.groups.settings import web_sender_name

            post_kwargs = {
                "sender_kind": "user",
                "sender_name": web_sender_name(group.get("settings")),
                "sender_identity": {"channel_type": "web", "sender_id": profile},
                "hop": 0,
            }

        try:
            row = await fanout.post_message(
                group_id=group["id"], content=text, **post_kwargs,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if row is None:
            return JSONResponse(
                {
                    "error": "Duplicate",
                    "message": "That message is already in the timeline.",
                },
                status_code=409,
            )
        return JSONResponse({"message": row}, status_code=202)

    async def handle_message_trace(request: Request) -> JSONResponse:
        """The reasoning behind one posted answer — for its own author, or admin.

        A room is shared, but a member's REASONING is not. The steps carry that
        profile's tool calls: the arguments it passed and what came back, which
        is its own tenant's data (file paths, query results, credentials in an
        error string). Sharing a room means the others hear what it decided to
        say, not how it works. So this is narrower than the rest of the group
        API: membership is enough to read the room, but only the profile that
        wrote the turn — or the admin who runs them all — can read behind it.
        ``llm_messages`` is never returned to anyone here.
        """
        group, err = await _load_for_view(request)
        if err is not None:
            return err
        message_id = request.path_params["message_id"]
        row = await get_group_chat_storage().get_message(message_id)
        if row is None or row.get("group_id") != group["id"]:
            return JSONResponse({"error": "Message not found"}, status_code=404)

        source_message_id = row.get("source_message_id")
        source = (
            await conversation_storage.get_message(source_message_id)
            if source_message_id
            else None
        )
        # "Nothing to show" is answered before "not yours to see": a human post
        # has no turn and no author, and refusing one as if it held a secret
        # would be both wrong and confusing.
        if source is None:
            return JSONResponse(
                {
                    "error": "No trace",
                    "message": "This message did not come from an agent turn.",
                },
                status_code=404,
            )

        author = row.get("sender_profile")
        if not is_admin(request) and _profile_from_request(request) != author:
            return JSONResponse(
                {
                    "error": "Forbidden",
                    "message": (
                        "Only the agent that wrote a message (or the admin "
                        "profile) can read its reasoning."
                    ),
                },
                status_code=403,
            )
        metadata = source.get("metadata") or {}
        return JSONResponse({
            "conversation_id": (
                row.get("source_conversation_id") or source.get("conversation_id")
            ),
            "message": {
                "id": source.get("id"),
                "content": source.get("content"),
                "thinking_steps": source.get("thinking_steps") or [],
                # The artefacts the turn produced (terminals, files). Carried
                # for the same reason the timeline carries them: a trace that
                # cannot show the shell it opened is only half the turn.
                "parts": source.get("parts") or [],
                # Same field the timeline enrichment attaches, so a post that
                # arrives through this fallback shows the same usage chip as one
                # that came with the page.
                "token_usage": source.get("token_usage"),
                "provider": metadata.get("provider"),
                "model": metadata.get("model"),
                "created_at": source.get("created_at"),
            },
        })

    async def _in_flight_seat_frames(
        group: Dict[str, Any], viewer: str, viewer_is_admin: bool,
    ) -> List[Dict[str, Any]]:
        """The steps already taken by whichever member turns are running now.

        Seat frames are published ephemerally, so the group ring holds none of
        them and a client opening a busy room would otherwise join blind —
        watching an agent it can see is thinking, with no idea what it has been
        doing for the last minute. The authoritative copy is each seat's own
        replay ring, so it is read straight from there, filtered by the same
        rule the live tail uses.

        Carries no group ``seq`` (like the synthesised ``ready`` frame): these
        never went through the group bus, and inventing one would collide with a
        real frame. They do carry the SEAT bus's own ``seat_seq``, which is what
        lets a client joining mid-turn recognise the step it is about to receive
        again from the live tail — a frame published in the moment between
        subscribing and this snapshot arrives twice, and matching sequence
        numbers make that exact rather than a guess from the step's contents.
        """
        from app.events import get_event_stream_bus
        from app.groups.hooks import seat_event_payload

        bus = get_event_stream_bus()
        frames: List[Dict[str, Any]] = []
        for row in group.get("member_rows") or []:
            profile = row.get("profile") or ""
            conversation_id = row.get("shadow_conversation_id")
            if not profile or not conversation_id:
                continue
            if not _may_see_seat(viewer, viewer_is_admin, profile):
                continue
            try:
                ring, active = await bus.snapshot(conversation_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    f"[group] could not snapshot the seat {conversation_id}",
                    exc_info=True,
                )
                continue
            if not active:
                continue
            for event in ring:
                payload = seat_event_payload(profile, conversation_id, event)
                if payload is not None:
                    frames.append({"type": "seat_event", "data": payload})
        return frames

    async def handle_group_stream(request: Request) -> Any:
        """Server-Sent Events for one room: new posts, who is thinking, and the
        steps each member's agent is taking.

        The bus ring is replayed first (optionally trimmed with
        ``?since=<ordering>`` so a reconnecting client is not handed messages it
        already has), then whatever the in-flight turns have already emitted,
        then a ``ready`` frame carrying every member's current state, then the
        live tail. Unlike a conversation stream this is not scoped to a run — a
        room has no runs, only a history.

        Frames are shaped per viewer, which is the privacy boundary between two
        members of the same room. A ``seat_event`` is one profile's own tool
        calls and their output, so a peer's is dropped whole; a
        ``group_updated`` is the room's configuration, so a member's copy is
        redacted the same way the detail route redacts it — the bus publishes
        one frame to everybody and only this endpoint knows who is reading.
        The viewer is resolved BEFORE the generator: the generator body runs
        after this handler has returned, and the auth decision must not be
        re-derived from a request that is by then only being drained.
        """
        group, err = await _load_for_view(request)
        if err is not None:
            return err
        group_id = group["id"]
        viewer, viewer_is_admin = _profile_from_request(request), is_admin(request)

        since_raw = request.query_params.get("since")
        try:
            since = int(since_raw) if since_raw not in (None, "") else None
        except (TypeError, ValueError):
            since = None

        from app.groups.bus import get_group_stream_bus

        bus = get_group_stream_bus()
        queue, replay = await bus.subscribe(group_id)

        thinking = set(_thinking_profiles(group))
        agents = {
            profile: ("thinking" if profile in thinking else "idle")
            for profile in (group.get("members") or [])
        }

        def _for_viewer(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            """One frame as this viewer may see it, or ``None`` to drop it."""
            event_type = event.get("type")
            if event_type == "seat_event":
                profile = (event.get("data") or {}).get("profile") or ""
                if not _may_see_seat(viewer, viewer_is_admin, profile):
                    return None
                return event
            return event

        async def generator():
            def _frame(payload: Dict[str, Any]) -> bytes:
                return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

            try:
                for event in replay:
                    shaped = _for_viewer(event)
                    if shaped is None:
                        continue
                    if since is not None and event.get("type") == "message":
                        try:
                            if int((event.get("data") or {}).get("ordering", -1)) <= since:
                                continue
                        except (TypeError, ValueError):
                            pass
                    yield _frame(shaped)
                for event in await _in_flight_seat_frames(
                    group, viewer, viewer_is_admin,
                ):
                    yield _frame(event)
                yield _frame({"type": "ready", "data": {"agents": agents}})

                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=_KEEPALIVE_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        # SSE comment line — ignored by clients, keeps proxies happy.
                        yield b": keepalive\n\n"
                        continue
                    shaped = _for_viewer(event)
                    if shaped is None:
                        continue
                    yield _frame(shaped)
            finally:
                await bus.unsubscribe(group_id, queue)

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            generator(), media_type="text/event-stream", headers=headers,
        )

    async def handle_groups_dispatch(request: Request) -> JSONResponse:
        """Dispatch /api/group-chats by HTTP method."""
        if request.method == "GET":
            return await handle_list_groups(request)
        if request.method == "POST":
            return await handle_create_group(request)
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    async def handle_group_detail_dispatch(request: Request) -> JSONResponse:
        """Dispatch /api/group-chats/{group_id} by HTTP method."""
        if request.method == "GET":
            return await handle_get_group(request)
        if request.method == "PATCH":
            return await handle_update_group(request)
        if request.method == "DELETE":
            return await handle_delete_group(request)
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    async def handle_messages_dispatch(request: Request) -> JSONResponse:
        """Dispatch /api/group-chats/{group_id}/messages by HTTP method."""
        if request.method == "GET":
            return await handle_list_messages(request)
        if request.method == "POST":
            return await handle_post_message(request)
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    return [
        Route(
            "/api/group-chats",
            endpoint=handle_groups_dispatch,
            methods=["GET", "POST"],
        ),
        Route(
            "/api/group-chats/{group_id}",
            endpoint=handle_group_detail_dispatch,
            methods=["GET", "PATCH", "DELETE"],
        ),
        Route(
            "/api/group-chats/{group_id}/messages",
            endpoint=handle_messages_dispatch,
            methods=["GET", "POST"],
        ),
        Route(
            "/api/group-chats/{group_id}/messages/{message_id}/trace",
            endpoint=handle_message_trace,
            methods=["GET"],
        ),
        Route(
            "/api/group-chats/{group_id}/stream",
            endpoint=handle_group_stream,
            methods=["GET"],
        ),
    ]
