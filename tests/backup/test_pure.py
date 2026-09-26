"""Unit tests for the pure backup modules (no DB): path relocation, file
inclusion rules, and the encryption envelope."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from app.backup import crypto
from app.backup.manifest import Manifest, SourcePaths
from app.backup.paths import (
    RelocationReport,
    build_path_map,
    relocate_command,
    relocate_path,
    transform_row,
)
from app.backup.rules import is_excluded, iter_backup_files, iter_workspace_files


def _win_manifest() -> Manifest:
    return Manifest(
        app_version="0.0.8", alembic_revision="r1", db_provider="sqlite",
        platform="win32", hostname="pc",
        source_paths=SourcePaths(
            system_dir=r"C:\Users\alice\.cremind",
            home_dir=r"C:\Users\alice",
            user_working_dir=r"C:\Users\alice\Documents",
            sep="\\", case_insensitive=True,
        ),
    )


# ── path relocation ────────────────────────────────────────────────────────


def test_relocate_windows_to_posix():
    pm = build_path_map(_win_manifest(), "/root/.cremind", "/root")
    got, changed, was_abs = relocate_path(pm, r"C:\Users\alice\.cremind\admin\skills\gmail")
    assert got == "/root/.cremind/admin/skills/gmail"
    assert changed and was_abs


def test_relocate_home_prefix_when_outside_system_dir():
    pm = build_path_map(_win_manifest(), "/root/.cremind", "/home/bob")
    got, changed, _ = relocate_path(pm, r"C:\Users\alice\Documents\notes")
    assert got == "/home/bob/Documents/notes"
    assert changed


def test_relocate_system_dir_wins_over_home():
    # A path under both prefixes must relocate via the (longer) system-dir rule.
    pm = build_path_map(_win_manifest(), "/root/.cremind", "/home/bob")
    got, _, _ = relocate_path(pm, r"C:\Users\alice\.cremind\admin")
    assert got == "/root/.cremind/admin"


def test_relocate_unmapped_absolute_left_alone():
    pm = build_path_map(_win_manifest(), "/root/.cremind", "/root")
    got, changed, was_abs = relocate_path(pm, r"D:\projects\x")
    assert got == r"D:\projects\x"
    assert not changed and was_abs


def test_relocate_relative_untouched():
    pm = build_path_map(_win_manifest(), "/root/.cremind", "/root")
    got, changed, was_abs = relocate_path(pm, "relative/path")
    assert got == "relative/path"
    assert not changed and not was_abs


def test_relocate_posix_to_windows():
    m = Manifest(
        app_version="0.0.8", alembic_revision="r1", db_provider="sqlite",
        platform="linux", hostname="srv",
        source_paths=SourcePaths(system_dir="/root/.cremind", home_dir="/root",
                                 user_working_dir="/root/Documents", sep="/", case_insensitive=False),
    )
    pm = build_path_map(m, r"C:\Users\bob\.cremind", r"C:\Users\bob")
    got, changed, _ = relocate_path(pm, "/root/.cremind/admin/skills/x")
    assert got == r"C:\Users\bob\.cremind\admin\skills\x"
    assert changed


def test_relocate_command_tokens():
    pm = build_path_map(_win_manifest(), "/root/.cremind", "/root")
    cmd = r"uv run C:\Users\alice\.cremind\admin\skills\gmail\scripts\listener.py --flag"
    new, changed = relocate_command(pm, cmd)
    assert new == "uv run /root/.cremind/admin/skills/gmail/scripts/listener.py --flag"
    assert changed


def test_transform_row_records_relocations_and_unmapped():
    pm = build_path_map(_win_manifest(), "/root/.cremind", "/root")
    rep = RelocationReport()
    row = {"working_dir": r"C:\Users\alice\.cremind\admin\x", "command": "uv run x.py", "is_pty": False}
    transform_row(pm, "autostart_processes", row, rep)
    assert row["working_dir"] == "/root/.cremind/admin/x"
    assert len(rep.relocated) == 1

    rep2 = RelocationReport()
    row2 = {"working_dir": r"D:\external\dir", "command": "x", "is_pty": False}
    transform_row(pm, "autostart_processes", row2, rep2)
    assert row2["working_dir"] == r"D:\external\dir"  # unchanged
    assert len(rep2.unmapped) == 1


def test_transform_row_relocates_the_documentation_search_folder_under_either_table_name():
    """Rows are relocated while loading into the ARCHIVE's schema: an archive
    from before the rename carries ``userdoc_sources``, a newer one
    ``document_sources`` — both hold the profile's indexed folder."""
    pm = build_path_map(_win_manifest(), "/root/.cremind", "/home/bob")
    for table in ("document_sources", "userdoc_sources"):
        rep = RelocationReport()
        row = {"profile": "admin", "kind": "local", "root_path": r"C:\Users\alice\Documents\notes"}
        transform_row(pm, table, row, rep)
        assert row["root_path"] == "/home/bob/Documents/notes", table
        assert len(rep.relocated) == 1
    rep = RelocationReport()
    drive = {"profile": "admin", "kind": "drive", "root_path": None}
    transform_row(pm, "document_sources", drive, rep)
    assert drive["root_path"] is None and not rep.relocated and not rep.unmapped


