"""Agent-name profile endpoints.

- ``GET /api/profiles/agent-names`` feeds the chat ``@`` menu: every visible
  profile with its (defaulted) agent name; internal ``__`` profiles stay hidden;
  auth required.
- ``GET|PUT /api/profiles/{profile}/agent-name`` read/write a single profile's
  name, guarded to the caller's own profile (mirrors the persona endpoints).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Callable

import pytest

from app.api.profiles import get_profile_routes
from app.config.settings import BaseConfig


@pytest.fixture
def system_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    return tmp_path


def _storage(rows: list[dict]) -> SimpleNamespace:
    async def list_profiles():
        return rows

    return SimpleNamespace(list_profiles=list_profiles)


def _handler(storage: SimpleNamespace, path: str, method: str) -> Callable:
    for route in get_profile_routes(storage):  # type: ignore[arg-type]
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _auth(username: str = "admin", **kw) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, username=username), **kw
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


def test_agent_names_lists_visible_with_defaults(system_dir):
    storage = _storage([{"name": "admin"}, {"name": "lee"}, {"name": "__server__"}])
    handler = _handler(storage, "/api/profiles/agent-names", "GET")

    body = _body(asyncio.run(handler(_auth())))

    assert body == {
        "agents": [
            {"profile": "admin", "name": "Cremind"},
            {"profile": "lee", "name": "lee"},
        ]
    }


def test_agent_names_requires_auth(system_dir):
    handler = _handler(_storage([{"name": "admin"}]), "/api/profiles/agent-names", "GET")
    resp = asyncio.run(handler(SimpleNamespace(user=SimpleNamespace(is_authenticated=False))))
    assert resp.status_code == 401


def test_get_agent_name_default(system_dir):
    handler = _handler(_storage([]), "/api/profiles/{profile_name}/agent-name", "GET")
    req = _auth(username="admin", path_params={"profile_name": "admin"})
    assert _body(asyncio.run(handler(req))) == {"name": "Cremind"}


def test_get_agent_name_forbids_other_profile(system_dir):
    handler = _handler(_storage([]), "/api/profiles/{profile_name}/agent-name", "GET")
    req = _auth(username="lee", path_params={"profile_name": "admin"})
    assert asyncio.run(handler(req)).status_code == 403


def test_put_then_get_round_trip(system_dir):
    put = _handler(_storage([]), "/api/profiles/{profile_name}/agent-name", "PUT")
    get = _handler(_storage([]), "/api/profiles/{profile_name}/agent-name", "GET")

    async def json_body():
        return {"name": "Jarvis"}

    put_req = _auth(username="admin", path_params={"profile_name": "admin"})
    put_req.json = json_body
    assert _body(asyncio.run(put(put_req))) == {"success": True}

    get_req = _auth(username="admin", path_params={"profile_name": "admin"})
    assert _body(asyncio.run(get(get_req))) == {"name": "Jarvis"}


def test_put_rejects_empty_name(system_dir):
    put = _handler(_storage([]), "/api/profiles/{profile_name}/agent-name", "PUT")

    async def json_body():
        return {"name": "   "}

    req = _auth(username="admin", path_params={"profile_name": "admin"})
    req.json = json_body
    assert asyncio.run(put(req)).status_code == 400


def test_put_forbids_other_profile(system_dir):
    put = _handler(_storage([]), "/api/profiles/{profile_name}/agent-name", "PUT")

    async def json_body():
        return {"name": "Hacker"}

    req = _auth(username="lee", path_params={"profile_name": "admin"})
    req.json = json_body
    assert asyncio.run(put(req)).status_code == 403
