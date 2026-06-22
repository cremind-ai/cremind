"""Tests for the unified-working-directory path fix.

Covers the two pure helpers introduced/changed by the fix:

* ``system_file._safe_resolve`` — now accepts absolute paths that fall under an
  allowlist of roots (was: rejected every absolute path as traversal).
* ``system_file._report_path`` — relative inside base, absolute otherwise.
"""

import os

import pytest

from app.tools.builtin.system_file import _report_path, _safe_resolve


# --------------------------------------------------------------------------- #
# _safe_resolve
# --------------------------------------------------------------------------- #

def test_relative_path_resolves_under_base(tmp_path):
    base = str(tmp_path)
    target = _safe_resolve(base, "sub/file.txt")
    assert target == os.path.realpath(os.path.join(base, "sub", "file.txt"))


def test_leading_slash_is_treated_as_relative(tmp_path):
    # "/foo" (no drive on Windows) must stay relative to base, not escape.
    base = str(tmp_path)
    target = _safe_resolve(base, "/foo.txt")
    assert target == os.path.realpath(os.path.join(base, "foo.txt"))


def test_absolute_path_inside_base_is_accepted(tmp_path):
    base = str(tmp_path)
    abs_inside = os.path.join(base, "inside.txt")
    assert _safe_resolve(base, abs_inside) == os.path.realpath(abs_inside)


def test_absolute_path_inside_allowed_root_is_accepted(tmp_path):
    base = tmp_path / "cwd"
    skill = tmp_path / "skill"
    base.mkdir()
    skill.mkdir()
    target = _safe_resolve(str(base), str(skill / "scripts" / "main.py"),
                           allowed_roots=[str(skill)])
    assert target == os.path.realpath(str(skill / "scripts" / "main.py"))


def test_forward_slash_absolute_path_accepted_under_root(tmp_path):
    # Forward-slash absolute paths must resolve under an allowed root on Windows too.
    base = tmp_path / "cwd"
    skill = tmp_path / "skill"
    base.mkdir()
    skill.mkdir()
    fwd = str(skill).replace("\\", "/") + "/references/devices.md"
    target = _safe_resolve(str(base), fwd, allowed_roots=[str(skill)])
    assert target == os.path.realpath(str(skill / "references" / "devices.md"))


def test_absolute_path_outside_all_roots_is_rejected(tmp_path):
    base = tmp_path / "cwd"
    other = tmp_path / "other"
    base.mkdir()
    other.mkdir()
    with pytest.raises(ValueError) as exc:
        _safe_resolve(str(base), str(other / "secret.txt"))
    assert "Access denied" in str(exc.value)


def test_dotdot_escape_is_rejected(tmp_path):
    base = tmp_path / "cwd"
    base.mkdir()
    with pytest.raises(ValueError):
        _safe_resolve(str(base), "../../etc/passwd")


# --------------------------------------------------------------------------- #
# _report_path
# --------------------------------------------------------------------------- #

def test_report_path_relative_when_inside_base(tmp_path):
    base = os.path.realpath(str(tmp_path))
    full = os.path.join(base, "a", "b.txt")
    assert _report_path(full, base) == "a/b.txt"


def test_report_path_absolute_when_outside_base(tmp_path):
    base = os.path.realpath(str(tmp_path / "cwd"))
    outside = os.path.realpath(str(tmp_path / "skill" / "devices.md"))
    # Outside base → returns the absolute path (no "../.." string).
    assert _report_path(outside, base) == outside.replace(os.sep, "/")
    assert ".." not in _report_path(outside, base)