def test_transform_row_server_config_only_working_dir_key():
    pm = build_path_map(_win_manifest(), "/root/.cremind", "/root")
    rep = RelocationReport()
    row = {"key": "user_working_dir", "value": r"C:\Users\alice\Documents"}
    transform_row(pm, "server_config", row, rep)
    assert row["value"] == "/root/Documents"

    rep2 = RelocationReport()
    other = {"key": "jwt_secret", "value": r"C:\not\a\path\secret"}
    transform_row(pm, "server_config", other, rep2)
    assert other["value"] == r"C:\not\a\path\secret"  # untouched


# ── per-profile working directories ─────────────────────────────────────────


def _win_manifest_with_workspaces() -> Manifest:
    m = _win_manifest()
    m.source_paths.workspaces_root = r"C:\Users\alice\.cremind\workspaces"
    return m


def test_a_profiles_chosen_working_dir_is_relocated():
    """``profiles.working_dir`` holds a folder the admin chose (NULL = the
    default, which needs no relocation)."""
    pm = build_path_map(_win_manifest(), "/root/.cremind", "/root")
    rep = RelocationReport()
    admin = {"name": "admin", "working_dir": r"C:\Users\alice\Documents"}
    transform_row(pm, "profiles", admin, rep)
    assert admin["working_dir"] == "/root/Documents"
    assert rep.relocated == [{"table": "profiles", "column": "working_dir", "profile": "admin",
                              "old": r"C:\Users\alice\Documents", "new": "/root/Documents"}]

    bob = {"name": "bob", "working_dir": None}
    transform_row(pm, "profiles", bob, rep)
    assert bob["working_dir"] is None and len(rep.relocated) == 1 and not rep.unmapped

    # An archive from before the column existed carries no such key at all.
    old = {"name": "carol"}
    transform_row(pm, "profiles", old, rep)
    assert "working_dir" not in old


def test_a_chosen_folder_from_the_other_os_that_cannot_be_mapped_falls_back_to_the_default():
    """``D:\\work`` means nothing on Linux: resolving it there would make a
    folder named ``D:\\work`` in the server's cwd. The profile goes back to
    its default folder, and the report names it. On the same OS family the
    folder may exist, so it is kept (and reported as unmapped)."""
    pm = build_path_map(_win_manifest(), "/root/.cremind", "/root")
    rep = RelocationReport()
    bob = {"name": "bob", "working_dir": r"D:\work"}
    transform_row(pm, "profiles", bob, rep)
    assert bob["working_dir"] is None
    assert rep.unmapped == [{"table": "profiles", "column": "working_dir", "value": r"D:\work",
                             "profile": "bob", "reset_to_default": True}]

    # An older archive's server-wide folder becomes the admin's: an empty
    # value leaves the admin on its default after the migration.
    legacy = {"key": "user_working_dir", "value": r"D:\work"}
    transform_row(pm, "server_config", legacy, rep)
    assert legacy["value"] == ""
    assert rep.unmapped[-1]["profile"] == "admin" and rep.unmapped[-1]["reset_to_default"]

    same_os = build_path_map(_win_manifest(), r"C:\Users\bob\.cremind", r"C:\Users\bob")
    rep = RelocationReport()
    carol = {"name": "carol", "working_dir": r"D:\work"}
    transform_row(same_os, "profiles", carol, rep)
    assert carol["working_dir"] == r"D:\work"
    assert rep.unmapped == [{"table": "profiles", "column": "working_dir", "value": r"D:\work",
                             "profile": "carol"}]


