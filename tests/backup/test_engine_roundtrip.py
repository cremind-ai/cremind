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
    resets the global DB provider so each side of the round-trip is isolated.

    ``workspaces`` sets CREMIND_WORKSPACES_DIR for that side (a container's
    root outside the system dir); by default it is unset, so the profiles'
    working directories live in ``<system dir>/workspaces``."""
    from app.config.settings import BaseConfig

    def use(system_dir: Path, *, workspaces: Path | None = None):
        monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(system_dir))
        monkeypatch.delenv("CREMIND_DB_PROVIDER", raising=False)
        if workspaces is None:
            monkeypatch.delenv("CREMIND_WORKSPACES_DIR", raising=False)
        else:
            monkeypatch.setenv("CREMIND_WORKSPACES_DIR", str(workspaces))
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


# ── Cremind manual pages and document indexes ───────────────────────────────


def _two_profiles() -> None:
    now = time.time()
    with get_database_provider().sync_engine().begin() as c:
        for pid, name in (("p1", "admin"), ("p2", "bob")):
            c.execute(
                text("INSERT INTO profiles (id, name, created_at, updated_at) VALUES (:i, :n, :t, :t)"),
                {"i": pid, "n": name, "t": now},
            )


def _put(path: Path, text_: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text_, encoding="utf-8")


def test_manual_pages_travel_by_uuid_and_indexes_stay_behind(restore_env, tmp_path):
    """A profile's own manual pages live outside its name-keyed tree, at
    ``storage/cremind_documents/profiles/<uuid>``: the archive must carry them
    (for both profiles, each to its own uuid) — and never the derived index
    store, the pre-rename index root, or the re-seeded shared mirror."""
    import tarfile

    from app.backup import engine as be
    from app.backup.manifest import FILES_PREFIX

    src, dst = tmp_path / "src", tmp_path / "dst"
    restore_env(src)
    migrations.upgrade("head")
    _two_profiles()
    manual = src / "storage" / "cremind_documents"
    _put(manual / "profiles" / "p1" / "mine.md", "admin's page")
    _put(manual / "profiles" / "p2" / "bob.md", "bob's page")
    _put(manual / "shared" / "document.md", "bundled")
    _put(src / "storage" / "documents" / "p1" / "index.db", "index")
    _put(src / "storage" / "userdocs" / "p2" / "index.db", "old index")

    archive = be.create_backup(be.BackupOptions()).path
    with tarfile.open(str(archive), "r:gz") as tf:
        names = set(tf.getnames())
    assert f"{FILES_PREFIX}storage/cremind_documents/profiles/p1/mine.md" in names
    assert f"{FILES_PREFIX}storage/cremind_documents/profiles/p2/bob.md" in names
    assert not any(
        n.startswith(f"{FILES_PREFIX}{p}")
        for n in names
        for p in ("storage/documents", "storage/userdocs", "storage/cremind_documents/shared")
    ), names

    restore_env(dst)
    migrations.upgrade("head")
    report = be.restore_backup(archive, target_system_dir=str(dst))
    assert report.ok

    restored = dst / "storage" / "cremind_documents" / "profiles"
    assert (restored / "p1" / "mine.md").read_text(encoding="utf-8") == "admin's page"
    assert (restored / "p2" / "bob.md").read_text(encoding="utf-8") == "bob's page"
    assert not (restored / "p1" / "bob.md").exists()
    assert not (dst / "storage" / "documents").exists()


def test_an_archive_from_before_the_move_restores_pages_to_the_new_place(restore_env, tmp_path):
    """An archive made before the manual moved carries a profile's pages at
    ``<profile>/documents``. The restore copies them there, then relocates
    them to the uuid directory of the restored profile row."""
    from app.backup import engine as be

    src, dst = tmp_path / "src", tmp_path / "dst"
    restore_env(src)
    migrations.upgrade("head")
    _two_profiles()
    # The pre-move layout, as an older install's archive holds it.
    _put(src / "admin" / "documents" / "note.md", "admin's old-layout page")
    _put(src / "bob" / "documents" / "guides" / "g.md", "bob's old-layout page")
    _put(src / "admin" / "PERSONA.md", "persona")

    archive = be.create_backup(be.BackupOptions()).path

    restore_env(dst)
    migrations.upgrade("head")
    report = be.restore_backup(archive, target_system_dir=str(dst))
    assert report.ok, report.error

    pages = dst / "storage" / "cremind_documents" / "profiles"
    assert (pages / "p1" / "note.md").read_text(encoding="utf-8") == "admin's old-layout page"
    assert (pages / "p2" / "guides" / "g.md").read_text(encoding="utf-8") == "bob's old-layout page"
    assert not (dst / "admin" / "documents").exists()
    assert not (dst / "bob" / "documents").exists()
    assert (dst / "admin" / "PERSONA.md").exists(), "the rest of the tree is restored as before"


# ── the profiles' working directories ───────────────────────────────────────


def _seed_workspaces(root: Path) -> None:
    """Two profiles' default folders plus a deleted profile's kept one —
    with the files a system-tree prune would wrongly drop."""
    _put(root / "admin" / "notes.md", "admin's notes")
    _put(root / "bob" / "app" / "poetry.lock", "lock")
    _put(root / "bob" / "draft.tmp", "draft")
    # Looks like a pre-move manual page, but it is bob's own file: the
    # post-restore relocation must leave it where it is.
    _put(root / "bob" / "documents" / "report.md", "bob's report")
    _put(root / "bob" / "app" / "node_modules" / "left-pad" / "index.js", "dep")
    _put(root / "bob" / "app" / ".venv" / "pyvenv.cfg", "venv")
    _put(root / ".deleted" / "carol-20260101-000000" / "kept.md", "carol's")


def _set_working_dir(name: str, value: str) -> None:
    with get_database_provider().sync_engine().begin() as c:
        c.execute(text("UPDATE profiles SET working_dir = :v WHERE name = :n"), {"v": value, "n": name})


def _archive_names(archive: Path) -> set[str]:
    import tarfile

    with tarfile.open(str(archive), "r:gz") as tf:
        return set(tf.getnames())


def test_the_working_directories_travel_by_default(restore_env, tmp_path):
    """Every profile's default folder (and a deleted profile's kept one) is
    archived under ``workspaces/`` and restored into the target's workspaces
    root — lock and tmp files included, dependency folders not — without the
    manual relocation mistaking a workspace's ``documents`` for a manual."""
    from app.backup import engine as be

    src, dst = tmp_path / "src", tmp_path / "dst"
    restore_env(src)
    migrations.upgrade("head")
    _two_profiles()
    _put(src / "bob" / "PERSONA.md", "persona")
    _seed_workspaces(src / "workspaces")
    # A folder an admin chose INSIDE the root (a new profile that found its
    # default taken gets <name>-2): stored explicitly, relocated on restore.
    _set_working_dir("bob", str(src / "workspaces" / "bob-2"))

    result = be.create_backup(be.BackupOptions())
    assert result.manifest.workspaces_included is True
    assert result.manifest.source_paths.workspaces_root == str(src / "workspaces")
    assert result.workspaces_file_count == 5
    names = _archive_names(result.path)
    for rel in ("admin/notes.md", "bob/app/poetry.lock", "bob/draft.tmp",
                "bob/documents/report.md", ".deleted/carol-20260101-000000/kept.md"):
        assert f"workspaces/{rel}" in names, rel
    assert not [n for n in names if "node_modules" in n or ".venv" in n]
    # Never a second copy through the system-dir walk.
    assert not [n for n in names if n.startswith("files/workspaces")]

    restore_env(dst)
    migrations.upgrade("head")
    report = be.restore_backup(result.path, target_system_dir=str(dst))
    assert report.ok, report.error

    ws = dst / "workspaces"
    assert (ws / "admin" / "notes.md").read_text(encoding="utf-8") == "admin's notes"
    assert (ws / "bob" / "app" / "poetry.lock").is_file()
    assert (ws / "bob" / "draft.tmp").is_file()
    assert (ws / ".deleted" / "carol-20260101-000000" / "kept.md").is_file()
    assert (ws / "bob" / "documents" / "report.md").read_text(encoding="utf-8") == "bob's report"
    assert not (dst / "storage" / "cremind_documents" / "profiles" / "p2" / "report.md").exists()
    assert (dst / "bob" / "PERSONA.md").is_file()

    set_database_provider(None)
    set_database_provider(create_database_provider())
    with get_database_provider().sync_engine().connect() as c:
        rows = dict(c.execute(text("SELECT name, working_dir FROM profiles")).all())
    assert rows["bob"] == str(dst / "workspaces" / "bob-2")
    assert rows["admin"] is None


