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
from app.backup.rules import is_excluded


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
