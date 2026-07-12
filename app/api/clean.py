"""REST endpoint for the per-profile "clean data" feature.

``POST /api/clean`` wipes data for the caller's own profile (resolved from the token,
like every other module) — either a custom subset of components or a ``working`` /
``factory`` preset. Thin by design: auth + validation + busy-guard, then delegate to
:func:`app.reset.engine.run_clean`. Scoped strictly to ``request.user.username``, so a
token can only ever clean its own profile.
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.reset.components import expand_scope
from app.reset.deps import Deps
from app.reset.engine import run_clean
from app.utils.logger import logger


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _busy() -> JSONResponse | None:
    """409 while a backup/restore or a blueprint import is running.

    A factory reset must not race a whole-system restore/import — both mutate the
    same profile-scoped rows and on-disk trees.
    """
    try:
        from app.backup.status import any_running
        if any_running():
            return JSONResponse(
                {"error": "busy", "message": "A backup/restore is in progress; try again shortly."},
                status_code=409,
            )
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.blueprint.session import find_active_session
        if find_active_session() is not None:
            return JSONResponse(
                {"error": "busy", "message": "A blueprint import is in progress; try again shortly."},
                status_code=409,
            )
    except Exception:  # noqa: BLE001
        pass
    return None


def get_clean_routes(
    conversation_storage,
    config_storage,
    *,
    registry=None,
    drop_profile_embeddings=None,
) -> list[Route]:
    """Route factory for ``POST /api/clean`` (see :mod:`app.reset`)."""

    async def handle_clean(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "Invalid request body"}, status_code=400)

        scope = body.get("scope") or "custom"
        components = body.get("components") or []
        if not isinstance(components, list):
            return JSONResponse({"error": "'components' must be a list"}, status_code=400)

        try:
            component_set = expand_scope(scope, components)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        busy = await asyncio.to_thread(_busy)
        if busy is not None:
            return busy

        # Resolve the document service lazily (it's built during boot, after the
        # route factory runs). registry falls back to the booted state's registry.
        document_service = None
        state_registry = registry
        try:
            from app.runtime import get_state
            state = get_state()
            document_service = getattr(state, "document_service", None)
            if state_registry is None:
                state_registry = getattr(state, "registry", None)
        except Exception:  # noqa: BLE001
            pass

        deps = Deps(
            conversation_storage=conversation_storage,
            config_storage=config_storage,
            registry=state_registry,
            drop_profile_embeddings=drop_profile_embeddings,
            document_service=document_service,
        )

        logger.info(
            f"[clean] profile={profile} scope={scope} "
            f"components={sorted(component_set)}"
        )
        report = await run_clean(profile, component_set, deps)
        return JSONResponse({
            "success": not report["errors"],
            "scope": scope,
            "profile": profile,
            **report,
        })

    return [Route("/api/clean", endpoint=handle_clean, methods=["POST"])]
