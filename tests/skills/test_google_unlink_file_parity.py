"""Guardrail: the backend's delete list matches what the Google skills declare.

:mod:`app.google.registry` names the files a Google link consists of. So does each
skill's ``scripts/app/config.py`` — independently, because the skills run
standalone under ``uv`` from a profile directory where ``app.*`` is not
importable. That duplication can silently drift, and when it does, an unlink
leaves a credential-bearing file behind (or deletes user config). This test asserts
the two agree, and pins the deliberate exclusions with the reason for each.

``config.py`` is parsed rather than imported: importing it would pull in
``dotenv`` and, transitively, the skill's Google libraries.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Optional

import pytest

from app.google.registry import GOOGLE_SKILLS, PRESERVED_REL, TOKEN_TMP_REL
from app.skills.sync import BUILTIN_SKILLS_DIR

# ``config.py`` builds paths from these roots; the first two are computed from
# ``__file__`` rather than assigned as a ``/`` expression, so they are seeded.
_ROOTS = {"SCRIPTS_DIR": "scripts", "PROJECT_DIR": "", "EVENTS_DIR": "events"}

# Declared in config.py but deliberately never deleted, with why.
_NEVER_DELETED = {
    "ENV_PATH": "user config, and re-materialized from tool_configs on every boot",
    "LOCK_FILE": "a live OS lock; removing it breaks the single-instance guard",
    "HEARTBEAT_FILE": "not credential-derived (and never actually written by these two)",
}

# Declared credential-bearing state that an unlink must take.
_MUST_DELETE = ("TOKEN_PATH", "STATE_FILE", "GRANTS_PATH")


def _join(base: str, name: str) -> str:
    return f"{base}/{name}" if base else name


def _resolve(node: ast.AST, known: Dict[str, str]) -> Optional[str]:
    """Resolve ``<NAME> / "literal"`` to a skill-relative POSIX path."""
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
        return None
    if not isinstance(node.right, ast.Constant) or not isinstance(node.right.value, str):
        return None
    left = node.left
    if isinstance(left, ast.Name) and left.id in known:
        return _join(known[left.id], node.right.value)
    nested = _resolve(left, known)
    return None if nested is None else _join(nested, node.right.value)


def _path_constants(config_path: Path) -> Dict[str, str]:
    tree = ast.parse(config_path.read_text(encoding="utf-8"))
    known = dict(_ROOTS)
    found: Dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        resolved = _resolve(node.value, known)
        if resolved is not None:
            known[target.id] = resolved
            found[target.id] = resolved
    return found


def _config_path(dir_name: str) -> Path:
    path = BUILTIN_SKILLS_DIR / dir_name / "scripts" / "app" / "config.py"
    assert path.is_file(), f"missing {path}"
    return path


@pytest.mark.parametrize("spec", GOOGLE_SKILLS, ids=lambda s: s.dir_name)
def test_the_declared_credential_files_are_all_deleted(spec):
    declared = _path_constants(_config_path(spec.dir_name))
    deleting = {rel.as_posix() for rel in spec.delete_rel}

    for name in _MUST_DELETE:
        if name in declared:
            assert declared[name] in deleting, (
                f"{spec.dir_name}'s {name} ({declared[name]}) holds link state but "
                "app/google/registry.py does not delete it"
            )


@pytest.mark.parametrize("spec", GOOGLE_SKILLS, ids=lambda s: s.dir_name)
def test_nothing_is_deleted_that_the_skill_did_not_declare(spec):
    """Otherwise a typo in the registry silently deletes nothing, forever."""
    declared = set(_path_constants(_config_path(spec.dir_name)).values())
    # ``.tmp`` siblings are created by TokenStore.save / _save_state at runtime
    # rather than declared, so accept them for any declared file.
    allowed = declared | {f"{value}.tmp" for value in declared}

    for rel in spec.delete_rel:
        assert rel.as_posix() in allowed, (
            f"registry deletes {rel.as_posix()} for {spec.dir_name}, which its "
            "config.py never declares"
        )


@pytest.mark.parametrize("spec", GOOGLE_SKILLS, ids=lambda s: s.dir_name)
def test_user_config_and_the_lock_are_never_deleted(spec):
    declared = _path_constants(_config_path(spec.dir_name))
    deleting = {rel.as_posix() for rel in spec.delete_rel}

    for name, why in _NEVER_DELETED.items():
        if name in declared:
            assert declared[name] not in deleting, (
                f"{spec.dir_name}'s {name} must survive an unlink: {why}"
            )
    # Belt and braces: the preserved list names them independently of config.py.
    for rel in PRESERVED_REL:
        assert rel.as_posix() not in deleting


@pytest.mark.parametrize("spec", GOOGLE_SKILLS, ids=lambda s: s.dir_name)
def test_the_temp_credential_file_is_always_deleted(spec):
    """A crash between ``write`` and ``os.replace`` leaves a full credential set
    in it, which is why every skill's .gitignore lists it."""
    assert TOKEN_TMP_REL in spec.delete_rel


@pytest.mark.parametrize("spec", GOOGLE_SKILLS, ids=lambda s: s.dir_name)
def test_the_event_folders_match_what_the_skill_declares(spec):
    declared = _path_constants(_config_path(spec.dir_name))
    event_dirs = {
        value for name, value in declared.items()
        if name.endswith("_DIR") and value.startswith("events/")
    }
    assert {rel.as_posix() for rel in spec.event_dirs_rel} == event_dirs, (
        f"{spec.dir_name}'s event folders and registry event_types disagree"
    )


@pytest.mark.parametrize("spec", GOOGLE_SKILLS, ids=lambda s: s.dir_name)
def test_the_listener_declaration_matches_the_skill(spec):
    """``has_listener`` decides whether unlink deregisters an autostart process."""
    listener = BUILTIN_SKILLS_DIR / spec.dir_name / "scripts" / "event_listener.py"
    assert listener.is_file() is spec.has_listener


def test_the_registry_covers_every_google_skill_on_disk():
    """Discovered by glob, so a sixth Google skill cannot slip past this file."""
    discovered = {
        path.parents[3].name
        for path in BUILTIN_SKILLS_DIR.glob("*/scripts/app/google/auth.py")
    }
    assert discovered == {spec.dir_name for spec in GOOGLE_SKILLS}
