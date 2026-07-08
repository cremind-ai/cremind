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
        c.execute(
            text("INSERT INTO profiles (id, name, created_at, updated_at) VALUES ('p1','admin',:t,:t)"),
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

    # Restore into a fresh, different system dir.
    restore_env(dst)
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
    assert wd.startswith(str(dst))
    assert str(dst) in cmd
    assert str(src) not in cmd

    # Files: token + OAuth token restored; .env + uploads_tmp excluded.
    assert (dst / "tokens" / "admin.token").is_file()
    assert (dst / "admin" / "skills" / "gmail" / "scripts" / ".google_token.json").is_file()
    assert not (dst / "admin" / "skills" / "gmail" / "scripts" / ".env").exists()
    assert not (dst / "admin" / "uploads_tmp" / "c1" / "e.bin").exists()


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
