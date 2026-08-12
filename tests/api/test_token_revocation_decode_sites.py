"""A revoked token must be dead at *every* entry point, not just the API.

Session JWTs are decoded in five places, and they don't share a code path by
accident of history — the HTTP middleware, the A2A call-context builder, the
WebSocket handshake (which bypasses the middleware entirely), and two handlers
that re-read their own bearer. Any one of them still calling ``jwt.decode``
directly is a hole a revoked token walks through, so this pins all five.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from sqlalchemy import text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_SECRET = "test-secret-that-is-long-enough-for-hs256"


@pytest.fixture
def env(tmp_path, monkeypatch):
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
    monkeypatch.setattr(settings_mod.BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path / "sysdir"))
    monkeypatch.setattr(settings_mod.BaseConfig, "get_jwt_secret", classmethod(lambda cls: _SECRET))
    serial_mod.invalidate_serial_cache()
    yield provider
    serial_mod.invalidate_serial_cache()


def _conn(token: str, path: str = "/api/conversations"):
    return SimpleNamespace(
        headers={"Authorization": f"Bearer {token}"},
        url=SimpleNamespace(path=path),
        user=SimpleNamespace(is_authenticated=True, username="admin"),
        query_params={},
    )


# Each entry: (label, callable(token) -> "accepted" | "rejected")
def _site_middleware(token):
    from app.server import JWTAuthBackend
    from app.config.settings import BaseConfig

    backend = JWTAuthBackend(secret_provider=BaseConfig.get_jwt_secret)
    result = asyncio.run(backend.authenticate(_conn(token)))
    return "rejected" if result is None else "accepted"


def _site_a2a_context(token):
    from app.server import JWTCallContextBuilder
    from app.config.settings import BaseConfig

    builder = JWTCallContextBuilder(secret_provider=BaseConfig.get_jwt_secret)
    ctx = builder.build(_conn(token))
    return "accepted" if ctx.state.get("profile") else "rejected"


def _site_websocket(token):
    from app.api.processes import _decode_ws_token

    return "rejected" if _decode_ws_token(token) is None else "accepted"


def _site_me(token):
    from app.api.tokens import get_me

    resp = asyncio.run(get_me(_conn(token, "/api/me")))
    return "rejected" if resp.status_code == 401 else "accepted"


def _site_system_vars(token):
    from app.api.system_vars import list_system_vars

    resp = asyncio.run(list_system_vars(_conn(token, "/api/system-vars")))
    return "rejected" if resp.status_code == 401 else "accepted"


_SITES = [
    ("http middleware", _site_middleware),
    ("a2a call context", _site_a2a_context),
    ("websocket handshake", _site_websocket),
    ("GET /api/me", _site_me),
    ("GET /api/system-vars", _site_system_vars),
]


@pytest.mark.parametrize("label,check", _SITES, ids=[s[0] for s in _SITES])
def test_a_current_token_is_accepted_everywhere(env, label, check):
    from app.config.settings import BaseConfig

    token, _ = BaseConfig.mint_token("admin")
    assert check(token) == "accepted"


@pytest.mark.parametrize("label,check", _SITES, ids=[s[0] for s in _SITES])
def test_a_revoked_token_is_rejected_everywhere(env, label, check):
    from app.auth import bump_serial
    from app.config.settings import BaseConfig

    token, _ = BaseConfig.mint_token("admin")
    bump_serial("admin")
    assert check(token) == "rejected", f"{label} still accepts a revoked token"


@pytest.mark.parametrize("label,check", _SITES, ids=[s[0] for s in _SITES])
def test_another_profile_is_unaffected(env, label, check):
    from app.auth import bump_serial
    from app.config.settings import BaseConfig

    bobs, _ = BaseConfig.mint_token("bob")
    bump_serial("admin")
    assert check(bobs) == "accepted"


def test_terminals_shares_the_websocket_decoder(env):
    """`app/api/terminals.py` imports this private — it must not fork."""
    import app.api.processes as processes
    import app.api.terminals as terminals

    assert terminals._decode_ws_token is processes._decode_ws_token


def test_system_vars_does_not_hand_a_revoked_holder_its_replacement(env):
    """The self-healing-revocation trap: CREMIND_TOKEN's resolver reads the
    token file, so this route would otherwise leak the fresh token."""
    from app.api.system_vars import list_system_vars
    from app.auth import rotate_profile_token
    from app.config.settings import BaseConfig

    stale, _ = BaseConfig.mint_token("admin")
    rotate_profile_token("admin")

    resp = asyncio.run(list_system_vars(_conn(stale, "/api/system-vars")))
    assert resp.status_code == 401
    assert "token" not in json.loads(resp.body.decode())
