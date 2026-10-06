"""REST API for simple device setup — the calling profile's own hardware
(``docs/tags/setup-api.md`` §1). Every route requires a session JWT;
everything is scoped to ``request.user.username`` and its profile UUID, and
another profile's ids answer 404.

- ``GET    /api/tags/connections``                 gateways/workers with their bridges and tags
- ``POST   /api/tags/setup-sessions``              ``{operation, server_url, companion_id?}`` -> 201 ``{session, launch_url}``
- ``GET    /api/tags/setup-sessions/{id}``         -> ``{session}``
- ``POST   /api/tags/setup-sessions/{id}/confirm`` -> ``{session}`` (same sign-in that created it)
- ``DELETE /api/tags/setup-sessions/{id}``         -> ``{session}``
- ``POST   /api/tags/discovery``                   ``{role, setup_code, gateway_id?, duration_s?}`` -> 201 ``{discovery}``
- ``GET    /api/tags/discovery/{id}``              -> ``{discovery}``
- ``POST   /api/tags/pairings``                    ``{discovery_id, candidate_id, name?}`` -> 201 ``{pairing}``
- ``GET    /api/tags/pairings/{id}`` / ``DELETE``  -> ``{pairing}``
- ``POST   /api/tags/devices/{id}/unpair``         ``{force?}`` -> ``{operation, device}``
- ``POST   /api/tags/devices/{id}/pause`` / ``resume`` -> ``{device}``
- ``POST   /api/tags/devices/{id}/move``           ``{bridge_id}`` -> ``{operation}`` (a tag onto its gateway,
  while that serves tags on its own radio, or a ready bridge of it)
- ``POST   /api/tags/devices/{id}/test``           -> 201 ``{delivery}``
- ``POST   /api/tags/recoveries``                  ``{companion_id, server_url}`` -> 201 ``{recovery, session, launch_url}``
- ``GET    /api/tags/recoveries/{id}``             -> ``{recovery}``

Mutations honour ``Idempotency-Key`` (:mod:`app.tags.idempotency`). Setup
codes are never logged.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import require_auth
from app.api.tags import json_body, tag_error_response
from app.tags import idempotency
from app.tags.service import TagError
from app.utils.logger import logger


def _profile(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


async def _optional_body(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    raw = await request.body()
    if not raw.strip():
        return {}, None
    return await json_body(request)


def get_tags_setup_routes() -> list[Route]:
    from app.tags import operations, setup

    def guarded(fn):
        async def handler(request: Request) -> JSONResponse:
            unauth = require_auth(request)
            if unauth is not None:
                return unauth
            try:
                return await fn(request)
            except TagError as exc:
                return tag_error_response(exc)
        return handler

    def once(route: str, fn):
        """A mutation answered once per Idempotency-Key (per profile)."""
        async def handler(request: Request) -> JSONResponse:
            body, err = await _optional_body(request)
            if err is not None:
                return err
            key = idempotency.request_key(request, body)

            async def produce() -> JSONResponse:
                try:
                    return await fn(request, body)
                except TagError as exc:
                    return tag_error_response(exc)
            return await idempotency.run_once(f"profile:{_profile(request)}", route + ":" +
                                              str(request.path_params), key, body, produce)
        return guarded(handler)

    async def connections(request: Request) -> JSONResponse:
        return JSONResponse(await operations.connections(_profile(request)))

    async def create_session(request: Request, body: dict[str, Any]) -> JSONResponse:
        operations.require_simple_setup()
        session, url = await setup.create_session(
            _profile(request), operation=body.get("operation"), server_url=body.get("server_url"),
            companion_id=body.get("companion_id"), authorization=request.headers.get("authorization"))
        logger.info(f"[tags] setup session {session['id']} ({session['operation']}) for {_profile(request)}")
        return JSONResponse({"session": session, "launch_url": url}, status_code=201)

    async def get_session(request: Request) -> JSONResponse:
        return JSONResponse({"session": await setup.get_session(_profile(request), request.path_params["session_id"])})

    async def confirm(request: Request, body: dict[str, Any]) -> JSONResponse:
        session = await setup.confirm_session(_profile(request), request.path_params["session_id"],
                                              authorization=request.headers.get("authorization"))
        return JSONResponse({"session": session})

    async def cancel(request: Request) -> JSONResponse:
        return JSONResponse({"session": await setup.cancel_session(_profile(request),
                                                                   request.path_params["session_id"])})

    async def discovery(request: Request, body: dict[str, Any]) -> JSONResponse:
        return JSONResponse({"discovery": await operations.start_discovery(_profile(request), body)},
                            status_code=201)

    async def get_discovery(request: Request) -> JSONResponse:
        return JSONResponse({"discovery": await operations.get_discovery(_profile(request),
                                                                         request.path_params["op_id"])})

    async def pairing(request: Request, body: dict[str, Any]) -> JSONResponse:
        return JSONResponse({"pairing": await operations.start_pairing(_profile(request), body)}, status_code=201)

    async def import_tag(request: Request, body: dict[str, Any]) -> JSONResponse:
        out = await operations.start_import(_profile(request), body)
        logger.info(f"[tags] tag import requested by {_profile(request)}")
        return JSONResponse({"pairing": out}, status_code=201)

    async def get_pairing(request: Request) -> JSONResponse:
        return JSONResponse({"pairing": await operations.get_pairing(_profile(request), request.path_params["op_id"])})

    async def cancel_pairing(request: Request) -> JSONResponse:
        return JSONResponse({"pairing": await operations.cancel_pairing(_profile(request),
                                                                        request.path_params["op_id"])})

    async def unpair(request: Request, body: dict[str, Any]) -> JSONResponse:
        out = await operations.unpair(_profile(request), request.path_params["device_id"], body)
        logger.info(f"[tags] device {request.path_params['device_id']} removal requested by {_profile(request)}")
        return JSONResponse(out)

    def pause_handler(paused: bool):
        async def handler(request: Request, body: dict[str, Any]) -> JSONResponse:
            return JSONResponse(await operations.set_paused(_profile(request), request.path_params["device_id"],
                                                            paused))
        return handler

    async def move(request: Request, body: dict[str, Any]) -> JSONResponse:
        return JSONResponse(await operations.move_tag(_profile(request), request.path_params["device_id"], body))

    async def test_card(request: Request, body: dict[str, Any]) -> JSONResponse:
        return JSONResponse({"delivery": await operations.send_test(_profile(request),
                                                                    request.path_params["device_id"])},
                            status_code=201)

    async def recovery(request: Request, body: dict[str, Any]) -> JSONResponse:
        out = await operations.start_recovery(_profile(request), body,
                                              authorization=request.headers.get("authorization"))
        return JSONResponse(out, status_code=201)

    async def get_recovery(request: Request) -> JSONResponse:
        return JSONResponse({"recovery": await operations.get_recovery(_profile(request),
                                                                       request.path_params["op_id"])})

    async def delete_session(request: Request) -> JSONResponse:
        return await cancel(request)

    return [
        Route("/api/tags/connections", guarded(connections), methods=["GET"]),
        Route("/api/tags/setup-sessions", once("create_session", create_session), methods=["POST"]),
        Route("/api/tags/setup-sessions/{session_id}", guarded(get_session), methods=["GET"]),
        Route("/api/tags/setup-sessions/{session_id}", guarded(delete_session), methods=["DELETE"]),
        Route("/api/tags/setup-sessions/{session_id}/confirm", once("confirm", confirm), methods=["POST"]),
        Route("/api/tags/discovery", once("discovery", discovery), methods=["POST"]),
        Route("/api/tags/discovery/{op_id}", guarded(get_discovery), methods=["GET"]),
        Route("/api/tags/pairings", once("pairing", pairing), methods=["POST"]),
        Route("/api/tags/pairings/{op_id}", guarded(get_pairing), methods=["GET"]),
        Route("/api/tags/pairings/{op_id}", guarded(cancel_pairing), methods=["DELETE"]),
        Route("/api/tags/imports", once("import", import_tag), methods=["POST"]),
        Route("/api/tags/devices/{device_id}/unpair", once("unpair", unpair), methods=["POST"]),
        Route("/api/tags/devices/{device_id}/pause", once("pause", pause_handler(True)), methods=["POST"]),
        Route("/api/tags/devices/{device_id}/resume", once("resume", pause_handler(False)), methods=["POST"]),
        Route("/api/tags/devices/{device_id}/move", once("move", move), methods=["POST"]),
        Route("/api/tags/devices/{device_id}/test", once("test", test_card), methods=["POST"]),
        Route("/api/tags/recoveries", once("recovery", recovery), methods=["POST"]),
        Route("/api/tags/recoveries/{op_id}", guarded(get_recovery), methods=["GET"]),
    ]


__all__ = ["get_tags_setup_routes"]