def test_a_path_in_the_workspaces_root_follows_the_targets_root():
    """The target may keep the workspaces outside its system dir (a
    container's CREMIND_WORKSPACES_DIR): the longer workspaces rule wins over
    the system-dir rule it sits under."""
    pm = build_path_map(
        _win_manifest_with_workspaces(), "/root/.cremind", "/root",
        target_workspaces_root="/root/Documents/cremind-workspaces",
    )
    got, changed, _ = relocate_path(pm, r"C:\Users\alice\.cremind\workspaces\bob-2\src")
    assert changed and got == "/root/Documents/cremind-workspaces/bob-2/src"
    # Everything else in the system dir still maps to the target's.
    got, _, _ = relocate_path(pm, r"C:\Users\alice\.cremind\admin\skills\x")
    assert got == "/root/.cremind/admin/skills/x"

    rep = RelocationReport()
    row = {"name": "bob", "working_dir": r"C:\Users\alice\.cremind\workspaces\bob-2"}
    transform_row(pm, "profiles", row, rep)
    assert row["working_dir"] == "/root/Documents/cremind-workspaces/bob-2"
    # The admin's folder follows the same rule; a user's own ``workspaces``
    # folder under the home dir relocates by the home rule, as any other.
    row = {"name": "admin", "working_dir": r"C:\Users\alice\.cremind\workspaces\admin"}
    transform_row(pm, "profiles", row, rep)
    assert row["working_dir"] == "/root/Documents/cremind-workspaces/admin"
    got, _, _ = relocate_path(pm, r"C:\Users\alice\Documents\workspaces\client-x")
    assert got == "/root/Documents/workspaces/client-x"


def test_without_a_target_root_the_source_root_adds_no_rule():
    """Blueprints (and callers that predate the workspaces) pass no target
    root: such paths relocate through the system/home rules as before."""
    pm = build_path_map(_win_manifest_with_workspaces(), "/root/.cremind", "/root")
    got, _, _ = relocate_path(pm, r"C:\Users\alice\.cremind\workspaces\bob")
    assert got == "/root/.cremind/workspaces/bob"
    assert len(pm.rules) == 2


def test_a_workspaces_root_outside_home_and_system_dir_is_mapped_not_left_unmapped():
    m = Manifest(
        app_version="0.0.8", alembic_revision="r1", db_provider="sqlite",
        platform="linux", hostname="srv",
        source_paths=SourcePaths(system_dir="/root/.cremind", home_dir="/root",
                                 user_working_dir="/root/Documents", sep="/",
                                 case_insensitive=False, workspaces_root="/data/workspaces"),
    )
    pm = build_path_map(m, r"C:\Users\bob\.cremind", r"C:\Users\bob",
                        target_workspaces_root=r"C:\Users\bob\.cremind\workspaces")
    rep = RelocationReport()
    row = {"id": "c1", "working_directory": "/data/workspaces/ann/project"}
    transform_row(pm, "conversations", row, rep)
    assert row["working_directory"] == r"C:\Users\bob\.cremind\workspaces\ann\project"
    assert not rep.unmapped


