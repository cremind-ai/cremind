"""`/api/auth/*` — token status and rotation.

Drives the handlers directly (no ASGI app). What matters here is the
authorization shape: a caller rotates its own profile, only ``admin`` may
target someone else's, and the response carries the credential the caller now
needs — plus the file on disk that is its only other copy.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from sqlalchemy import text  # noqa: E402

import app.api.auth as A  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_SECRET = "test-secret-that-is-long-enough-for-hs256"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Migrated DB + two profiles + a pinned system dir and JWT secret."""
    import app.auth.serial as serial_mod
    import app.config.settings as settings_mod
    import app.databases as dbs
    import app.storage.migrations as mig

    provider = SqliteDatabaseProvider(str(tmp_path / "auth.db"))
    monkeypatch.setattr(dbs, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(mig, "get_database_provider", lambda *a, **k: provider)
    monkeypatch.setattr(serial_mod, "get_database_provider", lambda *a, **k: provider)
    mig.upgrade("head")
    with provider.sync_engine().begin() as c:
        for pid, name in (("1", "admin"), ("2", "bob")):
            c.execute(
                text(
                    "INSERT INTO profiles (id,name,created_at,updated_at,token_serial) "
                    "VALUES (:i,:n,0,0,0)"
                ),
                {"i": pid, "n": name},
            )

    sysdir = tmp_path / "sysdir"
    monkeypatch.setattr(settings_mod.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.setattr(settings_mod.BaseConfig, "get_jwt_secret", classmethod(lambda cls: _SECRET))
    serial_mod.invalidate_serial_cache()
    yield SimpleNamespace(provider=provider, sysdir=sysdir)
    serial_mod.invalidate_serial_cache()


class _Storage:
    """Just the one method the handler needs."""

    def __init__(self, names):
        self._names = set(names)

    async def profile_exists(self, name: str) -> bool:
        return name in self._names


def _req(profile: str = "admin", *, authenticated: bool = True, body: dict | None = None,
         token: str | None = None, query: dict | None = None) -> SimpleNamespace:
    payload = json.dumps(body).encode() if body is not None else b""

    async def _body() -> bytes:
        return payload

    from app.config.settings import BaseConfig

    bearer = token if token is not None else BaseConfig.mint_token(profile)[0]
    return SimpleNamespace(
        user=SimpleNamespace(is_authenticated=authenticated, username=profile),
        headers={"Authorization": f"Bearer {bearer}"},
        query_params=query or {},
        body=_body,
    )


def _out(resp) -> dict:
    return json.loads(resp.body.decode())


def _regenerate(request, names=("admin", "bob")):
    return asyncio.run(A.handle_auth_regenerate(request, _Storage(names)))


# ── regenerate ─────────────────────────────────────────────────────────────


def test_rotating_own_profile_returns_a_working_token(env):
    from app.auth import verify_token
    from app.config.settings import BaseConfig

    old, _ = BaseConfig.mint_token("admin")
    resp = _regenerate(_req("admin"))
    assert resp.status_code == 200
    out = _out(resp)

    assert out["profile"] == "admin"
    assert out["serial"] == 1
    assert verify_token(out["token"]) is not None
    assert verify_token(old) is None


def test_serial_increments_across_rotations(env):
    assert _out(_regenerate(_req("admin")))["serial"] == 1
    assert _out(_regenerate(_req("admin")))["serial"] == 2


def test_the_token_file_is_written_and_matches(env):
    out = _out(_regenerate(_req("admin")))
    path = out["token_file"]
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as f:
        assert f.read() == out["token"]
    if os.name != "nt":
        assert os.stat(path).st_mode & 0o777 == 0o600


def test_unauthenticated_is_401(env):
    resp = _regenerate(_req("admin", authenticated=False))
    assert resp.status_code == 401


def test_non_admin_cannot_rotate_another_profile(env):
    resp = _regenerate(_req("bob", body={"profile": "admin"}))
    assert resp.status_code == 403
    out = _out(resp)
    assert out["error"] == "admin_required"
    # The CLI surfaces `message`, which _check_response drops from `error`.
    assert "admin" in out["message"]


def test_admin_can_rotate_another_profile(env):
    from app.auth import current_serial

    resp = _regenerate(_req("admin", body={"profile": "bob"}))
    assert resp.status_code == 200
    assert _out(resp)["profile"] == "bob"
    assert current_serial("bob") == 1
    assert current_serial("admin") == 0


def test_targeting_your_own_profile_by_name_needs_no_admin(env):
    resp = _regenerate(_req("bob", body={"profile": "bob"}))
    assert resp.status_code == 200


def test_invalid_profile_name_is_400(env):
    resp = _regenerate(_req("admin", body={"profile": "../../etc/passwd"}))
    assert resp.status_code == 400
    assert _out(resp)["error"] == "invalid_profile"


def test_unknown_profile_is_404(env):
    resp = _regenerate(_req("admin", body={"profile": "ghost"}))
    assert resp.status_code == 404
    assert _out(resp)["error"] == "profile_not_found"


@pytest.mark.parametrize("value", [0, -1, 99999, "x", 8761])
def test_expires_hours_is_bounded(env, value):
    resp = _regenerate(_req("admin", body={"expires_hours": value}))
    assert resp.status_code == 400
    assert _out(resp)["error"] == "invalid_expires_hours"


def test_expires_hours_is_honoured(env):
    import jwt as pyjwt

    out = _out(_regenerate(_req("admin", body={"expires_hours": 1})))
    claims = pyjwt.decode(out["token"], _SECRET, algorithms=["HS256"])
    assert claims["exp"] - claims["iat"] == 3600


def test_malformed_json_body_is_400(env):
    async def _body() -> bytes:
        return b"{not json"

    request = _req("admin")
    request.body = _body
    resp = _regenerate(request)
    assert resp.status_code == 400
    assert _out(resp)["error"] == "invalid_json"


# ── status ─────────────────────────────────────────────────────────────────


def test_status_reports_a_valid_token(env):
    resp = asyncio.run(A.handle_auth_status(_req("admin")))
    out = _out(resp)
    assert out["profile"] == "admin"
    assert out["valid"] is True
    assert out["token_serial"] == 0
    assert out["current_serial"] == 0


def test_status_reports_a_revoked_token(env):
    """The whole reason `auth status` exists — `cremind me` can't see this."""
    from app.config.settings import BaseConfig
    from app.auth import bump_serial

    stale, _ = BaseConfig.mint_token("admin")
    bump_serial("admin")

    # Middleware would normally have rejected this already; the handler is being
    # asked directly with a token that is signed and unexpired but out of date.
    resp = asyncio.run(A.handle_auth_status(_req("admin", token=stale)))
    assert resp.status_code == 401


def test_status_unauthenticated_is_401(env):
    resp = asyncio.run(A.handle_auth_status(_req("admin", authenticated=False)))
    assert resp.status_code == 401


def test_status_for_another_profile_requires_admin(env):
    resp = asyncio.run(A.handle_auth_status(_req("bob", query={"profile": "admin"})))
    assert resp.status_code == 403


def test_admin_status_for_another_profile_omits_their_token_serial(env):
    """An admin holds its own token, not bob's — reporting a serial for the
    presented token would be meaningless."""
    from app.auth import bump_serial

    bump_serial("bob")
    out = _out(asyncio.run(A.handle_auth_status(_req("admin", query={"profile": "bob"}))))
    assert out["profile"] == "bob"
    assert out["current_serial"] == 1
    assert out["token_serial"] is None
    assert out["valid"] is True


def test_routes_are_registered(env):
    paths = {(r.path, tuple(sorted(r.methods or ()))) for r in A.get_auth_routes(_Storage(["admin"]))}
    assert ("/api/auth/status", ("GET", "HEAD")) in paths
    assert ("/api/auth/regenerate", ("POST",)) in paths