def test_no_workspaces_leaves_them_out_and_the_restore_leaves_them_alone(restore_env, tmp_path):
    """``--no-workspaces``: nothing under ``workspaces/``, the manifest says
    so, and restoring it deletes nothing already in the target's folders."""
    from app.backup import engine as be

    src, dst = tmp_path / "src", tmp_path / "dst"
    restore_env(src)
    migrations.upgrade("head")
    _two_profiles()
    _seed_workspaces(src / "workspaces")

    result = be.create_backup(be.BackupOptions(include_workspaces=False))
    assert result.manifest.workspaces_included is False
    assert be.read_manifest(result.path).workspaces_included is False
    assert result.workspaces_file_count == 0
    assert not [n for n in _archive_names(result.path) if n.startswith("workspaces/")]

    restore_env(dst)
    migrations.upgrade("head")
    _put(dst / "workspaces" / "bob" / "mine.md", "already here")
    report = be.restore_backup(result.path, target_system_dir=str(dst))
    assert report.ok, report.error
    assert (dst / "workspaces" / "bob" / "mine.md").read_text(encoding="utf-8") == "already here"
    assert not (dst / "workspaces" / "admin" / "notes.md").exists()
    assert any("does not include the profiles' working directories" in w for w in report.warnings)


def test_a_restore_merges_into_existing_working_directories(restore_env, tmp_path):
    """Files the archive carries overwrite theirs; files it does not are kept."""
    from app.backup import engine as be

    src, dst = tmp_path / "src", tmp_path / "dst"
    restore_env(src)
    migrations.upgrade("head")
    _two_profiles()
    _put(src / "workspaces" / "bob" / "shared.md", "from the backup")
    archive = be.create_backup(be.BackupOptions()).path

    restore_env(dst)
    migrations.upgrade("head")
    _put(dst / "workspaces" / "bob" / "shared.md", "local edit")
    _put(dst / "workspaces" / "bob" / "local-only.md", "keep me")
    assert be.restore_backup(archive, target_system_dir=str(dst)).ok
    assert (dst / "workspaces" / "bob" / "shared.md").read_text(encoding="utf-8") == "from the backup"
    assert (dst / "workspaces" / "bob" / "local-only.md").read_text(encoding="utf-8") == "keep me"


