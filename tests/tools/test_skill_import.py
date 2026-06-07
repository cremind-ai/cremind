"""Unit tests for skill delete/reset (sync.py) and import (importer.py, scanner.py).

Covers:
- ``parse_skill_dir`` / ``find_skill_dirs`` discovery across root, nested, and
  multi-skill archive layouts.
- ``parse_github_url`` parsing of URLs and ``owner/repo`` shorthands.
- ``install_skills_from_dir`` install + collision rejection (existing + built-in)
  + the "nothing valid found" error.
- ``delete_profile_skill`` traversal guard, and ``reset_builtin_skill`` restoring
  a shipped built-in from source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.skills import sync as skill_sync
from app.skills.importer import (
    SkillImportError,
    install_archive,
    install_skills_from_dir,
    parse_github_url,
)
from app.skills.scanner import find_skill_dirs, parse_skill_dir


def _make_skill(parent: Path, dir_name: str, *, name: str | None = None) -> Path:
    """Create ``parent/dir_name/SKILL.md`` with valid frontmatter."""
    skill_dir = parent / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name or dir_name}\ndescription: A test skill.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return skill_dir


# ── scanner discovery ────────────────────────────────────────────────────────


def test_parse_skill_dir_valid_and_invalid(tmp_path: Path) -> None:
    good = _make_skill(tmp_path, "alpha")
    assert parse_skill_dir(good) is not None

    # No SKILL.md
    empty = tmp_path / "empty"
    empty.mkdir()
    assert parse_skill_dir(empty) is None

    # SKILL.md missing required name
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\ndescription: no name\n---\n", encoding="utf-8")
    assert parse_skill_dir(bad) is None


def test_find_skill_dirs_root_layout(tmp_path: Path) -> None:
    # Archive whose root IS the skill dir (SKILL.md sits directly in `root`).
    root = _make_skill(tmp_path, "skill-at-root")
    found = find_skill_dirs(root)
    assert len(found) == 1
    assert found[0].name == "skill-at-root"
    assert found[0].dir_path == root


def test_find_skill_dirs_nested_and_multi(tmp_path: Path) -> None:
    # repo-main/skills/{one,two}/SKILL.md  +  repo-main/three/SKILL.md
    repo = tmp_path / "repo-main"
    skills = repo / "skills"
    _make_skill(skills, "one")
    _make_skill(skills, "two")
    _make_skill(repo, "three")
    found = {s.name for s in find_skill_dirs(tmp_path)}
    assert found == {"one", "two", "three"}


def test_find_skill_dirs_does_not_descend_into_a_skill(tmp_path: Path) -> None:
    # A skill that happens to contain a nested dir with its own SKILL.md (e.g.
    # an example) must not be double-counted -- we stop at the first skill.
    outer = _make_skill(tmp_path, "outer")
    _make_skill(outer, "example")  # nested SKILL.md inside the skill
    found = find_skill_dirs(tmp_path)
    assert [s.name for s in found] == ["outer"]


# ── GitHub URL parsing ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/owner/repo", ("owner", "repo")),
        ("http://github.com/owner/repo.git", ("owner", "repo")),
        ("github.com/owner/repo/", ("owner", "repo")),
        ("https://www.github.com/owner/repo/tree/main", ("owner", "repo")),
        ("owner/repo", ("owner", "repo")),
        ("owner/repo.git", ("owner", "repo")),
    ],
)
def test_parse_github_url_ok(url: str, expected: tuple[str, str]) -> None:
    assert parse_github_url(url) == expected


@pytest.mark.parametrize("bad", ["", "   ", "not a url", "https://gitlab.com/o/r", "ftp://x"])
def test_parse_github_url_rejects(bad: str) -> None:
    with pytest.raises(SkillImportError):
        parse_github_url(bad)


# ── install_skills_from_dir ──────────────────────────────────────────────────


def _point_system_dir(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    from app.config.settings import BaseConfig

    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(root), raising=False)


def test_install_copies_valid_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _point_system_dir(monkeypatch, tmp_path / "system")
    src = tmp_path / "src"
    _make_skill(src, "cool-skill")

    result = install_skills_from_dir(src, "default")

    assert result["installed"] == ["cool-skill"]
    dest = skill_sync.profile_skills_dir("default") / "cool-skill" / "SKILL.md"
    assert dest.exists()


def test_install_archive_zip_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    _point_system_dir(monkeypatch, tmp_path / "system")
    # Build a zip whose top folder contains a skill: repo/zipped-skill/SKILL.md
    staging = tmp_path / "staging"
    _make_skill(staging / "repo", "zipped-skill")
    archive_base = tmp_path / "bundle"
    archive_path = Path(shutil.make_archive(str(archive_base), "zip", root_dir=staging))

    result = install_archive(archive_path, "bundle.zip", "default")

    assert result["installed"] == ["zipped-skill"]
    assert (skill_sync.profile_skills_dir("default") / "zipped-skill" / "SKILL.md").exists()


def test_install_rejects_existing_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_system_dir(monkeypatch, tmp_path / "system")
    # Pre-existing skill in the profile.
    _make_skill(skill_sync.profile_skills_dir("default"), "dup")
    src = tmp_path / "src"
    _make_skill(src, "dup")

    with pytest.raises(SkillImportError):
        install_skills_from_dir(src, "default")


def test_install_rejects_builtin_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_system_dir(monkeypatch, tmp_path / "system")
    builtins = skill_sync.builtin_skill_dir_names()
    assert builtins, "expected shipped built-in skills to exist"
    name = sorted(builtins)[0]
    src = tmp_path / "src"
    _make_skill(src, name)

    with pytest.raises(SkillImportError):
        install_skills_from_dir(src, "default")


def test_install_raises_when_no_skill_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_system_dir(monkeypatch, tmp_path / "system")
    src = tmp_path / "src"
    src.mkdir()
    (src / "README.md").write_text("not a skill", encoding="utf-8")

    with pytest.raises(SkillImportError):
        install_skills_from_dir(src, "default")


# ── delete / reset ───────────────────────────────────────────────────────────


def test_delete_profile_skill_removes_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_system_dir(monkeypatch, tmp_path / "system")
    skills_root = skill_sync.profile_skills_dir("default")
    _make_skill(skills_root, "gone")

    assert skill_sync.delete_profile_skill("default", "gone") is True
    assert not (skills_root / "gone").exists()
    # Idempotent: deleting again is a no-op.
    assert skill_sync.delete_profile_skill("default", "gone") is False


@pytest.mark.parametrize("evil", ["..", "../escape", "../../etc"])
def test_delete_profile_skill_blocks_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evil: str
) -> None:
    _point_system_dir(monkeypatch, tmp_path / "system")
    with pytest.raises(ValueError):
        skill_sync.delete_profile_skill("default", evil)


def test_reset_builtin_restores_from_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_system_dir(monkeypatch, tmp_path / "system")
    name = sorted(skill_sync.builtin_skill_dir_names())[0]
    pristine = (skill_sync.BUILTIN_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")

    skills_root = skill_sync.profile_skills_dir("default")
    skills_root.mkdir(parents=True, exist_ok=True)
    # Simulate a user-tampered copy.
    tampered = skills_root / name
    tampered.mkdir()
    (tampered / "SKILL.md").write_text("---\nname: x\ndescription: tampered\n---\n", encoding="utf-8")

    skill_sync.reset_builtin_skill("default", name)

    restored = (skills_root / name / "SKILL.md").read_text(encoding="utf-8")
    assert restored == pristine


def test_reset_builtin_rejects_non_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_system_dir(monkeypatch, tmp_path / "system")
    with pytest.raises(ValueError):
        skill_sync.reset_builtin_skill("default", "definitely-not-a-builtin")


def test_is_builtin_skill_dir() -> None:
    builtins = skill_sync.builtin_skill_dir_names()
    assert builtins
    assert skill_sync.is_builtin_skill_dir(sorted(builtins)[0]) is True
    assert skill_sync.is_builtin_skill_dir("nope-not-real") is False
    assert skill_sync.is_builtin_skill_dir("") is False
