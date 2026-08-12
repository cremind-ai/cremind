"""`app/cli/session.write_token` — the CLI's half of the token file.

Other processes read this file live (every `cremind` command, every
`exec_shell` spawn), and the caller has just revoked whatever it replaces — so
the write has to be atomic, permissioned, and loud when it fails.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def sysdir(tmp_path, monkeypatch):
    d = tmp_path / "sysdir"
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(d))
    return d


def test_write_then_read_round_trips(sysdir):
    import app.cli.session as s

    path = s.write_token("admin", "JWT-VALUE")
    assert Path(path).is_file()
    assert s.read_token("admin") == "JWT-VALUE"
    assert s.list_profiles() == ["admin"]


def test_creates_the_tokens_directory(sysdir):
    import app.cli.session as s

    assert not sysdir.exists()
    s.write_token("admin", "JWT")
    assert (sysdir / "tokens").is_dir()


def test_overwriting_leaves_no_temp_file_behind(sysdir):
    """A stray temp file would be cruft; one named `*.token` would be a phantom
    profile in the picker."""
    import app.cli.session as s

    s.write_token("admin", "one")
    s.write_token("admin", "two")

    assert s.read_token("admin") == "two"
    assert sorted(p.name for p in (sysdir / "tokens").iterdir()) == ["admin.token"]


def test_the_temp_name_could_never_look_like_a_profile(sysdir, monkeypatch):
    """Pin the invariant directly: if the rename fails, whatever is left must
    not be picked up by list_profiles()."""
    import app.cli.session as s

    def _fail(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", _fail)
    with pytest.raises(OSError):
        s.write_token("admin", "JWT")
    assert s.list_profiles() == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_mode_is_0600(sysdir):
    import app.cli.session as s

    path = s.write_token("admin", "JWT")
    assert Path(path).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("profile,token", [("", "JWT"), ("admin", "")])
def test_empty_arguments_are_rejected(sysdir, profile, token):
    import app.cli.session as s

    with pytest.raises(ValueError):
        s.write_token(profile, token)


def test_failures_propagate_rather_than_being_swallowed(sysdir, monkeypatch):
    """Unlike the session-map writer: a silent failure here leaves the user with
    a revoked token and no explanation."""
    import app.cli.session as s

    def _fail(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _fail)
    with pytest.raises(OSError):
        s.write_token("admin", "JWT")
