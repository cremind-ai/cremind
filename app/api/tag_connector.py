"""The Cremind Tag connector API: ``/api/tag-connector/v1/*`` (connector-api.md).

The PC companion connects OUT to Cremind and authenticates every request with
a connector credential — and nothing else::

    Authorization: CremindTag <credential-id>.<secret>

A session JWT (``Bearer``) or a missing/malformed header answers 401; a revoked
credential answers 401 ``credential_revoked`` once its secret checks out; a
credential of the wrong kind answers 403 ``wrong_credential_kind``. The
profile is ALWAYS the credential's own, never a request field. Everywhere
else in Cremind the ``CremindTag`` scheme answers 401
(:class:`app.middleware.tag_connector_guard.TagConnectorGuard`), and the JWT
backend never authenticates it.

Errors are ``{"error": <code>, "message": <sentence>, "detail": <same>}`` —
connector-api.md names the sentence ``detail``, the rest of Cremind
``message``; both are sent. Timestamps are ISO 8601 UTC strings with
milliseconds (``2026-09-27T10:00:00.123Z``). Epochs and revisions are uint32.

Hardware credential (one companion):

- ``GET  whoami`` (any kind) -> ``{credential_id, kind, companion_id, profile, api_version, server_time}``
- ``POST inventory``  ``{gateways, bridges, tags}`` -> ``{devices, assignments}``.
  Each ``tags[]`` item may carry ``"epoch": <uint32>`` — the highest
  assignment epoch the companion has used for that tag or learned from it
  (the handshake's ``CHALLENGE.stored_epoch``). Cremind keeps
  ``epoch = max(stored, reported)``, so a forgotten-then-re-reported tag, or
  one whose epoch a restore rewound, is never assigned an epoch it refuses
  (``STALE_EPOCH``). When the report is ahead of work Cremind still owes under
  the old epoch (an owned tag's assignment, a pending clear), that work is
  re-queued as ``assign_tag`` / ``clear_tag`` at ``reported + 1`` and the
  ``assignments`` in the response carry the new epoch. Omitted, negative,
  non-integer or out-of-range values are ignored — as is any out-of-range
  field of an item, so one bad item never fails the whole report.
- ``POST heartbeat``  ``{companion, queue, devices}`` -> ``{server_time, commands_pending}``
- ``GET  commands?wait=<s≤30>`` -> ``{commands}``; returns early when one is queued.
  Ownership commands (``assign_tag`` / ``clear_tag``) come first. A profile's
  identify / refresh is queued at most once per tag while pending.
- ``POST commands/{id}/claim`` -> 200 the command object, or 409 ``already_claimed``
- ``POST commands/{id}/result`` ``{status: succeeded|failed, result?, error?}`` -> ``{ok, command}``;
  the same status again is a no-op, a different one 409 ``already_completed``.
  A command Cremind stopped waiting for (``expired``) still takes a late
  result. A failed or expired ``clear_tag`` is re-queued (3 attempts in all),
  after which the tag's status reads ``clear_failed``.

Content credential (one profile + one companion):

- ``POST sync`` ``{cursor?}`` -> ``{profile, companion_id, stream_id, cursor_valid, oldest_seq,
  head_seq, outstanding, tags, settings}``. ``settings`` is ``{enabled, layout,
  show_excerpts, qr_links, progress_cadence_s, timezone, language}``;
  ``timezone`` is always an IANA name; ``enabled`` false means the profile has
  switched Tags off (no new jobs until it is on again).
- ``GET  events?after=<seq>&limit=<n≤200>`` -> ``{stream_id, jobs, next_after, head_seq}``;
  410 ``cursor_expired`` (with ``oldest_seq``) when ``after`` is older than the
  retained history or newer than ``head_seq`` (a restore) — call ``sync``.
  ``jobs`` may include deliveries that are ALREADY TERMINAL (superseded,
  cancelled, expired … while the companion was away): check ``stage`` and skip
  them. Only jobs for tags the profile still owns are listed, and a live one
  only at the tag's current epoch; ``next_after`` still advances past the rest.
  Every content job has a ``replace_key`` (``delivery:<id>`` when the card has
  no shared one). A cancel from Cremind arrives as a ``resolved`` job whose
  ``resolves`` names that key.
- ``POST accepted`` ``{through_seq, delivery_ids}`` -> ``{accepted}``
- ``POST receipts`` ``{receipts: [...]}`` -> ``{applied, rejected: [{delivery_id, reason}]}``
  (idempotent, monotonic, compare-and-set). ``reason`` ∈ ``invalid`` /
  ``unknown`` / ``not_owned`` / ``epoch_mismatch`` / ``terminal``; a repeat of a
  receipt already applied is neither applied nor rejected. A job that reached
  ``gateway_received`` or later is not expired until 10 minutes after its
  ``expires_at``, so a final receipt (``uncertain`` included) sent around the
  deadline is applied rather than lost to the expiry sweep.
- ``POST previews`` ``{tag_id, revision, kind, png_base64, delivery_ids, epoch?}`` -> ``{stored}``
  (1-bit PNG, ≤ 64 KiB decoded). ``epoch`` (the tag epoch it was rendered
  for) is refused with 409 ``epoch_mismatch`` when it is not the current one;
  revisions compare within one epoch, and a change of owner deletes previews.

A content credential only ever sees deliveries of its profile on its
companion, and only tags that profile owns there.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api.tags import error_response
from app.tags import credentials as creds
from app.tags import routing
from app.tags.cards import iso
from app.tags.storage import (
    command_event, connector_command_json, ensure_stream, get_tag_storage,
)
from app.utils.logger import logger

PREFIX = "/api/tag-connector/v1"
API_VERSION = 1
MAX_WAIT_S = 30
MAX_PREVIEW_BYTES = 64 * 1024
MAX_U32 = 2 ** 32 - 1
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_TOUCH_EVERY_S = 60.0
_last_touch: dict[str, float] = {}


def _now_iso() -> str:
    return iso(time.time() * 1000)


async def authenticate(request: Request, kind: str | None) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Resolve the connector credential of ``request``.

    Returns ``(grant, None)`` — ``grant`` is ``{credential_id, kind,
    companion_id, profile}`` — or ``(None, error response)``."""
    header = request.headers.get("authorization") or ""
    if not header.strip():
        return None, error_response(401, "missing_credential",
                                    "Send 'Authorization: CremindTag <credential-id>.<secret>'.")
    if not creds.uses_scheme(header):
        bearer = header.strip().split(" ", 1)[0].lower() == "bearer"
        return None, error_response(401, "bearer_not_accepted" if bearer else "unsupported_scheme",
                                    "The connector API accepts only CremindTag credentials.")
    parsed = creds.parse_authorization(header)
    if parsed is None:
        return None, error_response(401, "invalid_credential", "The credential is malformed.")
    store = get_tag_storage()
    row = await store.get_credential(parsed.credential_id)
    if not creds.verify(row, parsed.secret):
        return None, error_response(401, "invalid_credential", "The credential is not valid.")
    if row.get("revoked_at") is not None:
        return None, error_response(401, "credential_revoked", "The credential has been revoked.")
    if row["kind"] == creds.KIND_CONTENT and not row.get("profile"):
        return None, error_response(401, "invalid_credential", "The credential is not valid.")
    if kind is not None and row["kind"] != kind:
        return None, error_response(403, "wrong_credential_kind",
                                    f"This endpoint needs a {kind} credential.")
    mono = time.monotonic()
    if mono - _last_touch.get(row["id"], 0.0) >= _TOUCH_EVERY_S:
        _last_touch[row["id"]] = mono
        try:
            await store.touch_credential(row["id"], time.time() * 1000)
        except Exception:  # noqa: BLE001
            logger.debug("[tags] could not record credential use", exc_info=True)
    return {
        "credential_id": row["id"],
        "kind": row["kind"],
        "companion_id": row["companion_id"],
        "profile": row.get("profile") if row["kind"] == creds.KIND_CONTENT else None,
    }, None


