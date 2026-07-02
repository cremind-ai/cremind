"""Persona profile endpoints and the shared profile-ownership guard.

``GET|PUT /api/profiles/{profile}/persona`` read/write a single profile's
persona, authorized by `_require_own_profile`:
  * 401 unauthenticated,
  * 400 for a missing or malformed profile name (e.g. the persona text
    mis-slotted into the name position — the reported bug),
  * 403 for a valid name that is not the caller's own profile,
  * 200/success for the caller's own profile.
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


def _storage(rows: list[dict] | None = None) -> SimpleNamespace:
    async def list_profiles():
        return rows or []

    return SimpleNamespace(list_profiles=list_profiles)


def _handler(path: str, method: str, storage: SimpleNamespace | None = None) -> Callable:
    for route in get_profile_routes(storage or _storage()):  # type: ignore[arg-type]
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _auth(username: str = "admin", **kw) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, username=username), **kw
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


_PERSONA_PATH = "/api/profiles/{profile_name}/persona"


def test_update_persona_own_profile_round_trips(system_dir):
    put = _handler(_PERSONA_PATH, "PUT")
    get = _handler(_PERSONA_PATH, "GET")

    async def json_body():
        return {"content": "You are concise."}

    put_req = _auth(username="admin", path_params={"profile_name": "admin"})
    put_req.json = json_body
    assert _body(asyncio.run(put(put_req))) == {"success": True}

    get_req = _auth(username="admin", path_params={"profile_name": "admin"})
    assert _body(asyncio.run(get(get_req))) == {"content": "You are concise."}


def test_update_persona_requires_auth(system_dir):
    put = _handler(_PERSONA_PATH, "PUT")
    resp = asyncio.run(put(SimpleNamespace(
        user=SimpleNamespace(is_authenticated=False),
        path_params={"profile_name": "admin"},
    )))
    assert resp.status_code == 401


def test_update_persona_forbids_other_profile(system_dir):
    put = _handler(_PERSONA_PATH, "PUT")

    async def json_body():
        return {"content": "malicious"}

    # Valid name, but not the caller's own profile -> 403 with a clear message.
    req = _auth(username="lee", path_params={"profile_name": "admin"})
    req.json = json_body
    resp = asyncio.run(put(req))
    assert resp.status_code == 403
    assert "own profile" in _body(resp)["error"]
    assert "lee" in _body(resp)["error"]


def test_update_persona_invalid_name_is_400_not_403(system_dir):
    # The reported bug: the persona text lands in the name slot. A name with
    # spaces/newlines/`*` is not a valid profile name -> 400, distinct from the
    # 403 "not your profile".
    put = _handler(_PERSONA_PATH, "PUT")

    async def json_body():
        return {"content": ""}

    blob = "You are **Cremind**, the personal AI assistant\nrunning on the user's own"
    req = _auth(username="admin", path_params={"profile_name": blob})
    req.json = json_body
    resp = asyncio.run(put(req))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "Invalid profile name"


def test_get_persona_invalid_name_is_400(system_dir):
    get = _handler(_PERSONA_PATH, "GET")
    req = _auth(username="admin", path_params={"profile_name": "Not A Profile"})
    resp = asyncio.run(get(req))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "Invalid profile name"


def test_persona_name_too_long_is_400(system_dir):
    get = _handler(_PERSONA_PATH, "GET")
    req = _auth(username="admin", path_params={"profile_name": "a" * 65})
    resp = asyncio.run(get(req))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "Invalid profile name"
