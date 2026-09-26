"""Old ``/api/userdocs/*`` paths answer 410 Gone with where they moved.

Documentation search's REST surface moved to ``/api/documentation-search/*``
path for path. A UI tab or CLI built before the rename must learn that from a
clear ``EndpointRenamed`` body (and ``replacement``) rather than from the SPA
index page a stray GET would otherwise get, or a bare 404.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.applications import Starlette
from starlette.routing import Match
from starlette.testclient import TestClient

from app.api.retired import get_retired_routes, replacement_path


@pytest.fixture
def client() -> TestClient:
    return TestClient(Starlette(routes=get_retired_routes()))


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("old,new", [
    ("/api/userdocs", "/api/documentation-search"),
    ("/api/userdocs/", "/api/documentation-search"),
    ("/api/userdocs/status", "/api/documentation-search/status"),
    ("/api/userdocs/files/k7m2xq9a/text", "/api/documentation-search/files/k7m2xq9a/text"),
    ("/api/userdocs/research/3f9c/continue", "/api/documentation-search/research/3f9c/continue"),
    ("/api/userdocs/stream", "/api/documentation-search/stream"),
])
def test_every_method_on_an_old_path_is_gone_with_its_replacement(client, method, old, new):
    response = client.request(method, old, follow_redirects=False)

    assert response.status_code == 410
    assert response.headers["cache-control"] == "no-store"
    if method == "HEAD":
        return
    body = response.json()
    assert body["error"] == "EndpointRenamed"
    assert body["replacement"] == new
    assert new in body["message"]
    assert "pip install -U cremind" in body["message"]
    assert "`cremind docs`" in body["message"]


def test_the_query_string_is_not_needed_to_answer(client):
    response = client.get("/api/userdocs/files?limit=5&source=drive")
    assert response.status_code == 410
    assert response.json()["replacement"] == "/api/documentation-search/files"


def test_replacement_path():
    assert replacement_path("/api/userdocs", "") == "/api/documentation-search"
    assert replacement_path("/api/userdocs", "admin") == "/api/documentation-search/admin"
    assert replacement_path("/api/userdocs", "/files/x/raw/") == "/api/documentation-search/files/x/raw"


def _api_routes():
    from app.api import get_api_routes

    return get_api_routes(
        registry=MagicMock(), pending_return_urls={},
        conversation_storage=MagicMock(), config_storage=MagicMock(),
    )


def _first_full_match(routes, method: str, path: str):
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    for route in routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    return None


def test_the_api_registers_the_retired_routes():
    retired = {route.path for route in get_retired_routes()}
    assert retired <= {route.path for route in _api_routes()}


def test_no_live_route_is_shadowed_by_a_retired_one():
    """Every live documentation-search path still resolves to its own
    handler, and no live route anywhere starts with the retired prefix."""
    routes = _api_routes()
    live = [r for r in routes if r.path.startswith("/api/documentation-search")]
    assert live, "the documentation-search routes are expected in the API"
    for route in live:
        method = sorted(route.methods - {"HEAD"})[0]
        concrete = route.path.replace("{leaf}", "search").replace("{fid}", "k7m2xq9a") \
            .replace("{job_id}", "3f9c")
        winner = _first_full_match(routes, method, concrete)
        assert winner is not None and winner.path == route.path
    assert not [
        r.path for r in routes
        if r.path.startswith("/api/userdocs") and r.endpoint.__module__ != "app.api.retired"
    ]
