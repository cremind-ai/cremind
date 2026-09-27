"""Shared fixtures and helpers for the Cremind Tag tests.

``tagenv`` is a throwaway database migrated to head through the real Alembic
chain, with three profiles (``admin``, ``p1``, ``p2``) and the Tags store
bound to it. It is parametrized by backend: always SQLite, and PostgreSQL too
when ``CREMIND_TEST_POSTGRES_URL`` names a THROWAWAY database (each test WIPES
its ``public`` schema) and ``psycopg``/``asyncpg`` are importable. Helpers
call API handlers directly with a stub request (the pattern of
``tests/api/test_event_runs_api.py``).
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any, Callable

import pytest

pytest.importorskip("a2a")

from sqlalchemy import text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

PG_URL = os.environ.get("CREMIND_TEST_POSTGRES_URL", "").strip()
BACKENDS = ["sqlite"] + (["postgres"] if PG_URL else [])


def postgres_provider():
    """A provider on the throwaway PostgreSQL database, its schema wiped."""
    pytest.importorskip("psycopg")
    pytest.importorskip("asyncpg")
    from sqlalchemy.engine import make_url

    from app.databases.postgres import PostgresDatabaseProvider

    url = make_url(PG_URL)
    provider = PostgresDatabaseProvider(
        host=url.host or "127.0.0.1", port=url.port or 5432,
        database=url.database or "postgres", user=url.username or "postgres",
        password=url.password or "", sslmode="disable",
    )
    with provider.sync_engine().begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    return provider


def run(coro):
    return asyncio.run(coro)


def body_of(resp) -> dict:
    return json.loads(resp.body)


def make_request(username: str | None = "p1", *, path: dict | None = None, query: dict | None = None,
                 body: Any = None, headers: dict | None = None):
    async def _json():
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=username is not None, username=username or ""),
        path_params=path or {}, query_params=query or {}, json=_json,
        headers={k.lower(): v for k, v in (headers or {}).items()},
    )


def find_handler(routes, path: str, method: str) -> Callable:
    for route in routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


@pytest.fixture(params=BACKENDS)
def tagenv(request, tmp_path, monkeypatch):
    import app.auth.serial as serial_mod
    import app.databases as dbs
    import app.storage.migrations as mig
    import app.tags.storage as tag_storage
    from app.tags import journal

    if request.param == "postgres":
        provider = postgres_provider()
    else:
        provider = SqliteDatabaseProvider(str(tmp_path / "tags.db"))
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(serial_mod, "get_database_provider", lambda *a, **k: provider)
    mig.upgrade("head")
    with provider.sync_engine().begin() as c:
        for pid, name in (("pid0", "admin"), ("pid1", "p1"), ("pid2", "p2")):
            c.execute(text(
                "INSERT INTO profiles (id, name, created_at, updated_at, token_serial) "
                "VALUES (:i, :n, 0, 0, 0)"
            ), {"i": pid, "n": name})

    async def _pragmas():
        async with provider.async_engine().begin() as conn:
            await provider.apply_pragmas(conn)

    run(_pragmas())
    store = tag_storage.TagStorage(provider)
    monkeypatch.setattr(tag_storage, "_instance", store)
    journal.invalidate_enabled_cache()
    journal.configure_standalone(None)
    journal.set_wake_callback(None)
    env = SimpleNamespace(provider=provider, store=store, engine=provider.sync_engine(),
                          backend=request.param)
    yield env
    journal.configure_standalone(None)
    journal.set_wake_callback(None)
    journal.invalidate_enabled_cache()
    run(provider.dispose())


def scalar(env, sql: str, **params):
    with env.engine.connect() as c:
        return c.execute(text(sql), params).scalar()


def rows(env, sql: str, **params) -> list[dict]:
    """Rows as dicts. JSON columns come back as JSON TEXT on both backends
    (PostgreSQL drivers decode them; SQLite returns the stored text)."""
    with env.engine.connect() as c:
        out = [dict(r) for r in c.execute(text(sql), params).mappings().all()]
    for row in out:
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                row[key] = json.dumps(value)
    return out


def enable(env, profile: str, **options) -> None:
    run(env.store.save_settings(profile, enabled=True, options=options or None,
                                replace_options=bool(options)))


def hardware(env, *, tags=("T1", "T2"), bridges=("B1",)) -> dict:
    """Register a companion and report one gateway, the bridges and the tags.
    Returns ids and the hardware ``Authorization`` value."""
    from app.tags import service

    companion, cred, secret = run(service.register_companion("Desk PC", created_by="admin"))
    inv = {
        "gateways": [{"hw_id": "gw-1", "fw": "0.1.0", "board": 1, "port": "COM7"}],
        "bridges": [{"hw_id": b, "addr": i + 2, "fw": "0.1.0"} for i, b in enumerate(bridges)],
        "tags": [{"tag_id": t, "board": 16, "panel": 1, "width": 400, "height": 300, "planes": 1}
                 for t in tags],
    }
    devices = run(env.store.upsert_inventory(companion["id"], inv))
    by_hw = {(d["kind"], d["hw_id"]): d for d in devices}
    return {
        "companion_id": companion["id"],
        "auth": f"CremindTag {cred['id']}.{secret}",
        "credential_id": cred["id"],
        "tags": {t: by_hw[("tag", t)]["id"] for t in tags},
        "bridges": {b: by_hw[("bridge", b)]["id"] for b in bridges},
    }


def claim(env, device_id: str, owner: str, *, cleared: bool = True) -> dict:
    """Claim a tag for ``owner``; with ``cleared`` also report the clear done."""
    from app.tags import service

    result = run(service.claim_tag(device_id, owner=owner, requested_by="admin"))
    if cleared:
        clear_cmd = next(c for c in result["commands"] if c["kind"] == "clear_tag")
        run(env.store.complete_command(clear_cmd["companion_id"], clear_cmd["id"],
                                       status="succeeded", result=None, error=None))
    return result


def content_credential(env, profile: str, companion_id: str) -> str:
    from app.tags import service

    cred, secret = run(service.create_content_credential(profile, companion_id=companion_id, label="x"))
    return f"CremindTag {cred['id']}.{secret}"
