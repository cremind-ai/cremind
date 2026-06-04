"""Version & health endpoints — unauthenticated.

These are the two read-only endpoints the upgrader and UI rely on before
they have (or in spite of) a JWT. Both must be reachable without auth so a
freshly-installed UI can probe a running backend before login, and the
upgrader can compare versions without holding a token.

Compatibility contract — see ``app/__version__.py``:

- ``backend``               : SemVer of this build.
- ``schema``                : Alembic head revision id (drives upgrade UX).
- ``min_supported_upgrade_from``
                            : oldest backend SemVer this build can migrate
                              from. Older installs must export-and-import.
- ``channel``               : release channel this build was installed from
                              (``production`` / ``test`` / ``dev``). Lets a
                              web client display the PyPI source without
                              having to go through the Electron preload.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.__version__ import (
    MIN_SUPPORTED_UPGRADE_FROM,
    __version__,
)
from app.upgrade.channel import get_channel


async def get_version(_request: Request) -> JSONResponse:
    schema = _current_schema_revision()
    return JSONResponse({
        "backend": __version__,
        "schema": schema,
        "min_supported_upgrade_from": MIN_SUPPORTED_UPGRADE_FROM,
        "channel": get_channel(),
    })


async def get_health(_request: Request) -> JSONResponse:
    """Probe each subsystem; return 200 only when all are healthy.

    Used as the post-upgrade gate by the upgrader — a 200 here means the
    new build came up cleanly and the upgrade flow can commit (drop the
    backup retention window). On 503 the upgrader rolls back.

    When the server is in deferred-storage mode (no ``bootstrap.toml``
    yet — first-run before the Setup Wizard completes), ``db`` reports
    ``"deferred"`` and the response is still 200 with ``status =
    "setup_pending"`` so Electron's healthcheck succeeds and the wizard
    can render.
    """
    db_status = await _probe_db()
    vs_status = _probe_vectorstore()

    overall_ok = db_status in ("ok", "deferred") and vs_status in ("ok", "disabled")
    if db_status == "deferred":
        status_text = "setup_pending"
    elif overall_ok:
        status_text = "ok"
    else:
        status_text = "degraded"
    body = {
        "status": status_text,
        "db": db_status,
        "vectorstore": vs_status,
    }
    return JSONResponse(body, status_code=200 if overall_ok else 503)


def _current_schema_revision() -> str:
    """Return the Alembic head revision id, or ``"unknown"`` if unavailable.

    Reading from the live database (rather than hardcoding) keeps this in
    sync with whatever migration ran last — important during the rollout
    window where the runtime version and the schema may briefly disagree.

    Returns ``"unknown"`` early when no ``bootstrap.toml`` exists — the
    Setup Wizard hasn't run yet so building a sync engine would
    materialise a SQLite file before the user has chosen a backend.
    """
    from app.config.bootstrap import bootstrap_exists
    if not bootstrap_exists():
        return "unknown"
    try:
        from app.databases import get_database_provider

        provider = get_database_provider()
        engine = provider.sync_engine()
        with engine.connect() as conn:
            from sqlalchemy import text
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
            return row[0] if row else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


async def _probe_db() -> str:
    # Pre-bootstrap: don't touch the provider — calling ``health_check()``
    # on SQLite would open a connection and create ``cremind.db``.
    from app.config.bootstrap import bootstrap_exists
    if not bootstrap_exists():
        return "deferred"
    try:
        from app.databases import get_database_provider

        await get_database_provider().health_check()
        return "ok"
    except Exception:  # noqa: BLE001
        return "error"


def _probe_vectorstore() -> str:
    """Probe the vector store. ``disabled`` is a healthy state."""
    try:
        from app.config.settings import BaseConfig

        if not BaseConfig.is_embedding_enabled():
            return "disabled"
        # A live vector store is wired up in app/server.py at boot. We
        # don't reach into it here — the embedding-stream route already
        # surfaces detailed status. For the health probe, the fact that
        # embedding is enabled and the backend is up is enough.
        return "ok"
    except Exception:  # noqa: BLE001
        return "error"


def get_version_routes() -> list[Route]:
    return [
        Route("/version", get_version, methods=["GET"]),
        Route("/health", get_health, methods=["GET"]),
    ]