def test_manifest_records_the_workspaces_and_reads_old_archives():
    m = _win_manifest_with_workspaces()
    m.workspaces_included = True
    d = m.to_dict()
    assert d["files"]["workspaces_included"] is True
    assert d["files"]["workspaces_member_prefix"] == "workspaces/"
    assert d["source_paths"]["workspaces_root"] == r"C:\Users\alice\.cremind\workspaces"
    back = Manifest.from_dict(d)
    assert back.workspaces_included is True
    assert back.source_paths.workspaces_root == r"C:\Users\alice\.cremind\workspaces"
    assert back.summary()["workspaces_included"] is True

    # An archive made before the per-profile working directories: none of the
    # new keys, and it never carried the workspaces.
    d.pop("files")
    d["source_paths"].pop("workspaces_root")
    old = Manifest.from_dict(d)
    assert old.workspaces_included is False
    assert old.source_paths.workspaces_root == ""
    assert old.source_paths.user_working_dir == r"C:\Users\alice\Documents"


def test_the_workspaces_keep_user_files_that_the_system_prunes_drop(tmp_path: Path):
    """Suffix prunes (``.lock .tmp .pyc .pyo``) are for Cremind's own trees; in
    a working directory they are the user's files. Only the dependency and
    virtualenv directories stay out."""
    root = tmp_path / "workspaces"
    for rel in (
        "admin/notes.md",
        "admin/project/poetry.lock",
        "admin/project/draft.tmp",
        "admin/project/compiled.pyc",
        "bob/report.pyo",
        "bob/app/node_modules/left-pad/index.js",
        "bob/app/.venv/pyvenv.cfg",
        "bob/app/venv/bin/python",
        "bob/app/pkg/__pycache__/m.cpython-313.pyc",
        ".deleted/carol-20260101-000000/kept.md",
    ):
        _touch(root, rel)

    got = {rel for _, rel in iter_workspace_files(str(root))}
    assert got == {
        "admin/notes.md",
        "admin/project/poetry.lock",
        "admin/project/draft.tmp",
        "admin/project/compiled.pyc",
        "bob/report.pyo",
        ".deleted/carol-20260101-000000/kept.md",
    }
    # The same names inside a profile tree of the system dir ARE pruned.
    assert is_excluded("admin/project/poetry.lock", is_dir=False)


def test_the_workspaces_walk_never_enters_the_system_dir(tmp_path: Path):
    """A workspaces root that contains the system dir (an odd
    CREMIND_WORKSPACES_DIR) must not archive the DB or the tokens through
    the back door; nor the archive being written into it."""
    root = tmp_path / "home"
    sysdir = root / ".cremind"
    _touch(root, ".cremind/storage/cremind.db")
    _touch(root, ".cremind/tokens/admin.token")
    _touch(root, "ann/notes.md")
    _touch(root, "ann/out.cremind-backup.part")

    got = {
        rel for _, rel in iter_workspace_files(
            str(root), exclude_dirs=[str(sysdir)],
            exclude_files=[str(root / "ann" / "out.cremind-backup.part")],
        )
    }
    assert got == {"ann/notes.md"}
    # A root that IS the excluded dir yields nothing; a missing root too.
    assert list(iter_workspace_files(str(sysdir), exclude_dirs=[str(sysdir)])) == []
    assert list(iter_workspace_files(str(tmp_path / "missing"))) == []


# ── inclusion / exclusion rules ─────────────────────────────────────────────


def test_rules_exclude_derived_and_transient():
    assert is_excluded("admin/skills/gmail/scripts/.env", is_dir=False)
    assert is_excluded("admin/uploads_tmp", is_dir=True)
    assert is_excluded("admin/uploads_tmp/c1/file.bin", is_dir=False)
    assert is_excluded("admin/tools/builtin/exec_shell/stdout", is_dir=True)
    assert is_excluded("admin/skills/x/__pycache__", is_dir=True)
    assert is_excluded("admin/skills/x/y.pyc", is_dir=False)
    assert is_excluded("browser-profile/Default/Cache", is_dir=True)


def test_rules_include_real_content():
    assert not is_excluded("tokens/admin.token", is_dir=False)
    assert not is_excluded("admin/skills/gmail/scripts/.google_token.json", is_dir=False)
    assert not is_excluded("admin/PERSONA.md", is_dir=False)
    assert not is_excluded("admin/documents/note.md", is_dir=False)
    # A "Cache" dir outside a browser-profile tree is NOT pruned.
    assert not is_excluded("admin/documents/Cache", is_dir=True)


