"""The bootstrap API Cremind Connect uses during a setup session:
``/api/tag-setup/v1/*`` (cremind-tag ``docs/setup-api.md`` §2).

``Authorization: CremindSetup <session_id>.<token>`` — the token from the
launch link, whose SHA-256 is all Cremind stores — plus, from ``bind`` on, an
Ed25519 proof by the Connect installation key the session was bound to
(``proof`` in POST bodies, ``X-Cremind-Connect-Proof`` on GET). Nothing else
is accepted here and these credentials are accepted nowhere else
(:class:`app.middleware.tag_connector_guard.TagConnectorGuard`).

- ``POST sessions/{id}/bind``     ``{installation, proof}``
- ``GET  sessions/{id}``          (proof header)
- ``POST sessions/{id}/approve``  ``{gateway, proof}``
- ``POST sessions/{id}/redeem``   ``{idempotency_key, controller_pub, credentials, proof}``
- ``POST sessions/{id}/fail``     ``{code, message, proof}``
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api.tags import error_response, json_body, tag_error_response
from app.tags import setup
from app.tags.service import TagError
from app.utils.logger import logger

PREFIX = "/api/tag-setup/v1"


def _refuse_other_schemes(request: Request) -> JSONResponse | None:
    header = (request.headers.get("authorization") or "").strip()
    scheme = header.split(" ", 1)[0].lower() if header else ""
    if scheme != setup.SCHEME.lower():
        return error_response(401, "invalid_setup_credential",
                              "This API accepts only the setup credential of a Cremind Connect link.")
    return None


def get_tag_setup_bootstrap_routes() -> list[Route]:
    def handler(action: str):
        async def run(request: Request) -> JSONResponse:
            refused = _refuse_other_schemes(request)
            if refused is not None:
                return refused
            session_id = request.path_params["session_id"]
            header = request.headers.get("authorization")
            try:
                if action == "poll":
                    return JSONResponse(await setup.poll(session_id, header,
                                                         request.headers.get("x-cremind-connect-proof")))
                body, err = await json_body(request)
                if err is not None:
                    return err
                fn = {"bind": setup.bind, "approve": setup.approve, "redeem": setup.redeem,
                      "fail": setup.fail}[action]
                out = await fn(session_id, header, body)
            except TagError as exc:
                if exc.status >= 500 or exc.status == 401:
                    logger.info(f"[tags] setup {action} refused: {exc.code}")
                return tag_error_response(exc)
            if action in ("bind", "redeem", "fail"):
                logger.info(f"[tags] setup session {session_id}: {action}")
            return JSONResponse(out)
        return run

    return [
        Route(f"{PREFIX}/sessions/{{session_id}}/bind", handler("bind"), methods=["POST"]),
        Route(f"{PREFIX}/sessions/{{session_id}}", handler("poll"), methods=["GET"]),
        Route(f"{PREFIX}/sessions/{{session_id}}/approve", handler("approve"), methods=["POST"]),
        Route(f"{PREFIX}/sessions/{{session_id}}/redeem", handler("redeem"), methods=["POST"]),
        Route(f"{PREFIX}/sessions/{{session_id}}/fail", handler("fail"), methods=["POST"]),
    ]


__all__ = ["PREFIX", "get_tag_setup_bootstrap_routes"]
