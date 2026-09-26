"""Search-tools API: which search sources the agent may use in a conversation.

The composer's **Search tools** control, ``cremind conv search-tools`` and
``cremind group search-tools`` all read and write through here. One body shape
comes back from every route (``SearchToolsState``, see
:mod:`app.agent.search_tools`)::

    {"version": 3, "enabled": [...], "effective": [...],
     "tools": [{"id", "label", "description", "available", "unavailable_reason"}],
     "pending_next_response": false, "cache_warning": null}

Routes
------
- ``GET  /api/search-tools`` — the defaults and availability a NEW ordinary
  conversation would start with (the composer's new-chat slot).
- ``GET/PUT /api/conversations/{id}/search-tools`` — an ordinary or event-run
  conversation, owner only. A group seat is refused: the room holds its
  selection.
- ``GET/PUT /api/group-chats/{id}/search-tools`` — a room's shared selection.
  Anyone who may post in the room may change it (its members and the admin);
  every other room setting stays admin-only.

A ``PUT`` carries ``{"version", "enabled"}``. The write is a compare-and-set on
the version: a stale version gets ``409`` with the current state, an update
that changes nothing keeps the version, and a successful change is published on
the conversation's (or room's) event stream as ``search_tools``.

Saving never touches a running response: the run froze its selection when it
started, and the saved one is adopted by the next response that starts. Saving
also never enables a tool the profile turned off, starts indexing, or grants
document access — availability is computed, never granted, here.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.agent import search_tools as st
from app.api._auth import is_admin, require_auth
from app.utils.logger import logger


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _error(status: int, error: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": error, "message": message, **extra}, status_code=status)


async def _read_body(request: Request) -> Tuple[Optional[int], Any, Optional[JSONResponse]]:
    """``(version, enabled, error)`` from a PUT body, or the 400 explaining why not."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None, None, _error(400, "InvalidBody", "The body must be a JSON object.")
    if not isinstance(body, dict):
        return None, None, _error(400, "InvalidBody", "The body must be a JSON object.")
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        return None, None, _error(
            400, "InvalidVersion",
            "Pass the 'version' you last read (a non-negative integer) so a concurrent "
            "change is not overwritten.",
        )
    if "enabled" not in body:
        return None, None, _error(
            400, "InvalidSelection",
            "Pass 'enabled': a list of search-tool ids, [] for none, or null for the defaults.",
        )
    return version, body.get("enabled"), None


def _normalize(enabled: Any) -> Tuple[Optional[List[str]], Optional[JSONResponse]]:
    try:
        return st.normalize_selection(enabled), None
    except st.SelectionError as exc:
        return None, _error(400, "InvalidSelection", str(exc))


async def _availability(profile: str, origin: str) -> Dict[str, st.Availability]:
    # Registry and gate reads touch the database: keep them off the event loop.
    return await asyncio.to_thread(st.availability, profile, origin)


async def _origin_for(conversation_storage: Any, conv: Dict[str, Any]) -> str:
    """The ``allow_in`` class of a conversation, derived the way a run derives
    it (so the picker offers exactly what the next response will get)."""
    from app.agent.stream_runner import _resolve_message_origin

    try:
        origin = await _resolve_message_origin(
            conversation_storage, conv, conv["id"], event_run=conv.get("kind") == "event_run",
        )
    except Exception:  # noqa: BLE001
        origin = None
    return st.origin_key(origin)


def _pending_warning(
    selection: Optional[List[str]],
    version: int,
    baseline: Optional[Dict[str, Any]],
    avail: Dict[str, st.Availability],
    prior_activity: bool,
) -> Optional[str]:
    """The warning a GET shows while a saved change is not adopted yet: the next
    response will expose a different set of search tools than the last one did.

    Without a baseline (a conversation from before this feature), the last
    responses exposed the defaults, so a saved change that differs from the
    defaults counts once the conversation has had a response at all.
    """
    after = st.effective_ids(selection, avail)
    base = st.baseline_effective(baseline)
    if base is not None:
        if (baseline or {}).get("version", 0) < version and base != after:
            return st.CACHE_WARNING
        return None
    if prior_activity and version > 0 and after != st.effective_ids(None, avail):
        return st.CACHE_WARNING
    return None