async def _body(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None, error_response(400, "invalid_json", "The request body must be a JSON object.")
    if not isinstance(body, dict):
        return None, error_response(400, "invalid_json", "The request body must be a JSON object.")
    return body, None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _cursor_expired(oldest: int, head: int, stream_id: str | None) -> JSONResponse:
    return error_response(410, "cursor_expired",
                          "The cursor is outside the retained history; call sync.",
                          oldest_seq=oldest, head_seq=head, stream_id=stream_id)


async def _profile_tags(profile: str, companion_id: str) -> list[dict[str, Any]]:
    store = get_tag_storage()
    devices = await store.list_devices(companion_id=companion_id)
    bridges = {d["id"]: d["hw_id"] for d in devices if d["kind"] == "bridge"}
    out = []
    for d in devices:
        if d["kind"] != "tag" or d["owner_profile"] != profile:
            continue
        out.append({
            "tag_id": d["hw_id"],
            "name": d["name"],
            "epoch": d["epoch"],
            "bridge_hw_id": bridges.get(d["bridge_device_id"] or ""),
            "width": d["width"],
            "height": d["height"],
            "planes": d["planes"],
            "rotation": d["rotation"],
            "desired_revision": d["desired_revision"],
            "displayed_revision": d["displayed_revision"],
            "clear_required": d["clear_required"],
        })
    return out


def get_tag_connector_routes() -> list[Route]:
    store = get_tag_storage

    # ── any kind ──

    async def handle_whoami(request: Request) -> JSONResponse:
        grant, err = await authenticate(request, None)
        if err is not None:
            return err
        return JSONResponse({**grant, "api_version": API_VERSION, "server_time": _now_iso()})

    # ── hardware ──

    async def handle_inventory(request: Request) -> JSONResponse:
        grant, err = await authenticate(request, creds.KIND_HARDWARE)
        if err is not None:
            return err
        body, err = await _body(request)
        if err is not None:
            return err
        devices = await store().upsert_inventory(grant["companion_id"], body)
        hw_by_id = {d["id"]: d["hw_id"] for d in devices}
        assignments = [
            {
                "tag_id": d["hw_id"],
                "owner_profile": d["owner_profile"],
                "bridge_hw_id": hw_by_id.get(d["bridge_device_id"] or ""),
                "epoch": d["epoch"],
                "rotation": d["rotation"],
            }
            for d in devices if d["kind"] == "tag"
        ]
        return JSONResponse({"devices": devices, "assignments": assignments})

    async def handle_heartbeat(request: Request) -> JSONResponse:
        grant, err = await authenticate(request, creds.KIND_HARDWARE)
        if err is not None:
            return err
        body, err = await _body(request)
        if err is not None:
            return err
        pending = await store().record_heartbeat(grant["companion_id"], body)
        return JSONResponse({"server_time": _now_iso(), "commands_pending": pending})

    async def handle_commands(request: Request) -> JSONResponse:
        grant, err = await authenticate(request, creds.KIND_HARDWARE)
        if err is not None:
            return err
        wait = _as_int(request.query_params.get("wait") or 0)
        if wait is None or wait < 0:
            return error_response(400, "invalid_wait", "'wait' must be a whole number of seconds.")
        wait = min(wait, MAX_WAIT_S)
        companion_id = grant["companion_id"]
        event = command_event(companion_id)
        deadline = time.monotonic() + wait
        while True:
            event.clear()
            rows = await store().queued_commands(companion_id)
            remaining = deadline - time.monotonic()
            if rows or remaining <= 0:
                break
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, 5.0))
            except asyncio.TimeoutError:
                pass
        return JSONResponse({"commands": [connector_command_json(r) for r in rows]})

    async def handle_claim(request: Request) -> JSONResponse:
        grant, err = await authenticate(request, creds.KIND_HARDWARE)
        if err is not None:
            return err
        row, problem = await store().claim_command(grant["companion_id"], request.path_params["command_id"])
        if problem == "not_found":
            return error_response(404, "command_not_found", "No command with that id.")
        if problem == "already_claimed":
            return error_response(409, "already_claimed",
                                  f"The command is already {row['status']}.",
                                  command=connector_command_json(row))
        return JSONResponse(connector_command_json(row))

    async def handle_result(request: Request) -> JSONResponse:
        grant, err = await authenticate(request, creds.KIND_HARDWARE)
        if err is not None:
            return err
        body, err = await _body(request)
        if err is not None:
            return err
        status = body.get("status")
        if status not in ("succeeded", "failed"):
            return error_response(422, "invalid_status", "'status' must be 'succeeded' or 'failed'.")
        result = body.get("result")
        if result is not None and not isinstance(result, dict):
            return error_response(422, "invalid_result", "'result' must be an object.")
        error = body.get("error")
        if error is not None and not isinstance(error, str):
            return error_response(422, "invalid_error", "'error' must be a string.")
        row, problem = await store().complete_command(
            grant["companion_id"], request.path_params["command_id"],
            status=status, result=result, error=error,
        )
        if problem == "not_found":
            return error_response(404, "command_not_found", "No command with that id.")
        if problem == "conflict":
            return error_response(409, "already_completed",
                                  f"The command already finished as {row['status']}.",
                                  command=connector_command_json(row))
        return JSONResponse({"ok": True, "command": connector_command_json(row)})

    # ── content ──

    async def handle_sync(request: Request) -> JSONResponse:
        grant, err = await authenticate(request, creds.KIND_CONTENT)
        if err is not None:
            return err
        body, err = await _body(request)
        if err is not None:
            return err
        profile, companion_id = grant["profile"], grant["companion_id"]
        cursor = body.get("cursor")
        if cursor is not None and (_as_int(cursor) is None or _as_int(cursor) < 0):
            return error_response(400, "invalid_cursor", "'cursor' must be a whole number or null.")
        s = store()
        async with s.engine.begin() as conn:
            await ensure_stream(conn, profile)
        stream = await s.get_stream(profile) or {}
        head = int(stream.get("next_delivery_seq") or 0)
        pruned = int((stream.get("state") or {}).get("pruned_through_seq") or 0)
        cursor_valid = cursor is not None and pruned <= _as_int(cursor) <= head
        settings = await s.get_settings(profile)
        options = await routing.effective_options_async((settings or {}).get("options"))
        return JSONResponse({
            "profile": profile,
            "companion_id": companion_id,
            "stream_id": stream.get("stream_id"),
            "cursor_valid": bool(cursor_valid),
            "oldest_seq": pruned + 1,
            "head_seq": head,
            "outstanding": await s.outstanding_jobs(profile, companion_id),
            "tags": await _profile_tags(profile, companion_id),
            "settings": {
                "enabled": bool((settings or {}).get("enabled")),
                "layout": options["layout"],
                "show_excerpts": options["show_excerpts"],
                "qr_links": options["qr_links"],
                "progress_cadence_s": options["progress_cadence_s"],
                "timezone": routing.resolved_timezone(profile, options),
                "language": options["language"],
            },
        })

    async def handle_events(request: Request) -> JSONResponse:
        grant, err = await authenticate(request, creds.KIND_CONTENT)
        if err is not None:
            return err
        q = request.query_params
        after = _as_int(q.get("after") or 0)
        if after is None or after < 0:
            return error_response(400, "invalid_cursor", "'after' must be a whole number.")
        limit = _as_int(q.get("limit") or 100)
        if limit is None or limit < 1:
            return error_response(400, "invalid_limit", "'limit' must be a positive whole number.")
        limit = min(limit, 200)
        page = await store().jobs_after(grant["profile"], grant["companion_id"], after, limit)
        head, pruned = page["head_seq"], page["pruned_through"]
        if after > head or after < pruned:
            return _cursor_expired(pruned + 1, head, page["stream_id"])
        return JSONResponse({
            "stream_id": page["stream_id"],
            "jobs": page["jobs"],
            "next_after": page["next_after"],
            "head_seq": head,
        })

    async def handle_accepted(request: Request) -> JSONResponse:
        grant, err = await authenticate(request, creds.KIND_CONTENT)
        if err is not None:
            return err
        body, err = await _body(request)
        if err is not None:
            return err
        through = body.get("through_seq")
        if through is not None and (_as_int(through) is None or _as_int(through) < 0):
            return error_response(400, "invalid_cursor", "'through_seq' must be a whole number.")
        ids = body.get("delivery_ids") or []
        if not isinstance(ids, list) or len(ids) > 1000 or any(_as_int(i) is None for i in ids):
            return error_response(422, "invalid_delivery_ids", "'delivery_ids' must be a list of delivery ids.")
        accepted = await store().accept(grant["profile"], grant["companion_id"], [_as_int(i) for i in ids])
        return JSONResponse({"accepted": accepted})

    async def handle_receipts(request: Request) -> JSONResponse:
        grant, err = await authenticate(request, creds.KIND_CONTENT)
        if err is not None:
            return err
        body, err = await _body(request)
        if err is not None:
            return err
        receipts = body.get("receipts")
        if not isinstance(receipts, list) or len(receipts) > 500 or not all(isinstance(r, dict) for r in receipts):
            return error_response(422, "invalid_receipts", "'receipts' must be a list of receipt objects.")
        applied, rejected = await store().apply_receipts(grant["profile"], grant["companion_id"], receipts)
        return JSONResponse({"applied": applied, "rejected": rejected})

    async def handle_previews(request: Request) -> JSONResponse:
        grant, err = await authenticate(request, creds.KIND_CONTENT)
        if err is not None:
            return err
        body, err = await _body(request)
        if err is not None:
            return err
        kind = body.get("kind")
        if kind not in ("desired", "displayed"):
            return error_response(422, "invalid_kind", "'kind' must be 'desired' or 'displayed'.")
        revision = _as_int(body.get("revision"))
        if revision is None or not 0 <= revision <= MAX_U32:
            return error_response(422, "invalid_revision", "'revision' must be a whole number (uint32).")
        epoch = body.get("epoch")
        if epoch is not None and (_as_int(epoch) is None or not 0 <= _as_int(epoch) <= MAX_U32):
            return error_response(422, "invalid_epoch", "'epoch' must be a whole number (uint32).")
        ids = body.get("delivery_ids") or []
        if not isinstance(ids, list) or len(ids) > 200 or any(_as_int(i) is None for i in ids):
            return error_response(422, "invalid_delivery_ids", "'delivery_ids' must be a list of delivery ids.")
        raw = body.get("png_base64")
        if not isinstance(raw, str) or not raw:
            return error_response(422, "invalid_preview", "'png_base64' is required.")
        if len(raw) > (MAX_PREVIEW_BYTES * 4) // 3 + 8:
            return error_response(422, "preview_too_large", "A preview is at most 64 KiB.")
        try:
            png = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return error_response(422, "invalid_preview", "'png_base64' is not valid base64.")
        if len(png) > MAX_PREVIEW_BYTES:
            return error_response(422, "preview_too_large", "A preview is at most 64 KiB.")
        if not png.startswith(_PNG_MAGIC):
            return error_response(422, "invalid_preview", "The preview is not a PNG image.")
        stored, problem = await store().store_preview(
            grant["companion_id"], grant["profile"], body.get("tag_id"), kind=kind, revision=revision,
            epoch=_as_int(epoch) if epoch is not None else None,
            png_base64=base64.b64encode(png).decode("ascii"), delivery_ids=[_as_int(i) for i in ids],
        )
        if problem == "tag_not_found":
            return error_response(404, "tag_not_found", "No tag with that id belongs to this profile.")
        if problem == "epoch_mismatch":
            return error_response(409, "epoch_mismatch",
                                  "The preview was rendered for another epoch of the tag.")
        return JSONResponse({"stored": stored})

    return [
        Route(f"{PREFIX}/whoami", handle_whoami, methods=["GET"]),
        Route(f"{PREFIX}/inventory", handle_inventory, methods=["POST"]),
        Route(f"{PREFIX}/heartbeat", handle_heartbeat, methods=["POST"]),
        Route(f"{PREFIX}/commands", handle_commands, methods=["GET"]),
        Route(f"{PREFIX}/commands/{{command_id}}/claim", handle_claim, methods=["POST"]),
        Route(f"{PREFIX}/commands/{{command_id}}/result", handle_result, methods=["POST"]),
        Route(f"{PREFIX}/sync", handle_sync, methods=["POST"]),
        Route(f"{PREFIX}/events", handle_events, methods=["GET"]),
        Route(f"{PREFIX}/accepted", handle_accepted, methods=["POST"]),
        Route(f"{PREFIX}/receipts", handle_receipts, methods=["POST"]),
        Route(f"{PREFIX}/previews", handle_previews, methods=["POST"]),
    ]


__all__ = ["API_VERSION", "PREFIX", "authenticate", "get_tag_connector_routes"]
