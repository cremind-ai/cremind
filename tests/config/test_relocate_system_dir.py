"""Relocating the system directory must take the TLS material with it.

The Setup Wizard can move ``CREMIND_SYSTEM_DIR`` when the user picks a custom
path — and under ``CREMIND_SSL=after-setup`` the user has, by then, already
trusted the CA that lives under the old one. Leaving it behind means the next
boot generates a *different* CA and the browser warns again, despite the user
having done everything the wizard asked. That is the bug these tests pin.
"""

from __future__ import annotations

import os

import pytest


from app.config.settings import BaseConfig, relocate_system_directory


@pytest.fixture
def sysdirs(tmp_path, monkeypatch):
    """An old system dir with TLS material, and an unused new path."""
    old = tmp_path / "old"
    (old / "tls").mkdir(parents=True)
    (old / ".env").write_text("APP_URL=http://localhost:1515\n", encoding="utf-8")
    (old / "tls" / "ca.pem").write_text("ca-cert", encoding="utf-8")
    (old / "tls" / "ca.key").write_text("ca-key", encoding="utf-8")
    (old / "tls" / "cert.pem").write_text("leaf-cert", encoding="utf-8")
    (old / "tls" / "key.pem").write_text("leaf-key", encoding="utf-8")

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(old), raising=False)
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(old))
    return old, tmp_path / "new"


def test_tls_material_moves_with_the_system_dir(sysdirs):
    old, new = sysdirs

    relocate_system_directory(str(new))

    for name in ("ca.pem", "ca.key", "cert.pem", "key.pem"):
        assert (new / "tls" / name).is_file(), name
    assert (new / "tls" / "ca.pem").read_text() == "ca-cert"
    assert not (old / "tls").exists(), "the old copy must not linger"


def test_the_env_file_still_moves(sysdirs):
    """Regression guard on the behaviour that was already there."""
    old, new = sysdirs

    relocate_system_directory(str(new))

    assert (new / ".env").is_file()
    assert not (old / ".env").exists()
    assert "CREMIND_SYSTEM_DIR" in (new / ".env").read_text(encoding="utf-8")


def test_a_missing_tls_dir_is_not_an_error(tmp_path, monkeypatch):
    """Plain HTTP installs have no tls/ at all."""
    old = tmp_path / "old"
    old.mkdir()
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(old), raising=False)
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(old))

    relocate_system_directory(str(tmp_path / "new"))

    assert not (tmp_path / "new" / "tls").exists()


def test_an_existing_destination_is_left_alone(sysdirs):
    """Never clobber TLS material already at the destination."""
    old, new = sysdirs
    (new / "tls").mkdir(parents=True)
    (new / "tls" / "ca.pem").write_text("pre-existing", encoding="utf-8")

    relocate_system_directory(str(new))

    assert (new / "tls" / "ca.pem").read_text() == "pre-existing"


def test_the_copy_fallback_moves_everything(sysdirs, monkeypatch):
    """Across filesystems os.replace fails and we copy-then-delete instead."""
    old, new = sysdirs
    real_replace = os.replace

    def _replace(src, dst):
        # Fail only for the tls directory move, so the .env path is unaffected.
        if str(src).endswith("tls"):
            raise OSError("simulated cross-device move")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _replace)

    relocate_system_directory(str(new))

    for name in ("ca.pem", "ca.key", "cert.pem", "key.pem"):
        assert (new / "tls" / name).is_file(), name
    assert not (old / "tls").exists()


def test_a_failed_copy_leaves_the_original_intact(sysdirs, monkeypatch):
    """A half-moved tls/ is the outcome this must never produce.

    If the copy cannot complete, the CA the user trusted has to still be
    somewhere — so the source is left untouched and the operator is told.
    """
    old, new = sysdirs
    real_replace = os.replace
    monkeypatch.setattr(
        os, "replace",
        lambda s, d: (_ for _ in ()).throw(OSError("x")) if str(s).endswith("tls")
        else real_replace(s, d),
    )

    # ``shutil`` is imported inside the function, so patch the real module.
    import shutil

    real_copy2 = shutil.copy2
    calls = {"n": 0}

    def _copy2(src, dst, **kw):
        calls["n"] += 1
        if calls["n"] > 1:  # first file lands, the rest fail
            raise OSError("disk full")
        return real_copy2(src, dst, **kw)

    monkeypatch.setattr(shutil, "copy2", _copy2)

    relocate_system_directory(str(new))

    # Source survives, and no partial copy is left pretending to be complete.
    assert (old / "tls" / "ca.pem").is_file()
    assert sorted(p.name for p in (new / "tls").iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_key_material_keeps_its_mode_through_the_copy(sysdirs, monkeypatch):
    old, new = sysdirs
    os.chmod(old / "tls" / "ca.key", 0o600)
    real_replace = os.replace
    monkeypatch.setattr(
        os, "replace",
        lambda s, d: (_ for _ in ()).throw(OSError("x")) if str(s).endswith("tls")
        else real_replace(s, d),
    )

    relocate_system_directory(str(new))

    assert (os.stat(new / "tls" / "ca.key").st_mode & 0o777) == 0o600
    assert (os.stat(new / "tls" / "key.pem").st_mode & 0o777) == 0o600
