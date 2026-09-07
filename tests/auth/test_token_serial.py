"""Token revocation: the serial comparison rule and its cache.

The two headline cases are the back-compat pair — a token minted before this
feature (no ``tsr`` claim) must keep working on upgrade, and the *same* token
must stop working the moment its profile is rotated. Everything else here
guards the ways that rule could silently fail open.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("a2a")

import jwt as pyjwt  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402

_SECRET = "test-secret-that-is-long-enough-for-hs256"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A migrated throwaway DB with two profiles, wired as the active provider."""
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

    monkeypatch.setattr(settings_mod.BaseConfig, "get_jwt_secret", classmethod(lambda cls: _SECRET))
    # The env var alone does not redirect anything: BaseConfig resolves it once
    # at import, so ``tokens_dir()`` kept pointing at the developer's real
    # ~/.cremind/tokens — these tests were rotating and deleting live token
    # files there, and the "no stray temp file" assertion below was really
    # asserting the shape of that directory. Patch the resolved attribute too,
    # the way tests/api/test_auth_rotate.py does.
    sysdir = tmp_path / "sysdir"
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.setattr(settings_mod.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.delenv("CREMIND_TOKEN_SERIAL_CACHE_TTL", raising=False)
    serial_mod.invalidate_serial_cache()
    yield provider
    serial_mod.invalidate_serial_cache()


def _legacy_token(profile: str = "admin") -> str:
    """A token in the shape this codebase minted before revocation existed."""
    now = datetime.now(timezone.utc)
    return pyjwt.encode(
        {"sub": profile, "profile": profile, "iat": now, "exp": now + timedelta(hours=1)},
        _SECRET,
        algorithm="HS256",
    )


# ── the comparison rule ────────────────────────────────────────────────────


def test_legacy_token_without_claim_is_accepted_at_serial_zero(db):
    """Upgrading must not log existing users out."""
    from app.auth import verify_token

    assert verify_token(_legacy_token()) is not None


def test_http_era_token_is_rejected_after_transport_epoch_advances(db):
    from app.auth import verify_token
    from app.config.settings import BaseConfig

    old_http_token = _legacy_token()
    tls_dir = Path(BaseConfig.CREMIND_SYSTEM_DIR) / "tls"
    tls_dir.mkdir(parents=True)
    (tls_dir / "transition.json").write_text(
        '{"version":1,"phase":"activating","transport_epoch":1}',
        encoding="utf-8",
    )

    assert verify_token(old_http_token) is None
    current, _ = BaseConfig.mint_token("admin")
    claims = pyjwt.decode(current, _SECRET, algorithms=["HS256"])
    assert claims["tep"] == 1
    assert verify_token(current) is not None


def test_the_boundary_advance_re_signs_every_live_token_against_a_real_database(
    db, monkeypatch,
):
    """The end of the switch, exercised against a real DB rather than a stub.

    Everywhere else the serial snapshot is mocked, which would hide the failure
    that matters most here: the reissue skips any profile whose ``tsr`` does not
    match, so reading serials wrongly leaves that profile's on-host credential
    dead at the old epoch with no error raised anywhere.
    """
    import app.runtime as runtime
    from app.auth.serial import bump_serial
    from app.auth.tokens import token_file_path, verify_token, write_token_file
    from app.config import tls_mode, tls_transition
    from app.config.settings import BaseConfig

    # bob has been rotated; admin never has. Both must survive the switch.
    bump_serial("bob")
    admin, _ = BaseConfig.mint_token("admin")
    bob, _ = BaseConfig.mint_token("bob")
    write_token_file("admin", admin)
    write_token_file("bob", bob)

    tls_transition.save_transition({
        "version": 1, "id": "C" * 32, "phase": "activating",
        "instance_id": tls_transition.instance_id(),
        "source_origin": "http://cremind.lan:1515",
        "source_origins": ["http://cremind.lan:1515"],
        "target_origin": "https://cremind.lan:1515",
        "created_at": 0, "expires_at": None, "pending_transport_epoch": 1,
    }, announce=False)
    monkeypatch.setattr(tls_mode, "_boot_serving_https", True)
    monkeypatch.setattr(runtime.get_state(), "storage_ready", True)

    tls_transition.mark_active()

    stored = tls_transition.load_transition()
    assert stored["phase"] == "active" and stored["transport_epoch"] == 1
    for profile, previous in (("admin", admin), ("bob", bob)):
        migrated = token_file_path(profile).read_text(encoding="utf-8")
        assert migrated != previous, f"{profile}'s on-host token was left behind"
        assert verify_token(migrated) is not None
        assert verify_token(previous) is None
        old_claims = pyjwt.decode(previous, _SECRET, algorithms=["HS256"])
        new_claims = pyjwt.decode(migrated, _SECRET, algorithms=["HS256"])
        assert new_claims["tep"] == 1
        # A transport change is not a login: expiry and revocation state stand.
        assert new_claims["exp"] == old_claims["exp"]
        assert new_claims["tsr"] == old_claims["tsr"]


def test_the_boundary_will_not_move_while_the_database_is_unreachable(db, monkeypatch):
    """An empty serial snapshot would silently skip every rotated profile."""
    import app.auth.serial as serial_mod
    import app.runtime as runtime
    from app.auth.tokens import token_file_path, write_token_file
    from app.config import tls_mode, tls_transition
    from app.config.settings import BaseConfig

    serial_mod.bump_serial("bob")
    bob, _ = BaseConfig.mint_token("bob")
    write_token_file("bob", bob)
    tls_transition.save_transition({
        "version": 1, "id": "D" * 32, "phase": "activating",
        "instance_id": tls_transition.instance_id(),
        "source_origin": "http://cremind.lan:1515",
        "source_origins": ["http://cremind.lan:1515"],
        "target_origin": "https://cremind.lan:1515",
        "created_at": 0, "expires_at": None, "pending_transport_epoch": 1,
    }, announce=False)
    monkeypatch.setattr(tls_mode, "_boot_serving_https", True)
    monkeypatch.setattr(runtime.get_state(), "storage_ready", True)

    def unreachable(*_args, **_kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(serial_mod, "get_database_provider", unreachable)
    serial_mod.invalidate_serial_cache()
    tls_transition.mark_active()

    stored = tls_transition.load_transition()
    assert stored["phase"] == "activating"
    assert stored["pending_transport_epoch"] == 1
    assert "could not be re-signed" in stored["activation_error"]
    assert token_file_path("bob").read_text(encoding="utf-8") == bob


@pytest.mark.parametrize("claim", ["1", 1.0, None, [], {}, True])
def test_non_integer_transport_epoch_claims_are_rejected(db, claim):
    from app.auth import verify_token

    now = datetime.now(timezone.utc)
    malformed = pyjwt.encode(
        {"sub": "admin", "profile": "admin", "tsr": 0, "tep": claim,
         "iat": now, "exp": now + timedelta(hours=1)},
        _SECRET, algorithm="HS256",
    )
    assert verify_token(malformed) is None


def test_malformed_transport_metadata_fails_closed(db):
    from app.auth import verify_token
    from app.config.settings import BaseConfig

    tls_dir = Path(BaseConfig.CREMIND_SYSTEM_DIR) / "tls"
    tls_dir.mkdir(parents=True)
    (tls_dir / "transition.json").write_text("{damaged", encoding="utf-8")
    assert verify_token(_legacy_token()) is None
    with pytest.raises(RuntimeError, match="transport epoch"):
        BaseConfig.mint_token("admin")


def test_legacy_token_is_revoked_after_one_rotation(db):
    """...but it must still be killable, which "missing claim = skip" wouldn't be."""
    from app.auth import bump_serial, verify_token

    token = _legacy_token()
    bump_serial("admin")
    assert verify_token(token) is None


def test_minted_token_survives_and_dies_with_its_generation(db):
    from app.auth import bump_serial, verify_token
    from app.config.settings import BaseConfig

    first, _ = BaseConfig.mint_token("admin")
    assert pyjwt.decode(first, _SECRET, algorithms=["HS256"])["tsr"] == 0
    assert verify_token(first) is not None

    bump_serial("admin")
    second, _ = BaseConfig.mint_token("admin")
    assert verify_token(first) is None
    assert verify_token(second) is not None


def test_rotation_does_not_touch_other_profiles(db):
    from app.auth import bump_serial, verify_token
    from app.config.settings import BaseConfig

    bobs, _ = BaseConfig.mint_token("bob")
    bump_serial("admin")
    assert verify_token(bobs) is not None


@pytest.mark.parametrize("claim", ["1", 1.0, None, [], {}, True])
def test_non_integer_serial_claims_are_rejected(db, claim):
    """A well-formed token always carries an int; anything else is a forgery."""
    from app.auth import bump_serial, serial_matches

    bump_serial("admin")  # serial is now 1, so a truthy `True` can't sneak through
    assert serial_matches({"profile": "admin", "tsr": claim}) is False


def test_unknown_profile_reads_as_serial_zero(db):
    from app.auth import current_serial, serial_matches

    assert current_serial("ghost") == 0
    assert serial_matches({"profile": "ghost", "tsr": 0}) is True
    assert serial_matches({"profile": "ghost", "tsr": 1}) is False


def test_expired_and_wrongly_signed_tokens_still_fail(db):
    from app.auth import verify_token

    now = datetime.now(timezone.utc)
    expired = pyjwt.encode(
        {"sub": "admin", "profile": "admin", "tsr": 0,
         "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1)},
        _SECRET, algorithm="HS256",
    )
    wrong_key = pyjwt.encode(
        {"sub": "admin", "profile": "admin", "tsr": 0,
         "iat": now, "exp": now + timedelta(hours=1)},
        "a-completely-different-secret-value", algorithm="HS256",
    )
    assert verify_token(expired) is None
    assert verify_token(wrong_key) is None


def test_no_secret_yields_anonymous_not_an_error(db):
    """Setup mode: before a secret exists every request must read as anonymous."""
    from app.auth import verify_token

    assert verify_token(_legacy_token(), secret="") is None


# ── cache behaviour ────────────────────────────────────────────────────────


def _count_queries(provider):
    """Count SELECTs against `profiles` on the provider's sync engine."""
    calls: list[str] = []

    @event.listens_for(provider.sync_engine(), "before_cursor_execute")
    def _hook(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
        if "token_serial" in statement and statement.strip().upper().startswith("SELECT"):
            calls.append(statement)

    return calls


def test_snapshot_is_reused_within_the_ttl(db):
    from app.auth import current_serial, invalidate_serial_cache

    invalidate_serial_cache()
    calls = _count_queries(db)
    for _ in range(5):
        current_serial("admin")
    assert len(calls) == 1, "each check hit the DB — the snapshot isn't caching"


def test_bump_invalidates_the_cache_in_process(db):
    """The rotating process must be immediately consistent, not TTL-delayed."""
    from app.auth import bump_serial, current_serial

    assert current_serial("admin") == 0
    assert bump_serial("admin") == 1
    assert current_serial("admin") == 1


def test_expired_ttl_forces_a_refetch(db, monkeypatch):
    import app.auth.serial as serial_mod

    serial_mod.invalidate_serial_cache()
    assert serial_mod.current_serial("admin") == 0

    # A bump through a *different* engine, as `cremind auth regenerate --local`
    # (a separate OS process) or a second Helm replica would do — in-process
    # write-invalidation can't see it, only the TTL can.
    other = SqliteDatabaseProvider(str(db.db_path))
    with other.sync_engine().begin() as c:
        c.execute(text("UPDATE profiles SET token_serial = 7 WHERE name='admin'"))

    assert serial_mod.current_serial("admin") == 0, "expected the documented staleness window"

    clock = [1000.0]
    monkeypatch.setattr(serial_mod.time, "monotonic", lambda: clock[0])
    serial_mod.invalidate_serial_cache()
    serial_mod.current_serial("admin")  # populate at t=1000
    clock[0] += serial_mod._ttl_seconds() + 1
    assert serial_mod.current_serial("admin") == 7


def test_snapshot_failure_serves_the_last_good_value(db, monkeypatch):
    """A DB blip must never read as "everything is serial 0" — that accepts
    every revoked token."""
    import app.auth.serial as serial_mod

    serial_mod.bump_serial("admin")
    assert serial_mod.current_serial("admin") == 1

    serial_mod.invalidate_serial_cache()
    serial_mod.current_serial("admin")  # repopulate

    monkeypatch.setattr(
        serial_mod, "get_database_provider",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    monkeypatch.setattr(serial_mod, "_ttl_seconds", lambda: 0.0)  # force a refresh attempt
    assert serial_mod.all_serials()["admin"] == 1


def test_ttl_can_be_disabled_by_env(db, monkeypatch):
    import app.auth.serial as serial_mod

    monkeypatch.setenv("CREMIND_TOKEN_SERIAL_CACHE_TTL", "0")
    serial_mod.invalidate_serial_cache()
    serial_mod.current_serial("admin")
    calls = _count_queries(db)
    serial_mod.current_serial("admin")
    serial_mod.current_serial("admin")
    assert len(calls) == 2


# ── rotation helper ────────────────────────────────────────────────────────


def test_rotate_bumps_mints_and_writes_the_file(db, tmp_path):
    import os

    from app.auth import rotate_profile_token, verify_token

    before, _ = __import__("app.config.settings", fromlist=["BaseConfig"]).BaseConfig.mint_token("admin")
    result = rotate_profile_token("admin")

    assert result["serial"] == 1
    assert verify_token(before) is None
    assert verify_token(result["token"]) is not None

    path = result["token_file"]
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as f:
        assert f.read() == result["token"]
    if os.name != "nt":
        assert os.stat(path).st_mode & 0o777 == 0o600
    # No stray temp file left behind, and nothing that would look like a profile.
    names = sorted(os.listdir(os.path.dirname(path)))
    assert names == ["admin.token"]


def test_rotate_rejects_names_that_escape_the_tokens_dir(db):
    from app.auth import rotate_profile_token

    for bad in ("../../etc/passwd", "has space", "UPPER", ""):
        with pytest.raises(ValueError):
            rotate_profile_token(bad)


def test_rotate_refuses_to_mint_without_a_secret(db, monkeypatch):
    """The --local footgun: signing with "" yields a token nothing accepts."""
    import app.config.settings as settings_mod

    from app.auth import rotate_profile_token

    monkeypatch.setattr(settings_mod.BaseConfig, "get_jwt_secret", classmethod(lambda cls: ""))
    with pytest.raises(RuntimeError, match="no JWT secret"):
        rotate_profile_token("admin")


def test_rotate_unknown_profile_raises_lookup_error(db):
    from app.auth import rotate_profile_token

    with pytest.raises(LookupError):
        rotate_profile_token("ghost")


def test_delete_token_file_removes_it(db):
    from app.auth import delete_token_file, rotate_profile_token, token_file_path

    rotate_profile_token("admin")
    assert token_file_path("admin").exists()
    assert delete_token_file("admin") is True
    assert not token_file_path("admin").exists()
    assert delete_token_file("admin") is False
