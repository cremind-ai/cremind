"""End-to-end create → restore round-trip for the backup engine.

Builds a throwaway SQLite system directory, populates the DB + on-disk trees,
creates a backup, then restores it into a *second* system directory and asserts:
rows survive, absolute paths relocate to the new system dir, included files land,
excluded files don't, and passphrase encryption round-trips.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from sqlalchemy import text

from app.databases import create_database_provider, get_database_provider, set_database_provider
from app.storage import migrations

# ≥32-byte HMAC keys so PyJWT doesn't warn; distinct so we can prove the
# target's secret is preserved and the source's is never applied.
_SRC_SECRET = "src-secret-" + "0" * 32
_DST_SECRET = "dst-secret-" + "0" * 32


@pytest.fixture
def restore_env(tmp_path, monkeypatch):
    """Yield a helper that points BaseConfig/env at a given system dir and
    resets the global DB provider so each side of the round-trip is isolated."""
    from app.config.settings import BaseConfig

    def use(system_dir: Path):
        monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(system_dir))
        monkeypatch.delenv("CREMIND_DB_PROVIDER", raising=False)
        monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(system_dir), raising=False)
        monkeypatch.setattr(
            BaseConfig, "SQLITE_DB_PATH", str(system_dir / "storage" / "cremind.db"), raising=False
        )
        set_database_provider(None)
        set_database_provider(create_database_provider())

    yield use
    set_database_provider(None)


def _populate(system_dir: Path):
    """Migrate to head + insert rows and files worth round-tripping."""
    migrations.upgrade("head")
    now = time.time()
    eng = get_database_provider().sync_engine()
    with eng.begin() as c:
        # A non-zero token_serial: the restored token must be minted at the
        # serial the archive carries, not at 0 — otherwise every re-minted
        # recovery token is rejected on sight as revoked.
        c.execute(
            text(
                "INSERT INTO profiles (id, name, created_at, updated_at, token_serial) "
                "VALUES ('p1','admin',:t,:t,5)"
            ),
            {"t": now},
        )
        c.execute(
            text(
                "INSERT INTO autostart_processes (id, profile, command, working_dir, is_pty, created_at) "
                "VALUES ('a1','admin',:cmd,:wd,0,:t)"
            ),
            {
                "cmd": f"uv run {system_dir}/admin/skills/x/run.py",
                "wd": str(system_dir / "admin" / "skills" / "x"),
                "t": now,
            },
        )
        # Source JWT secret — must NOT be carried into the backup (installation-local).
        c.execute(
            text(
                "INSERT INTO server_config (key, value, is_secret, updated_at) "
                "VALUES ('jwt_secret', :s, 1, :t)"
            ),
            {"s": _SRC_SECRET, "t": now * 1000},
        )
    # Files: include a token + OAuth token; exclude a derived .env + uploads_tmp.
    (system_dir / "admin" / "skills" / "gmail" / "scripts").mkdir(parents=True, exist_ok=True)
    (system_dir / "admin" / "skills" / "gmail" / "scripts" / ".google_token.json").write_text('{"rt":"s"}')
    (system_dir / "admin" / "skills" / "gmail" / "scripts" / ".env").write_text("X=1")
    (system_dir / "tokens").mkdir(parents=True, exist_ok=True)
    (system_dir / "tokens" / "admin.token").write_text("jwt")
    (system_dir / "admin" / "uploads_tmp" / "c1").mkdir(parents=True, exist_ok=True)
    (system_dir / "admin" / "uploads_tmp" / "c1" / "e.bin").write_text("ephemeral")


def _do_roundtrip(restore_env, tmp_path, passphrase):
    from app.backup import engine as be

    src = tmp_path / "src"
    dst = tmp_path / "dst"

    restore_env(src)
    _populate(src)

    result = be.create_backup(be.BackupOptions(passphrase=passphrase))
    assert result.path.is_file()
    assert result.manifest.profiles == ["admin"]
    assert result.manifest.encrypted == bool(passphrase)

    # Manifest is readable even for encrypted archives (envelope header).
    man = be.read_manifest(result.path)
    assert man.app_version == result.manifest.app_version

    # Restore into a fresh, different system dir — but first give the TARGET its
    # own JWT secret so we can prove the restore keeps it (never the backup's).
    restore_env(dst)
    migrations.upgrade("head")
    with get_database_provider().sync_engine().begin() as c:
        c.execute(
            text(
                "INSERT INTO server_config (key, value, is_secret, updated_at) "
                "VALUES ('jwt_secret', :s, 1, :t)"
            ),
            {"s": _DST_SECRET, "t": time.time() * 1000},
        )
    report = be.restore_backup(result.path, passphrase, target_system_dir=str(dst))
    assert report.ok
    assert report.db_row_counts.get("profiles") == 1
    assert report.db_row_counts.get("autostart_processes") == 1

    # Rows survive + path relocated to the NEW system dir.
    set_database_provider(None)
    set_database_provider(create_database_provider())
    eng = get_database_provider().sync_engine()
    with eng.connect() as c:
        assert c.execute(text("SELECT name FROM profiles")).scalar() == "admin"
        wd = c.execute(text("SELECT working_dir FROM autostart_processes")).scalar()
        cmd = c.execute(text("SELECT command FROM autostart_processes")).scalar()
        secret = c.execute(
            text("SELECT value FROM server_config WHERE key='jwt_secret'")
        ).scalar()
    assert wd.startswith(str(dst))
    assert str(dst) in cmd
    assert str(src) not in cmd

    # JWT secret: the TARGET's is preserved; the backup's is never applied.
    assert secret == _DST_SECRET
    assert secret != _SRC_SECRET

    # Files: OAuth token restored; .env + uploads_tmp excluded.
    assert (dst / "admin" / "skills" / "gmail" / "scripts" / ".google_token.json").is_file()
    assert not (dst / "admin" / "skills" / "gmail" / "scripts" / ".env").exists()
    assert not (dst / "admin" / "uploads_tmp" / "c1" / "e.bin").exists()

    # Token file re-minted under the TARGET secret (valid), not the source's.
    import jwt as _jwt

    tok = (dst / "tokens" / "admin.token").read_text(encoding="utf-8").strip()
    decoded = _jwt.decode(tok, _DST_SECRET, algorithms=["HS256"])
    assert decoded["sub"] == "admin"
    # ...and at the archived generation, so it validates against the restored row.
    assert decoded["tsr"] == 5
    with pytest.raises(_jwt.InvalidTokenError):
        _jwt.decode(tok, _SRC_SECRET, algorithms=["HS256"])


def test_roundtrip_plain(restore_env, tmp_path):
    _do_roundtrip(restore_env, tmp_path, passphrase=None)


def test_roundtrip_encrypted(restore_env, tmp_path):
    _do_roundtrip(restore_env, tmp_path, passphrase="s3cret-pass")


def test_restore_wrong_passphrase_rejected(restore_env, tmp_path):
    from app.backup import engine as be
    from app.backup.manifest import BackupPassphraseError

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    restore_env(src)
    _populate(src)
    result = be.create_backup(be.BackupOptions(passphrase="right"))

    restore_env(dst)
    with pytest.raises(BackupPassphraseError):
        be.restore_backup(result.path, "wrong", target_system_dir=str(dst))


def test_backup_omits_jwt_secret_and_tokens(restore_env, tmp_path):
    """The archive must carry no JWT signing secret and no session-token files,
    while per-profile OAuth token files (user data) are still included."""
    import gzip
    import json
    import tarfile

    from app.backup import engine as be
    from app.backup.manifest import DB_MEMBER, FILES_PREFIX

    src = tmp_path / "src"
    restore_env(src)
    _populate(src)  # writes tokens/admin.token + server_config.jwt_secret + an OAuth token

    result = be.create_backup(be.BackupOptions())
    assert result.path.is_file()

    with tarfile.open(str(result.path), "r:gz") as tf:
        names = tf.getnames()
        # No JWT token files archived under files/tokens/**.
        assert not any(
            n == f"{FILES_PREFIX}tokens" or n.startswith(f"{FILES_PREFIX}tokens/")
            for n in names
        )
        # The per-profile OAuth token file IS still archived (user data).
        assert any(n.endswith("scripts/.google_token.json") for n in names)
        # The DB dump omits the server_config.jwt_secret row.
        raw = gzip.decompress(tf.extractfile(tf.getmember(DB_MEMBER)).read()).decode("utf-8")

    server_config_keys = [
        rec["row"].get("key")
        for rec in (json.loads(line) for line in raw.splitlines() if line.strip())
        if rec.get("table") == "server_config"
    ]
    assert "jwt_secret" not in server_config_keys


def _seed_owed_results(system_dir: Path) -> None:
    """A conversation owed one result, plus one already reported."""
    now_ms = time.time() * 1000
    eng = get_database_provider().sync_engine()
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO profiles (id, name, created_at, updated_at) "
                "VALUES ('p1','admin',:t,:t)"
            ),
            {"t": time.time()},
        )
        c.execute(
            text(
                "INSERT INTO conversations (id, profile, title, created_at, updated_at) "
                "VALUES ('c1','admin','Mail',:t,:t)"
            ),
            {"t": time.time()},
        )
        for rid, delivered, mode in (
            ("r-owed", None, None),
            ("r-done", now_ms, "injected"),
        ):
            c.execute(
                text(
                    "INSERT INTO event_runs (id, profile, source_kind, subscription_id, "
                    "status, label, action, turn_count, created_at, updated_at, "
                    "finished_at, origin_conversation_id, deliver_to_origin, "
                    "origin_delivered_at, origin_delivery_mode) "
                    "VALUES (:id,'admin','skill_event','s1','completed','nightly','',0,"
                    ":t,:t,:t,'c1',1,:d,:m)"
                ),
                {"id": rid, "t": now_ms, "d": delivered, "m": mode},
            )


def test_a_restore_never_replays_owed_results(restore_env, tmp_path):
    """An archive's undelivered results must not be reported into live chats.

    ``event_runs`` travels inside every backup, delivery state and all, and the
    exactly-once lock IS one of its columns — so a restore rewinds it. Without
    the close-out the next boot's sweep would report an archive's owed results
    into whatever conversations survived (a room, a Telegram group), and would
    report a second time anything delivered after the backup was taken.
    """
    from app.backup import engine as be

    src, dst = tmp_path / "src", tmp_path / "dst"
    restore_env(src)
    migrations.upgrade("head")
    _seed_owed_results(src)

    archive = be.create_backup(be.BackupOptions()).path

    restore_env(dst)
    migrations.upgrade("head")
    report = be.restore_backup(archive, target_system_dir=str(dst))

    set_database_provider(None)
    set_database_provider(create_database_provider())
    with get_database_provider().sync_engine().connect() as c:
        rows = dict(c.execute(text(
            "SELECT id, origin_delivery_mode FROM event_runs"
        )).all())

    assert rows["r-owed"] == "skipped", "an owed result must not be reported"
    assert rows["r-done"] == "injected", "an already-reported one is untouched"
    assert any("closed out" in w for w in report.warnings), report.warnings


def test_a_rollback_keeps_the_results_this_install_still_owes(restore_env, tmp_path):
    """A rollback restores THIS install's own state from minutes ago.

    Its undelivered results are genuinely still owed to live conversations, so
    discarding them would be the rollback quietly breaking the flows it exists
    to leave untouched.
    """
    from app.backup import engine as be

    src, dst = tmp_path / "src", tmp_path / "dst"
    restore_env(src)
    migrations.upgrade("head")
    _seed_owed_results(src)

    archive = be.create_backup(be.BackupOptions()).path

    restore_env(dst)
    migrations.upgrade("head")
    be.restore_backup(
        archive, target_system_dir=str(dst), close_out_owed_results=False,
    )

    set_database_provider(None)
    set_database_provider(create_database_provider())
    with get_database_provider().sync_engine().connect() as c:
        owed = c.execute(text(
            "SELECT origin_delivered_at FROM event_runs WHERE id='r-owed'"
        )).scalar()
    assert owed is None