def _touch(base: Path, rel: str) -> None:
    path = base.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def test_rules_exclude_the_documents_index_and_model_cache_by_name():
    assert is_excluded("storage/documents", is_dir=True)
    assert is_excluded("storage/documents/k7m2/index.db", is_dir=False)
    assert is_excluded(".cache", is_dir=True)
    assert is_excluded(".cache/huggingface/hub/models--x/blobs/abc", is_dir=False)
    # Only at the top: a profile's own folders with these names are its data.
    assert not is_excluded("storage", is_dir=True)
    assert not is_excluded("storage/documents.md", is_dir=False)
    assert not is_excluded("admin/storage/documents/notes.md", is_dir=False)
    assert not is_excluded("admin/.cache/keep.txt", is_dir=False)


def test_iter_backup_files_never_walks_the_index_or_the_model_cache(tmp_path: Path):
    """The index and the model cache stay out even when a profile named
    ``storage`` makes the walk start where the index lives — while the rest
    of that profile's files are backed up as usual."""
    for rel in (
        "storage/documents/k7m2/index.db",
        "storage/documents/k7m2/index.db-wal",
        "storage/documents/tmp/extract-1/page.png",
        ".cache/huggingface/hub/models--intfloat--multilingual-e5/blobs/abc",
        ".cache/sentence-transformers/model/config.json",
        "admin/PERSONA.md",
        "admin/.cache/keep.txt",
        "storage/PERSONA.md",
        "storage/skills/notes/SKILL.md",
    ):
        _touch(tmp_path, rel)

    backed_up = {rel for _, rel in iter_backup_files(str(tmp_path), ["admin", "storage"])}
    assert backed_up == {
        "admin/PERSONA.md",
        "admin/.cache/keep.txt",
        "storage/PERSONA.md",
        "storage/skills/notes/SKILL.md",
    }
    # Without that profile nothing under storage/ is walked at all.
    assert {rel for _, rel in iter_backup_files(str(tmp_path), ["admin"])} == {
        "admin/PERSONA.md", "admin/.cache/keep.txt",
    }


_ADMIN_UID = "a1a1a1a1-0000-4000-8000-000000000001"
_BOB_UID = "b2b2b2b2-0000-4000-8000-000000000002"


def test_each_profiles_manual_pages_are_backed_up_by_uuid_and_derived_data_is_not(tmp_path: Path):
    """A profile's own Cremind manual pages live outside its name-keyed tree
    (``storage/cremind_documents/profiles/<uuid>``) and are user content: they
    are archived. The shared mirror, both index roots and the relocation's
    bookkeeping are derived or local: never archived. A deleted profile's
    leftover pages are not either."""
    for rel in (
        f"storage/cremind_documents/profiles/{_ADMIN_UID}/mine.md",
        f"storage/cremind_documents/profiles/{_ADMIN_UID}/sub/deep.md",
        f"storage/cremind_documents/profiles/{_ADMIN_UID}/__pycache__/x.pyc",
        f"storage/cremind_documents/profiles/{_BOB_UID}/bob.md",
        "storage/cremind_documents/profiles/deleted-profile-uid/old.md",
        "storage/cremind_documents/shared/document.md",
        f"storage/documents/{_ADMIN_UID}/index.db",
        f"storage/userdocs/{_ADMIN_UID}/index.db-wal",
        "storage/document-relocation.json",
        "admin/PERSONA.md",
        "bob/PERSONA.md",
    ):
        _touch(tmp_path, rel)

    backed_up = {
        rel for _, rel in iter_backup_files(
            str(tmp_path), ["admin", "bob"], profile_uids=[_ADMIN_UID, _BOB_UID],
        )
    }
    assert backed_up == {
        "admin/PERSONA.md",
        "bob/PERSONA.md",
        f"storage/cremind_documents/profiles/{_ADMIN_UID}/mine.md",
        f"storage/cremind_documents/profiles/{_ADMIN_UID}/sub/deep.md",
        f"storage/cremind_documents/profiles/{_BOB_UID}/bob.md",
    }