async def _prior_activity(conversation_storage: Any, conversation_id: str) -> bool:
    probe = getattr(conversation_storage, "has_main_model_activity", None)
    if probe is None:
        return False
    try:
        return bool(await probe(conversation_id))
    except Exception:  # noqa: BLE001
        logger.debug(f"[search-tools] activity probe failed for {conversation_id}", exc_info=True)
        return False


async def _publish_conversation(conversation_id: str, profile: str, version: int) -> None:
    try:
        from app.events.stream_bus import get_event_stream_bus

        await get_event_stream_bus().publish_transient(
            conversation_id, "search_tools", {"version": version}, profile=profile,
        )
    except Exception:  # noqa: BLE001
        logger.debug(f"[search-tools] publish failed for {conversation_id}", exc_info=True)


async def _publish_group(group_id: str, version: int) -> None:
    try:
        from app.groups.bus import get_group_stream_bus

        await get_group_stream_bus().publish(group_id, "search_tools", {"version": version})
    except Exception:  # noqa: BLE001
        logger.debug(f"[search-tools] publish failed for group {group_id}", exc_info=True)


# ── ordinary / event conversations ─────────────────────────────────────────


async def conversation_state(
    conversation_storage: Any,
    row: Dict[str, Any],
    conv: Dict[str, Any],
    *,
    profile: str,
    cache_warning: Optional[str] = None,
    use_pending_warning: bool = True,
) -> st.State:
    """The state of one ordinary or event-run conversation."""
    avail = await _availability(profile, await _origin_for(conversation_storage, conv))
    selection = st.read_stored(row.get("search_tools"))
    version = int(row.get("search_tools_version") or 0)
    baseline = st.read_baseline(row.get("search_cache_baseline"))
    if cache_warning is None and use_pending_warning:
        cache_warning = _pending_warning(
            selection, version, baseline, avail,
            await _prior_activity(conversation_storage, row["id"]),
        )
    return st.build_state(
        selection=selection,
        version=version,
        avail=avail,
        pending=st.pending_next_response(version, baseline, st.running_versions(row["id"])),
        cache_warning=cache_warning,
    )


# ── group rooms ────────────────────────────────────────────────────────────


