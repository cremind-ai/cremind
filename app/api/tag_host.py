"""The hardware host API: ``/api/tag-host/v1/*`` — a desktop hardware host's side of Cremind.

A Cremind desktop installation enrolled as a gateway computer (for a remote
or container backend, which cannot see its USB ports) authenticates every
request with the host credential it received at enrollment, scoped to that
computer and ONE profile — and nothing else::

    Authorization: CremindHost <credential-id>.<secret>

It only ever connects out (no port opens on the gateway computer):

- ``POST hello``                        ``{name, platform, version, capabilities, status}`` -> ``{host_id, work_pending}``
- ``GET  work?wait=<s≤30>``             -> ``{work: [{id, kind, args, profile, profile_id}]}``: its profile's searches
  and connections for this computer (each taken as it is handed out)
- ``POST operations/{id}/progress``     ``{stage?, detail?, state?, found?, unidentified?, error?}`` -> ``{operation}``
- ``POST operations/{id}/worker``       ``{controller_pub, credentials: {hardware_sha256, content_sha256}, gateway}``
  -> ``{companion_id, credentials, operation_id, profile, generation, server}``: a connection's worker records
- ``POST leave``                        -> ``{host}``: the computer forgets its enrollment (its credential stops working)
- ``POST workers/{companion_id}/adopt`` -> ``{companion_id, generation, host_id, profile}``: take over a worker the
  older Cremind Connect ran on this computer (moved in as it was)

The backend's own hardware host calls the same functions in process
(:mod:`app.tags.hosting.local_host`). The gateway workers themselves use the
connector API (``/api/tag-connector/v1``) with their own credentials.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api.tags import tag_error_response
from app.tags import hosts
from app.tags.service import TagError

PREFIX = "/api/tag-host/v1"


async def _principal(request: Request) -> hosts.HostPrincipal:
    parsed = hosts.parse_host_authorization(request.headers.get("authorization"))
    if parsed is None:
        raise TagError(401, "invalid_credential", f"Send 'Authorization: {hosts.HOST_SCHEME} <credential-id>.<secret>'.")
    return await hosts.authenticate_host(*parsed)


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        raise TagError(400, "invalid_json", "The request body must be a JSON object.")
    return body


def get_tag_host_routes() -> list[Route]:
    def route(run):
        async def handler(request: Request) -> JSONResponse:
            try:
                principal = await _principal(request)
                return JSONResponse(await run(request, principal))
            except TagError as exc:
                return tag_error_response(exc)
        return handler

    async def hello(request: Request, p: hosts.HostPrincipal) -> dict[str, Any]:
        return await hosts.host_hello(p, await _body(request))

    async def work(request: Request, p: hosts.HostPrincipal) -> dict[str, Any]:
        return await hosts.host_work(p, request.query_params.get("wait") or 0)

    async def progress(request: Request, p: hosts.HostPrincipal) -> dict[str, Any]:
        return await hosts.host_progress(p, request.path_params["op_id"], await _body(request))

    async def worker(request: Request, p: hosts.HostPrincipal) -> dict[str, Any]:
        return await hosts.host_register_worker(p, request.path_params["op_id"], await _body(request))

    async def leave(request: Request, p: hosts.HostPrincipal) -> dict[str, Any]:
        return await hosts.host_leave(p)

    async def adopt(request: Request, p: hosts.HostPrincipal) -> dict[str, Any]:
        return await hosts.adopt_legacy_worker(p, request.path_params["companion_id"])

    return [
        Route(f"{PREFIX}/hello", route(hello), methods=["POST"]),
        Route(f"{PREFIX}/work", route(work), methods=["GET"]),
        Route(f"{PREFIX}/operations/{{op_id}}/progress", route(progress), methods=["POST"]),
        Route(f"{PREFIX}/operations/{{op_id}}/worker", route(worker), methods=["POST"]),
        Route(f"{PREFIX}/leave", route(leave), methods=["POST"]),
        Route(f"{PREFIX}/workers/{{companion_id}}/adopt", route(adopt), methods=["POST"]),
    ]


__all__ = ["PREFIX", "get_tag_host_routes"]