def test_a_profile_named_storage_does_not_duplicate_or_leak_manual_pages(tmp_path: Path):
    """Its walk starts at ``<SYS>/storage`` and so passes the manual roots:
    each page is archived once, and a deleted profile's pages still are not."""
    for rel in (
        f"storage/cremind_documents/profiles/{_ADMIN_UID}/mine.md",
        "storage/cremind_documents/profiles/deleted-profile-uid/old.md",
        "storage/cremind_documents/shared/document.md",
        "storage/document-relocation.lock",
        "storage/skills/notes/SKILL.md",
    ):
        _touch(tmp_path, rel)

    rels = [
        rel for _, rel in iter_backup_files(
            str(tmp_path), ["storage"], profile_uids=[_ADMIN_UID, "5707-uid-of-storage"],
        )
    ]
    assert sorted(rels) == sorted([
        "storage/skills/notes/SKILL.md",
        f"storage/cremind_documents/profiles/{_ADMIN_UID}/mine.md",
    ])


def test_the_manual_root_rules():
    assert is_excluded("storage/cremind_documents/shared", is_dir=True)
    assert is_excluded("storage/cremind_documents/shared/document.md", is_dir=False)
    assert not is_excluded(f"storage/cremind_documents/profiles/{_ADMIN_UID}/uploads_tmp/x.md", is_dir=False)
    assert is_excluded(f"storage/cremind_documents/profiles/{_ADMIN_UID}/a.tmp", is_dir=False)
    assert is_excluded("storage/userdocs", is_dir=True)


# ── encryption envelope ─────────────────────────────────────────────────────


def _encrypt_tar(path: Path, payload: bytes, passphrase: str, manifest: Manifest) -> None:
    raw = open(path, "wb")
    header, salt, nonce_prefix = crypto.new_header(manifest.to_dict())
    crypto.write_envelope_header(raw, header)
    key, _ = crypto.derive_key_from_header(passphrase, header)
    enc = crypto.EncryptingWriter(raw, key, nonce_prefix)
    tf = tarfile.open(fileobj=enc, mode="w|gz")
    info = tarfile.TarInfo("data.bin")
    info.size = len(payload)
    tf.addfile(info, io.BytesIO(payload))
    tf.close()
    enc.finalize()
    enc.close()
    raw.close()


def test_crypto_roundtrip_multichunk(tmp_path: Path):
    arc = tmp_path / "a.cremind-backup"
    payload = b"hello world " * 100000  # > 1 chunk
    _encrypt_tar(arc, payload, "hunter2", _win_manifest())

    assert crypto.is_encrypted(arc)
    assert crypto.verify_passphrase(arc, "hunter2")
    assert not crypto.verify_passphrase(arc, "wrong")

    # Manifest readable from the plaintext envelope header without passphrase.
    with open(arc, "rb") as f:
        header = crypto.read_envelope_header(f)
    assert header["manifest"]["app_version"] == "0.0.8"

    # Full decrypt round-trip.
    with open(arc, "rb") as f:
        header = crypto.read_envelope_header(f)
        key, np = crypto.derive_key_from_header("hunter2", header)
        dec = crypto.DecryptingReader(f, key, header, np)
        tf = tarfile.open(fileobj=dec, mode="r|gz")
        member = tf.next()
        data = tf.extractfile(member).read()
        tf.close()
    assert data == payload


def test_crypto_plain_gzip_not_flagged_encrypted(tmp_path: Path):
    plain = tmp_path / "plain.tar.gz"
    with tarfile.open(str(plain), mode="w:gz") as tf:
        info = tarfile.TarInfo("x")
        info.size = 3
        tf.addfile(info, io.BytesIO(b"abc"))
    assert not crypto.is_encrypted(plain)
    assert crypto.verify_passphrase(plain, "anything")  # no passphrase needed
