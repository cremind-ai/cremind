"""Standing-instructions profile endpoints.

``GET|PUT /api/profiles/{profile}/instructions`` read/write a profile's
INSTRUCTIONS.md, sharing `_require_own_profile` with the persona routes (401 /
400 / 403 / 200). Unlike the persona there is no seed template: a profile that
never set instructions reads back "" and no file is created.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Callable

import pytest

from app.api.profiles import get_profile_routes
from app.config.settings import BaseConfig
from app.utils.instructions import INSTRUCTIONS_FILENAME


@pytest.fixture
def system_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    return tmp_path


def _storage(rows: list[dict] | None = None) -> SimpleNamespace:
    async def list_profiles():
        return rows or []

    return SimpleNamespace(list_profiles=list_profiles)


def _handler(path: str, method: str) -> Callable:
    for route in get_profile_routes(_storage()):  # type: ignore[arg-type]
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _auth(username: str = "admin", **kw) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, username=username), **kw
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


_PATH = "/api/profiles/{profile_name}/instructions"


def test_update_instructions_own_profile_round_trips(system_dir):
    put = _handler(_PATH, "PUT")
    get = _handler(_PATH, "GET")

    async def json_body():
        return {"content": "Register new channel users in the Active-User sheet."}

    put_req = _auth(username="admin", path_params={"profile_name": "admin"})
    put_req.json = json_body
    assert _body(asyncio.run(put(put_req))) == {"success": True}

    get_req = _auth(username="admin", path_params={"profile_name": "admin"})
    assert _body(asyncio.run(get(get_req))) == {
        "content": "Register new channel users in the Active-User sheet."
    }


def test_get_instructions_defaults_to_empty_and_creates_nothing(system_dir):
    get = _handler(_PATH, "GET")
    req = _auth(username="admin", path_params={"profile_name": "admin"})
    assert _body(asyncio.run(get(req))) == {"content": ""}
    # "Never set" must not materialize a file — an absent file IS the default.
    assert not (system_dir / "admin" / INSTRUCTIONS_FILENAME).exists()


def test_update_instructions_accepts_explicit_clear(system_dir):
    put = _handler(_PATH, "PUT")
    get = _handler(_PATH, "GET")

    async def write():
        return {"content": "temporary"}

    async def clear():
        return {"content": ""}

    req = _auth(username="admin", path_params={"profile_name": "admin"})
    req.json = write
    asyncio.run(put(req))

    req2 = _auth(username="admin", path_params={"profile_name": "admin"})
    req2.json = clear
    asyncio.run(put(req2))

    get_req = _auth(username="admin", path_params={"profile_name": "admin"})
    assert _body(asyncio.run(get(get_req))) == {"content": ""}


def test_update_instructions_requires_content_field(system_dir):
    put = _handler(_PATH, "PUT")

    async def json_body():
        return {"nope": 1}

    req = _auth(username="admin", path_params={"profile_name": "admin"})
    req.json = json_body
    resp = asyncio.run(put(req))
    assert resp.status_code == 400
    assert "content" in _body(resp)["error"]


def test_update_instructions_requires_auth(system_dir):
    put = _handler(_PATH, "PUT")
    resp = asyncio.run(put(SimpleNamespace(
        user=SimpleNamespace(is_authenticated=False),
        path_params={"profile_name": "admin"},
    )))
    assert resp.status_code == 401


def test_update_instructions_forbids_other_profile(system_dir):
    put = _handler(_PATH, "PUT")

    async def json_body():
        return {"content": "malicious"}

    req = _auth(username="lee", path_params={"profile_name": "admin"})
    req.json = json_body
    resp = asyncio.run(put(req))
    assert resp.status_code == 403
    assert "own profile" in _body(resp)["error"]


def test_instructions_invalid_name_is_400_not_403(system_dir):
    # Mirrors the persona guard: instructions text mis-slotted into the name.
    get = _handler(_PATH, "GET")
    req = _auth(username="admin", path_params={"profile_name": "Not A Profile"})
    resp = asyncio.run(get(req))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "Invalid profile name"


def test_instructions_are_isolated_per_profile(system_dir):
    put = _handler(_PATH, "PUT")
    get = _handler(_PATH, "GET")

    async def json_body():
        return {"content": "admin-only directive"}

    req = _auth(username="admin", path_params={"profile_name": "admin"})
    req.json = json_body
    asyncio.run(put(req))

    # No admin inheritance — another profile starts empty (same as persona).
    other = _auth(username="lee", path_params={"profile_name": "lee"})
    assert _body(asyncio.run(get(other))) == {"content": ""}