def test_workspaces_outside_the_system_dir_restore_into_the_current_root(restore_env, tmp_path):
    """A container keeps them in the documents mount (CREMIND_WORKSPACES_DIR);
    a native target keeps them in its system dir. Each side's own root is
    used, and stored paths inside the root follow it."""
    from app.backup import engine as be

    src, dst = tmp_path / "src", tmp_path / "dst"
    mount = tmp_path / "mount" / "workspaces"
    restore_env(src, workspaces=mount)
    migrations.upgrade("head")
    _two_profiles()
    _seed_workspaces(mount)
    _set_working_dir("bob", str(mount / "bob-2"))

    result = be.create_backup(be.BackupOptions())
    assert result.manifest.source_paths.workspaces_root == str(mount)
    assert "workspaces/admin/notes.md" in _archive_names(result.path)

    restore_env(dst)  # native: <dst>/workspaces
    migrations.upgrade("head")
    report = be.restore_backup(result.path, target_system_dir=str(dst))
    assert report.ok, report.error
    assert (dst / "workspaces" / "admin" / "notes.md").is_file()
    assert any(str(mount) in w and str(dst / "workspaces") in w for w in report.warnings), report.warnings

    set_database_provider(None)
    set_database_provider(create_database_provider())
    with get_database_provider().sync_engine().connect() as c:
        bob = c.execute(text("SELECT working_dir FROM profiles WHERE name='bob'")).scalar()
    assert bob == str(dst / "workspaces" / "bob-2")

    # And the other way round: into a target whose root is outside its
    # system dir.
    dst2, mount2 = tmp_path / "dst2", tmp_path / "mount2" / "workspaces"
    restore_env(dst2, workspaces=mount2)
    migrations.upgrade("head")
    assert be.restore_backup(result.path, target_system_dir=str(dst2)).ok
    assert (mount2 / "bob" / "app" / "poetry.lock").is_file()
    assert not (dst2 / "workspaces").exists()


