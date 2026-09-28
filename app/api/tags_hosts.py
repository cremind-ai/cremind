"""REST API for gateway computers — where Cremind drives USB gateways itself
(:mod:`app.tags.hosts`). Every route requires a session JWT and acts for
``request.user.username``; another profile's desktop computer answers 404.

- ``GET    /api/tags/hosts``                              -> ``{hosts, active}``: the computers this profile may
  use (the server's own; its own desktop computers), their readiness, USB access and status
- ``POST   /api/tags/hosts/{host_id}/scan``               -> 202 ``{operation}``: search that computer's USB ports
- ``POST   /api/tags/hosts/{host_id}/prepare``            -> 202 ``{operation}``: install the server's gateway
  components (admin)
- ``PUT    /api/tags/hosts/{host_id}/access/{profile_id}`` ``{granted}`` -> ``{…}``: let a profile search and claim
  unclaimed hardware on the server's USB ports (admin)
- ``DELETE /api/tags/hosts/{host_id}``                    -> ``{host, connections}``: remove one of this profile's
  desktop gateway computers (its credential stops working; its connections stay, offline until moved)
- ``POST   /api/tags/connections``                        ``{host_id, candidate_id, name?}`` -> 202 ``{operation}``:
  connect a gateway a search found
- ``GET    /api/tags/operations/{op_id}`` / ``DELETE``    -> ``{operation}``: a search, connection or
  preparation, and cancelling one that has not claimed anything

Mutations honour ``Idempotency-Key``. Requests never name a serial port: a
search result is an opaque candidate bound to this profile, the computer and
the device it saw.
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


def _profile(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


async def _optional_body(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    raw = await request.body()
    if not raw.strip():
        return {}, None
    return await json_body(request)


def get_tags_hosts_routes() -> list[Route]:
    from app.tags import hosts
    from app.tags.operations import require_simple_setup

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
                    require_simple_setup()
                    return await fn(request, body)
                except TagError as exc:
                    return tag_error_response(exc)
            return await idempotency.run_once(f"profile:{_profile(request)}", route + ":" +
                                              str(request.path_params), key, body, produce)
        return guarded(handler)

    async def list_hosts(request: Request) -> JSONResponse:
        return JSONResponse(await hosts.list_hosts(_profile(request)))

    async def scan(request: Request, body: dict[str, Any]) -> JSONResponse:
        op = await hosts.start_scan(_profile(request), request.path_params["host_id"])
        return JSONResponse({"operation": op}, status_code=202)

    async def prepare(request: Request, body: dict[str, Any]) -> JSONResponse:
        op = await hosts.start_prepare(_profile(request), request.path_params["host_id"])
        return JSONResponse({"operation": op}, status_code=202)

    async def access(request: Request, body: dict[str, Any]) -> JSONResponse:
        return JSONResponse(await hosts.set_access(_profile(request), request.path_params["host_id"],
                                                   request.path_params["profile_id"], body.get("granted")))

    async def remove(request: Request, body: dict[str, Any]) -> JSONResponse:
        return JSONResponse(await hosts.remove_host(_profile(request), request.path_params["host_id"]))

    async def connect(request: Request, body: dict[str, Any]) -> JSONResponse:
        op = await hosts.start_connect(_profile(request), body)
        return JSONResponse({"operation": op}, status_code=202)

    async def get_operation(request: Request) -> JSONResponse:
        return JSONResponse({"operation": await hosts.get_operation(_profile(request), request.path_params["op_id"])})

    async def cancel_operation(request: Request) -> JSONResponse:
        return JSONResponse({"operation": await hosts.cancel_operation(_profile(request),
                                                                       request.path_params["op_id"])})

    return [
        Route("/api/tags/hosts", guarded(list_hosts), methods=["GET"]),
        Route("/api/tags/hosts/{host_id}/scan", once("host_scan", scan), methods=["POST"]),
        Route("/api/tags/hosts/{host_id}/prepare", once("host_prepare", prepare), methods=["POST"]),
        Route("/api/tags/hosts/{host_id}/access/{profile_id}", once("host_access", access), methods=["PUT"]),
        Route("/api/tags/hosts/{host_id}", once("host_remove", remove), methods=["DELETE"]),
        Route("/api/tags/connections", once("connect", connect), methods=["POST"]),
        Route("/api/tags/operations/{op_id}", guarded(get_operation), methods=["GET"]),
        Route("/api/tags/operations/{op_id}", guarded(cancel_operation), methods=["DELETE"]),
    ]


__all__ = ["get_tags_hosts_routes"]
