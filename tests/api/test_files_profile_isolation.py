"""One profile's exports are not another profile's to read.

The file routes have always been a path-traversal sandbox rather than an
authorization boundary: every authenticated profile may read anything under the
System Directory. That was survivable while the directory held working data and
credential stores were denied by name.

It stopped being survivable when ``cremind profile wizard finish`` and
``cremind config export`` started writing configuration files there. Those files
embed a live JWT, they live at a fully predictable path
(``<system dir>/<profile>/exports/cremind-<target>-config.md``), and the
download chip in chat resolves through the very route that would serve them to
anyone — so hiding the directory by name, the way credential stores are hidden,
would break the feature instead of protecting it.

Hence a narrow rule: ``<system dir>/<p>/exports/`` belongs to ``p``. Admin is
not exempt, because its own exports are in its own slice and the exemption would
only hand the most prompt-injectable profile a reader for everyone else's
tokens.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.api import files as files_api


@pytest.fixture
def system_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(files_api.BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(files_api, "get_user_working_directory", lambda: str(tmp_path / "work"))
    (tmp_path / "admin" / "exports").mkdir(parents=True)
    (tmp_path / "admin" / "exports" / "cremind-javis-config.md").write_text("token", encoding="utf-8")
    (tmp_path / "admin" / "uploads_tmp").mkdir(parents=True)
    (tmp_path / "admin" / "uploads_tmp" / "note.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "shared").mkdir()
    (tmp_path / "work").mkdir()
    return tmp_path


def _export(system_dir: Path) -> str:
    return os.path.realpath(system_dir / "admin" / "exports" / "cremind-javis-config.md")


def test_another_profile_cannot_reach_an_export(system_dir: Path) -> None:
    assert files_api._is_inside_allowed(_export(system_dir), None, "bob") is False


def test_the_owner_can_reach_its_own_export(system_dir: Path) -> None:
    """The download chip in chat goes through this route — denying the owner
    would break the hand-off this whole feature exists for."""
    assert files_api._is_inside_allowed(_export(system_dir), None, "admin") is True


def test_admin_is_not_exempt_from_another_profiles_export(system_dir: Path) -> None:
    (system_dir / "bob" / "exports").mkdir(parents=True)
    target = os.path.realpath(system_dir / "bob" / "exports" / "cremind-bob-config.md")
    assert files_api._is_inside_allowed(target, None, "admin") is False


def test_a_route_that_forgets_to_pass_a_profile_fails_closed(system_dir: Path) -> None:
    """The default has to deny: a forgotten argument should surface as a 403 in
    a test, not as someone else's token on the wire."""
    assert files_api._is_inside_allowed(_export(system_dir)) is False


def test_the_rule_is_narrow_and_leaves_the_rest_of_the_subtree_alone(system_dir: Path) -> None:
    """Deliberately not the whole ``<system dir>/<profile>/`` slice: a group
    chat renders every member agent's file tree out of it, and widening this
    needs its own audit."""
    other = os.path.realpath(system_dir / "admin" / "uploads_tmp" / "note.txt")
    assert files_api._is_inside_allowed(other, None, "bob") is True
    shared = os.path.realpath(system_dir / "shared")
    assert files_api._is_inside_allowed(shared, None, "bob") is True


def test_a_wizard_draft_is_denied_to_everyone_including_its_owner(system_dir: Path) -> None:
    """Drafts hold the API key the user pasted, until ``finish`` posts it.
    Nothing legitimately browses them — the CLI reads them straight off disk."""
    draft = system_dir / "admin" / "cli-wizards"
    draft.mkdir(parents=True)
    (draft / "javis.json").write_text("{}", encoding="utf-8")
    target = os.path.realpath(draft / "javis.json")
    assert files_api._is_inside_allowed(target, None, "admin") is False
    assert files_api._is_inside_allowed(target, None, "bob") is False
    # The legacy ``/api/files/{path}`` route resolves separately; it must agree.
    assert files_api._safe_resolve("admin/cli-wizards/javis.json", "admin") is None


def test_the_legacy_path_route_applies_the_same_rule(system_dir: Path) -> None:
    assert files_api._safe_resolve("admin/exports/cremind-javis-config.md", "bob") is None
    assert files_api._safe_resolve("admin/exports/cremind-javis-config.md", "admin") is not None


def test_a_traversal_out_of_a_private_directory_is_still_refused(system_dir: Path) -> None:
    """The rule reads resolved paths, so ``..`` cannot walk sideways into one
    and a symlink cannot walk out of one."""
    sneaky = os.path.join(
        str(system_dir), "shared", "..", "admin", "exports", "cremind-javis-config.md",
    )
    assert files_api._is_inside_allowed(os.path.realpath(sneaky), None, "bob") is False


# ── User Document Search's index ───────────────────────────────────────────


def _index(system_dir: Path) -> Path:
    d = system_dir / "storage" / "userdocs" / "0f6c2a9e-uid-of-alice"
    d.mkdir(parents=True)
    (d / "index.db").write_bytes(b"SQLite format 3\0 alice's extracted document text")
    return d


def test_no_profile_can_read_the_userdocs_index_through_the_file_routes(system_dir: Path) -> None:
    """Every profile's index (the text of its files) sits under one directory
    of the system folder, which the file routes otherwise serve. Nobody gets
    it there — not another profile, not its owner, not admin: the index is
    read only through the profile-scoped /api/userdocs routes."""
    d = _index(system_dir)
    target = os.path.realpath(d / "index.db")
    for who in ("bob", "admin", "alice"):
        assert files_api._is_inside_allowed(target, None, who) is False
        assert files_api._is_inside_allowed(os.path.realpath(d), None, who) is False
    assert files_api._safe_resolve("storage/userdocs/0f6c2a9e-uid-of-alice/index.db", "admin") is None
    assert files_api._safe_resolve("storage/userdocs", "bob") is None
    sneaky = os.path.join(str(system_dir), "shared", "..", "storage", "userdocs", d.name, "index.db")
    assert files_api._is_inside_allowed(os.path.realpath(sneaky), None, "bob") is False


def test_the_agents_file_tool_refuses_the_userdocs_index(system_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile whose working directory is the system folder (the built-in
    setup profile's default) must not reach another profile's index by a
    relative path."""
    from app.tools.builtin import system_file

    d = _index(system_dir)
    with pytest.raises(ValueError, match="internal index"):
        system_file._safe_resolve(str(system_dir), f"storage/userdocs/{d.name}/index.db")
    # The rest of the system folder is unaffected.
    assert system_file._safe_resolve(str(system_dir), "shared") == os.path.realpath(system_dir / "shared")
