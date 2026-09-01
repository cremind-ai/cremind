"""Authorization on ``DELETE /api/profiles/{profile}`` and ``POST /api/profiles``.

Deleting a profile is the one ``{profile_name}``-scoped route that opens up to
admin, and the reason is structural: a profile's token is the only credential
scoped to it, so "own profile only" made a profile removable *just* by the token
that dies with it — admin could see the row in the settings list and never act on
it. These tests pin the resulting shape:

- the ownership guard still rejects a *non-admin* reaching across profiles (the
  regression that the ``allow_admin`` flag must not widen);
- admin may delete any other profile;
- nobody, admin included, may delete ``admin`` — it is a literal profile name
  rather than a role, so removing it leaves an install no one can administer;
- the 401 → 400 → 403 ordering of the guard is unchanged, and an unknown-but-
  valid name still reaches storage and comes back 404;
- creating a profile is admin-only, since a new profile is a new tenant (its own
  skills, tool rows and embedding table) rather than a self-service action.
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


@pytest.fixture(autouse=True)
def _no_group_cleanup(monkeypatch):
    """Stub the group-chat teardown the delete handler calls.

    It reaches into the live group index and queue workers, none of which exist
    in a unit test. The handler swallows its failures, so without the stub these
    tests would still pass — but on a logged exception, hiding real breakage.
    """
    from app.groups import boot as groups_boot

    async def _noop(profile: str) -> None:
        return None

    monkeypatch.setattr(groups_boot, "on_profile_deleted", _noop)


def _storage(
    rows: list[dict] | None = None,
    *,
    deleted: list[str] | None = None,
    exists: bool = False,
    delete_result: bool = True,
) -> SimpleNamespace:
    async def list_profiles():
        return rows or []

    async def delete_profile(name: str) -> bool:
        if deleted is not None:
            deleted.append(name)
        return delete_result

    async def profile_exists(name: str) -> bool:
        return exists

    async def create_profile(name: str) -> dict:
        return {"name": name}

    return SimpleNamespace(
        list_profiles=list_profiles,
        delete_profile=delete_profile,
        profile_exists=profile_exists,
        create_profile=create_profile,
    )


def _handler(path: str, method: str, storage: SimpleNamespace | None = None) -> Callable:
    # registry=None so the skill watcher/teardown work is skipped entirely.
    for route in get_profile_routes(storage or _storage(), registry=None):  # type: ignore[arg-type]
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _auth(username: str = "admin", **kw) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=True, username=username), **kw
    )


def _body(resp) -> dict:
    return json.loads(resp.body)


def _json_body(payload: dict) -> Callable:
    """An awaitable ``request.json`` for the POST handler."""

    async def _json() -> dict:
        return payload

    return _json


_DELETE_PATH = "/api/profiles/{profile_name}"
_PROFILES_PATH = "/api/profiles"


# ── delete ───────────────────────────────────────────────────────────────────


def test_non_admin_cannot_delete_another_profile(system_dir):
    """Regression pin: opening delete to admin must not open it to everyone."""
    deleted: list[str] = []
    delete = _handler(_DELETE_PATH, "DELETE", _storage(deleted=deleted))

    req = _auth(username="lee", path_params={"profile_name": "sam"})
    resp = asyncio.run(delete(req))

    assert resp.status_code == 403
    assert "own profile" in _body(resp)["error"]
    assert "lee" in _body(resp)["error"]
    assert deleted == []


def test_non_admin_can_delete_own_profile(system_dir):
    deleted: list[str] = []
    delete = _handler(_DELETE_PATH, "DELETE", _storage(deleted=deleted))

    req = _auth(username="lee", path_params={"profile_name": "lee"})
    resp = asyncio.run(delete(req))

    assert resp.status_code == 200
    assert _body(resp)["success"] is True
    assert deleted == ["lee"]


def test_admin_can_delete_another_profile(system_dir):
    """The bug: admin owns the settings list but could not act on any row."""
    deleted: list[str] = []
    delete = _handler(_DELETE_PATH, "DELETE", _storage(deleted=deleted))

    req = _auth(username="admin", path_params={"profile_name": "lee"})
    resp = asyncio.run(delete(req))

    assert resp.status_code == 200
    assert deleted == ["lee"]


def test_admin_profile_itself_cannot_be_deleted(system_dir):
    """Admin deleting itself would leave nobody able to administer the install."""
    deleted: list[str] = []
    delete = _handler(_DELETE_PATH, "DELETE", _storage(deleted=deleted))

    req = _auth(username="admin", path_params={"profile_name": "admin"})
    resp = asyncio.run(delete(req))

    assert resp.status_code == 403
    assert _body(resp)["error"] == "The admin profile cannot be deleted."
    assert deleted == []


def test_delete_requires_auth(system_dir):
    delete = _handler(_DELETE_PATH, "DELETE")

    resp = asyncio.run(delete(SimpleNamespace(
        user=SimpleNamespace(is_authenticated=False),
        path_params={"profile_name": "lee"},
    )))

    assert resp.status_code == 401


def test_delete_invalid_name_is_400_not_403(system_dir):
    """Ordering pin: the name is validated before admin-ness is consulted."""
    delete = _handler(_DELETE_PATH, "DELETE")

    req = _auth(username="admin", path_params={"profile_name": "Not A Profile"})
    resp = asyncio.run(delete(req))

    assert resp.status_code == 400
    assert _body(resp)["error"] == "Invalid profile name"


def test_admin_deleting_unknown_profile_is_404(system_dir):
    """A valid name admin is allowed to touch, but no such row: storage decides."""
    delete = _handler(
        _DELETE_PATH, "DELETE", _storage(delete_result=False),
    )

    req = _auth(username="admin", path_params={"profile_name": "ghost"})
    resp = asyncio.run(delete(req))

    assert resp.status_code == 404
    assert "not found" in _body(resp)["error"]


# ── create ───────────────────────────────────────────────────────────────────


def test_create_profile_requires_admin(system_dir):
    created: list[str] = []
    storage = _storage()

    async def create_profile(name: str) -> dict:
        created.append(name)
        return {"name": name}

    storage.create_profile = create_profile
    post = _handler(_PROFILES_PATH, "POST", storage)

    req = _auth(username="lee")
    req.json = _json_body({"name": "sam"})
    resp = asyncio.run(post(req))

    assert resp.status_code == 403
    assert _body(resp)["error"] == "Only the admin profile can create profiles."
    assert created == []


def test_create_profile_unauthenticated_is_401(system_dir):
    """401 before 403 — an anonymous caller is not told the endpoint exists."""
    post = _handler(_PROFILES_PATH, "POST")

    req = SimpleNamespace(user=SimpleNamespace(is_authenticated=False))
    req.json = _json_body({"name": "sam"})
    resp = asyncio.run(post(req))

    assert resp.status_code == 401


def test_admin_can_create_profile(system_dir):
    created: list[str] = []
    storage = _storage()

    async def create_profile(name: str) -> dict:
        created.append(name)
        return {"name": name}

    storage.create_profile = create_profile
    post = _handler(_PROFILES_PATH, "POST", storage)

    req = _auth(username="admin")
    req.json = _json_body({"name": "sam"})
    resp = asyncio.run(post(req))

    assert resp.status_code == 201
    assert _body(resp)["profile"] == "sam"
    assert created == ["sam"]
