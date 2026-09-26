"""Endpoints that moved: answer their old paths with 410 Gone and the new one.

The personal-document search was served under ``/api/userdocs/*`` before the
search-tool rename; it is ``/api/documentation-search/*`` now, path for path
(``/api/userdocs/files/{fid}/text`` became
``/api/documentation-search/files/{fid}/text``, and so on). A web UI tab or a
``cremind`` CLI built before the rename still calls the old paths. Without these
routes a ``GET`` would fall through to the SPA fallback and come back as the
index page — HTML where the client expected JSON — and a write would be a bare
404/405. A 410 with a machine-readable ``EndpointRenamed`` body and the exact
``replacement`` path tells the person (and the code) what happened instead.

Every method is answered, and nothing is read from the request but its path:
the old endpoint does nothing any more, so there is nothing to authenticate. An
old UI's ``EventSource`` on ``/api/userdocs/stream`` gets the 410 and stops,
which is what a non-200 response does to an event stream.

Nothing here can shadow a live route: no current route lives under
``/api/userdocs``.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

#: Retired prefix → the prefix that replaced it, path for path.
RENAMED_PREFIXES: dict[str, str] = {
    "/api/userdocs": "/api/documentation-search",
}

_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def replacement_path(old_prefix: str, rest: str) -> str:
    """Where ``<old_prefix>/<rest>`` lives now."""
    new_prefix = RENAMED_PREFIXES[old_prefix]
    rest = rest.strip("/")
    return f"{new_prefix}/{rest}" if rest else new_prefix


def _renamed_endpoint(old_prefix: str):
    async def handle(request: Request) -> JSONResponse:
        replacement = replacement_path(old_prefix, request.path_params.get("rest", ""))
        old_path = request.scope.get("path") or old_prefix
        return JSONResponse(
            {
                "error": "EndpointRenamed",
                "message": (
                    f"{old_path} moved to {replacement} — update the client "
                    "(reload the web UI / pip install -U cremind); the CLI command "
                    "is now `cremind docs`."
                ),
                "replacement": replacement,
            },
            status_code=410,
            headers={"Cache-Control": "no-store"},
        )

    return handle


def get_retired_routes() -> list[Route]:
    """One pair of routes per retired prefix: the prefix itself and anything
    beneath it."""
    routes: list[Route] = []
    for old_prefix in RENAMED_PREFIXES:
        endpoint = _renamed_endpoint(old_prefix)
        routes.append(Route(old_prefix, endpoint, methods=_ALL_METHODS))
        routes.append(Route(f"{old_prefix}/{{rest:path}}", endpoint, methods=_ALL_METHODS))
    return routes


__all__ = ["RENAMED_PREFIXES", "get_retired_routes", "replacement_path"]
