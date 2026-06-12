"""Tests for the public ``GET /api/profiles/names`` endpoint.

The login screen ("Select a profile or log in") shows a dropdown of
available profiles instead of a free-text name field. That dropdown is
fed by a pre-auth endpoint, so these tests pin its contract:

- it answers without any authentication;
- it returns ONLY profile names — no ids, timestamps, or other fields
  from ``list_profiles`` rows;
- internal ``__``-prefixed profiles stay hidden;
- its error path does not leak exception details (it is public);
- the authenticated ``GET /api/profiles`` keeps requiring a token.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Callable

from app.api.profiles import get_profile_routes


def _make_storage(rows: list[dict]) -> SimpleNamespace:
    async def list_profiles():
        return rows

    return SimpleNamespace(list_profiles=list_profiles)


def _get_handler(storage: SimpleNamespace, path: str, method: str) -> Callable:
    routes = get_profile_routes(storage)  # type: ignore[arg-type]
    for route in routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} route not registered")


def _body(response) -> dict:
    return json.loads(response.body)


def test_names_endpoint_requires_no_auth() -> None:
    """The handler must not even look at ``request.user`` — the stub
    request has no ``user`` attribute, so any auth dependency would
    raise instead of answering."""
    storage = _make_storage([{"name": "admin", "id": 1}, {"name": "lee", "id": 2}])
    handler = _get_handler(storage, "/api/profiles/names", "GET")

    response = asyncio.run(handler(SimpleNamespace()))

    assert response.status_code == 200
    assert _body(response) == {"profiles": ["admin", "lee"]}


def test_names_endpoint_exposes_only_names() -> None:
    """Rows carry extra fields (ids, timestamps); none may leak."""
    storage = _make_storage(
        [{"name": "admin", "id": 7, "created_at": "2026-01-01", "persona": "secret"}]
    )
    handler = _get_handler(storage, "/api/profiles/names", "GET")

    body = _body(asyncio.run(handler(SimpleNamespace())))

    assert set(body.keys()) == {"profiles"}
    assert body["profiles"] == ["admin"]


def test_names_endpoint_hides_internal_profiles() -> None:
    storage = _make_storage([{"name": "__system"}, {"name": "lee"}])
    handler = _get_handler(storage, "/api/profiles/names", "GET")

    body = _body(asyncio.run(handler(SimpleNamespace())))

    assert body["profiles"] == ["lee"]


def test_names_endpoint_error_path_leaks_nothing() -> None:
    async def boom():
        raise RuntimeError("pg://user:hunter2@db/cremind exploded")

    storage = SimpleNamespace(list_profiles=boom)
    handler = _get_handler(storage, "/api/profiles/names", "GET")

    response = asyncio.run(handler(SimpleNamespace()))

    assert response.status_code == 500
    assert "hunter2" not in response.body.decode()
    assert _body(response) == {"error": "Internal server error"}


def test_authenticated_list_still_requires_token() -> None:
    """Regression pin: adding the public route must not loosen the
    authenticated ``GET /api/profiles``."""
    storage = _make_storage([{"name": "admin"}])
    handler = _get_handler(storage, "/api/profiles", "GET")

    request = SimpleNamespace(user=SimpleNamespace(is_authenticated=False))
    response = asyncio.run(handler(request))

    assert response.status_code == 401
