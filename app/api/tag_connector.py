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
  Each ``bridges[]`` item may carry its assignment-table capacity from CAPS:
  ``"max_tags": <1..255>`` (nRF52832: 10, nRF52840: 20) and
  ``"assigned": <0..255>``. They are kept in the bridge's ``info``; a bad
  value is dropped and the last good one kept. With ``max_tags`` known,
  Cremind refuses to claim or assign a tag onto a bridge that already holds
  that many (409 ``bridge_full`` to the admin), so no such ``assign_tag`` is
  queued.
- ``POST heartbeat``  ``{companion, queue, devices}`` -> ``{server_time, commands_pending}``
- ``GET  commands?wait=<s≤30>`` -> ``{commands}``; returns early when one is queued.
  Ownership commands (``assign_tag`` / ``clear_tag``) come first. A profile's
  identify / refresh is queued at most once per tag while pending.
- ``POST commands/{id}/claim`` -> 200 the command object, or 409 ``already_claimed``
- ``POST commands/{id}/result`` ``{status: succeeded|failed, result?, error?}`` -> ``{ok, command}``;
  the same status again is a no-op, a different one 409 ``already_completed``.
  A command Cremind stopped waiting for (``expired``) still takes a late
  result. A failed or expired ``clear_tag`` is re-queued (3 attempts in all),
  after which the tag's status reads ``clear_failed``. A failed ``assign_tag``
  at the tag's current epoch sets the tag's status to ``assign_failed`` (an
  admin assigns it to another bridge or releases it; a later success of an
  assign clears it). A bridge whose table is full reports
  ``{"status": "failed", "result": {"error": "bridge_full", "max_tags": <n>}}``:
  the command's ``error`` becomes ``bridge_full`` (``result.error`` is used
  when ``error`` is omitted), the tag is no longer counted on that bridge,
  and ``max_tags`` is recorded on the bridge.

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

This module is only the HTTP adapter: header and body parsing. The rules live
in :mod:`app.tags.connector_service`, which a worker hosted inside the backend
calls directly (:mod:`app.tags.hosting.local_connector`), so both enforce the
same authentication, ownership, lease and durability rules.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api.tags import error_response, tag_error_response
from app.tags import connector_service as svc
from app.tags import credentials as creds
from app.tags.service import TagError

PREFIX = "/api/tag-connector/v1"
API_VERSION = svc.API_VERSION


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
    try:
        return await svc.authenticate(parsed.credential_id, parsed.secret, kind), None
    except TagError as exc:
        return None, tag_error_response(exc)


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        raise TagError(400, "invalid_json", "The request body must be a JSON object.")
    return body


def _handler(endpoint: svc.Endpoint):
    """Authenticate for the endpoint's credential kind, run it, answer JSON or the refusal's error body."""

    async def handler(request: Request) -> JSONResponse:
        grant, err = await authenticate(request, endpoint.kind)
        if err is not None:
            return err
        try:
            call = svc.Call(dict(request.path_params), request.query_params,
                            await _body(request) if endpoint.body else None)
            return JSONResponse(await endpoint.run(grant, call))
        except TagError as exc:
            return tag_error_response(exc)

    return handler


def get_tag_connector_routes() -> list[Route]:
    return [Route(f"{PREFIX}{e.path}", _handler(e), methods=[e.method]) for e in svc.ENDPOINTS]


__all__ = ["API_VERSION", "PREFIX", "authenticate", "get_tag_connector_routes"]