def test_an_archive_from_before_the_working_directories_moves_the_folder_to_the_admin(restore_env, tmp_path):
    """Older archives carry the server-wide ``server_config.user_working_dir``:
    it is relocated on load and handed to the admin by the migration."""
    from app.backup import engine as be
    from app.backup.manifest import Manifest

    src, dst = tmp_path / "src", tmp_path / "dst"
    restore_env(src)
    migrations.upgrade("20260928c_search_tools")
    with get_database_provider().sync_engine().begin() as c:
        c.execute(text("INSERT INTO profiles (id, name, created_at, updated_at) VALUES ('p1','admin',0,0)"))
        c.execute(text(
            "INSERT INTO server_config (key, value, is_secret, updated_at) "
            "VALUES ('user_working_dir', :v, 0, 0)"
        ), {"v": str(src / "work")})
    archive = be.create_backup(be.BackupOptions()).path
    man = be.read_manifest(archive)
    assert man.alembic_revision == "20260928c_search_tools"
    assert isinstance(man, Manifest)

    restore_env(dst)
    migrations.upgrade("head")
    report = be.restore_backup(archive, target_system_dir=str(dst))
    assert report.ok, report.error

    set_database_provider(None)
    set_database_provider(create_database_provider())
    with get_database_provider().sync_engine().connect() as c:
        admin = c.execute(text("SELECT working_dir FROM profiles WHERE name='admin'")).scalar()
        legacy = c.execute(text("SELECT value FROM server_config WHERE key='user_working_dir'")).scalar()
    assert admin == str(dst / "work")
    assert legacy is None


def test_the_manifest_names_each_folder_and_the_restore_the_missing_chosen_ones(restore_env, tmp_path):
    """Each profile's folder is recorded; a folder an admin chose outside the
    workspaces root is never archived, and a restore onto a machine where
    its relocated path does not exist names the profile."""
    from app.backup import engine as be

    src, dst = tmp_path / "src", tmp_path / "dst"
    restore_env(src)
    migrations.upgrade("head")
    _two_profiles()
    chosen = src / "chosen"  # under the system dir only so it relocates to dst
    _put(chosen / "mine.md", "the admin's own file")
    _put(src / "workspaces" / "bob" / "notes.md", "bob's notes")
    _set_working_dir("admin", str(chosen))

    result = be.create_backup(be.BackupOptions())
    wd = result.manifest.working_dirs
    assert wd["admin"] == {"path": str(chosen), "default": False, "in_workspaces": False, "archived": False}
    assert wd["bob"] == {"path": str(src / "workspaces" / "bob"), "default": True,
                         "in_workspaces": True, "archived": True}
    assert result.manifest.source_paths.user_working_dir == str(chosen)
    assert be.read_manifest(result.path).summary()["working_dirs_elsewhere"] == ["admin"]
    assert not [n for n in _archive_names(result.path) if n.endswith("mine.md")]

    no_ws = be.create_backup(be.BackupOptions(
        dest=tmp_path / "no-ws.cremind-backup", include_workspaces=False,
    )).manifest.working_dirs
    assert no_ws["bob"]["in_workspaces"] is True and no_ws["bob"]["archived"] is False

    restore_env(dst)
    migrations.upgrade("head")
    report = be.restore_backup(result.path, target_system_dir=str(dst))
    assert report.ok, report.error
    assert (dst / "workspaces" / "bob" / "notes.md").is_file()
    notes = [w for w in report.warnings if "does not exist on this machine" in w]
    assert len(notes) == 1 and f"'admin' ({dst / 'chosen'})" in notes[0], report.warnings
    assert "'bob'" not in notes[0]


def test_staging_refuses_links_and_paths_that_leave_the_staging_dir(tmp_path):
    """A backup only ever holds regular files. A crafted archive's link (or an
    absolute / ``..`` / drive-letter name) is never staged, so neither a later
    member nor the copy into the workspaces root can write outside."""
    import io
    import json
    import tarfile

    from app.backup import engine as be

    archive = tmp_path / "crafted.cremind-backup"
    outside = tmp_path / "outside"
    outside.mkdir()

    def add(tf, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    with tarfile.open(str(archive), "w:gz") as tf:
        add(tf, "manifest.json", json.dumps({"format": "cremind-backup"}).encode())
        link = tarfile.TarInfo("workspaces/bob/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = str(outside)
        tf.addfile(link)
        hard = tarfile.TarInfo("workspaces/bob/hard")
        hard.type = tarfile.LNKTYPE
        hard.linkname = "manifest.json"
        tf.addfile(hard)
        add(tf, "workspaces/bob/escape/pwned.md", b"x")
        add(tf, "workspaces/../../evil.md", b"x")
        add(tf, "C:/evil.md", b"x")
        add(tf, "workspaces/bob/notes.md", b"bob's notes")

    staged = tmp_path / "staged"
    be.stage_backup(archive, None, staged)
    assert (staged / "workspaces" / "bob" / "notes.md").read_bytes() == b"bob's notes"
    assert not (staged / "workspaces" / "bob" / "hard").exists()
    escape = staged / "workspaces" / "bob" / "escape"
    assert not escape.is_symlink()
    assert list(outside.iterdir()) == []
    assert not (tmp_path / "evil.md").exists()