async def _seat_rows(conversation_storage: Any, group: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """``(profile, seat row)`` for every member whose seat exists."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    getter = getattr(conversation_storage, "get_search_tools_row", None)
    for member in group.get("member_rows") or []:
        profile, seat_id = member.get("profile"), member.get("shadow_conversation_id")
        if not profile or not seat_id or getter is None:
            continue
        try:
            row = await getter(seat_id)
        except Exception:  # noqa: BLE001
            row = None
        if row:
            out.append((profile, row))
    return out


async def group_state(
    conversation_storage: Any,
    group: Dict[str, Any],
    *,
    cache_warning: Optional[str] = None,
    use_pending_warning: bool = True,
) -> st.State:
    """The room's shared selection, with availability aggregated over its
    member agents — each still keeps its own tool settings and document
    consent, so what one member can use is never shown as another's."""
    from app.documents.gate import ORIGIN_ROOMS

    members = [m for m in (group.get("members") or []) if m]
    per_member = await asyncio.gather(*(_availability(m, ORIGIN_ROOMS) for m in members))
    merged = st.merge_room_availability(list(per_member))
    selection = st.read_stored(group.get("search_tools"))
    version = int(group.get("search_tools_version") or 0)

    pending = False
    warning = cache_warning
    by_member = dict(zip(members, per_member))
    for profile, seat in await _seat_rows(conversation_storage, group):
        baseline = st.read_baseline(seat.get("search_cache_baseline"))
        pending = pending or st.pending_next_response(version, baseline, st.running_versions(seat["id"]))
        if warning is None and use_pending_warning and profile in by_member:
            warning = _pending_warning(
                selection, version, baseline, by_member[profile],
                await _prior_activity(conversation_storage, seat["id"]),
            )
    return st.State(
        version=version,
        enabled=st.desired_ids(selection),
        effective=st.effective_ids(selection, merged),
        tools=st.room_tool_rows(merged),
        pending_next_response=pending,
        cache_warning=warning,
    )


async def _group_edit_warning(
    conversation_storage: Any,
    group: Dict[str, Any],
    before: Optional[List[str]],
    after: Optional[List[str]],
) -> Optional[str]:
    """The cache warning for a room edit: shown when at least one member seat
    would send a different prefix than its last request did. Members adopt the
    change at different times, so each seat is judged against its own
    baseline."""
    from app.documents.gate import ORIGIN_ROOMS

    for profile, seat in await _seat_rows(conversation_storage, group):
        avail = await _availability(profile, ORIGIN_ROOMS)
        if st.edit_may_miss_cache(
            before_effective=st.effective_ids(before, avail),
            after_effective=st.effective_ids(after, avail),
            baseline=st.read_baseline(seat.get("search_cache_baseline")),
            prior_activity=await _prior_activity(conversation_storage, seat["id"]),
        ):
            return st.CACHE_WARNING
    return None


# ── routes ─────────────────────────────────────────────────────────────────


def get_search_tools_routes(conversation_storage: Any) -> List[Route]:

    async def _load_conversation(
        request: Request,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[JSONResponse]]:
        """``(conversation, search-tools row, error)`` — owner only; a group
        seat is refused because the room holds its selection."""
        unauth = require_auth(request)
        if unauth is not None:
            return None, None, unauth
        profile = _profile_from_request(request)
        conversation_id = request.path_params["conversation_id"]
        conv = await conversation_storage.get_conversation(conversation_id)
        if not conv:
            return None, None, _error(404, "NotFound", "Conversation not found.")
        if conv.get("profile") != profile:
            return None, None, _error(403, "Forbidden", "This conversation belongs to another profile.")
        if conv.get("kind") == "group_chat":
            return None, None, _error(
                403, "GroupSeat",
                "This conversation is an agent's seat in a group chat; its search tools are "
                "the room's. Use /api/group-chats/{id}/search-tools.",
            )
        row = await conversation_storage.get_search_tools_row(conversation_id)
        if not row:
            return None, None, _error(404, "NotFound", "Conversation not found.")
        return conv, row, None

    async def handle_defaults(request: Request) -> JSONResponse:
        """The state a new ordinary conversation starts with."""
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        from app.documents.gate import ORIGIN_WEB_CLI

        profile = _profile_from_request(request)
        avail = await _availability(profile, ORIGIN_WEB_CLI)
        return JSONResponse(st.build_state(selection=None, version=0, avail=avail).as_dict())

    async def handle_get_conversation(request: Request) -> JSONResponse:
        conv, row, err = await _load_conversation(request)
        if err is not None:
            return err
        state = await conversation_state(
            conversation_storage, row, conv, profile=_profile_from_request(request),
        )
        return JSONResponse(state.as_dict())

    async def handle_put_conversation(request: Request) -> JSONResponse:
        conv, row, err = await _load_conversation(request)
        if err is not None:
            return err
        version, enabled, err = await _read_body(request)
        if err is not None:
            return err
        selection, err = _normalize(enabled)
        if err is not None:
            return err
        profile = _profile_from_request(request)
        before = st.read_stored(row.get("search_tools"))
        status, new_row = await conversation_storage.set_search_tools(
            conv["id"], expected_version=version, selection=selection,
        )
        if status == "missing" or not new_row:
            return _error(404, "NotFound", "Conversation not found.")
        if status == "conflict":
            current = await conversation_state(conversation_storage, new_row, conv, profile=profile)
            return _error(
                409, "VersionConflict",
                "The search tools were changed elsewhere; showing the current choice.",
                state=current.as_dict(),
            )
        if status == "noop":
            state = await conversation_state(
                conversation_storage, new_row, conv, profile=profile, use_pending_warning=False,
            )
            return JSONResponse(state.as_dict())

        avail = await _availability(profile, await _origin_for(conversation_storage, conv))
        warning = st.CACHE_WARNING if st.edit_may_miss_cache(
            before_effective=st.effective_ids(before, avail),
            after_effective=st.effective_ids(selection, avail),
            baseline=st.read_baseline(new_row.get("search_cache_baseline")),
            prior_activity=await _prior_activity(conversation_storage, conv["id"]),
        ) else None
        state = await conversation_state(
            conversation_storage, new_row, conv, profile=profile,
            cache_warning=warning, use_pending_warning=False,
        )
        await _publish_conversation(conv["id"], profile, state.version)
        logger.info(
            f"[search-tools] conversation={conv['id']} profile={profile} "
            f"v{version}->v{state.version} enabled={state.enabled}"
        )
        return JSONResponse(state.as_dict())

    async def _load_group(
        request: Request,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[JSONResponse]]:
        """``(group, error)`` — the admin or a member of the room (the people
        who may post in it); everyone else gets a 403, an unknown room a 404."""
        unauth = require_auth(request)
        if unauth is not None:
            return None, unauth
        from app.storage import get_group_chat_storage

        group = await get_group_chat_storage().get_group(request.path_params["group_id"])
        if group is None:
            return None, _error(404, "NotFound", "Group not found.")
        if not (is_admin(request) or _profile_from_request(request) in (group.get("members") or [])):
            return None, _error(403, "Forbidden", "Only the admin and this room's members can use it.")
        return group, None

    async def handle_get_group(request: Request) -> JSONResponse:
        group, err = await _load_group(request)
        if err is not None:
            return err
        return JSONResponse((await group_state(conversation_storage, group)).as_dict())

    async def handle_put_group(request: Request) -> JSONResponse:
        group, err = await _load_group(request)
        if err is not None:
            return err
        version, enabled, err = await _read_body(request)
        if err is not None:
            return err
        selection, err = _normalize(enabled)
        if err is not None:
            return err
        from app.storage import get_group_chat_storage

        storage = get_group_chat_storage()
        before = st.read_stored(group.get("search_tools"))
        status, new_group = await storage.set_group_search_tools(
            group["id"], expected_version=version, selection=selection,
        )
        if status == "missing" or not new_group:
            return _error(404, "NotFound", "Group not found.")
        if status == "conflict":
            current = await group_state(conversation_storage, new_group)
            return _error(
                409, "VersionConflict",
                "The room's search tools were changed by someone else; showing the current choice.",
                state=current.as_dict(),
            )
        if status == "noop":
            state = await group_state(conversation_storage, new_group, use_pending_warning=False)
            return JSONResponse(state.as_dict())
        warning = await _group_edit_warning(conversation_storage, new_group, before, selection)
        state = await group_state(
            conversation_storage, new_group, cache_warning=warning, use_pending_warning=False,
        )
        await _publish_group(new_group["id"], state.version)
        logger.info(
            f"[search-tools] group={new_group['id']} by={_profile_from_request(request)} "
            f"v{version}->v{state.version} enabled={state.enabled}"
        )
        return JSONResponse(state.as_dict())

    return [
        Route("/api/search-tools", handle_defaults, methods=["GET"]),
        Route("/api/conversations/{conversation_id}/search-tools", handle_get_conversation, methods=["GET"]),
        Route("/api/conversations/{conversation_id}/search-tools", handle_put_conversation, methods=["PUT"]),
        Route("/api/group-chats/{group_id}/search-tools", handle_get_group, methods=["GET"]),
        Route("/api/group-chats/{group_id}/search-tools", handle_put_group, methods=["PUT"]),
    ]


__all__ = ["conversation_state", "get_search_tools_routes", "group_state"]
