"""Guardrail: the five Google skills' ``google/`` helpers stay byte-identical.

Each Google skill is a standalone ``uv`` project, run from a profile directory
where ``app.*`` is not importable — so it cannot import a shared helper and every
skill ships its **own copy** of ``google/auth.py`` and friends. The copies are
currently identical, and they have to stay that way: a fix (or a new verb like
``unlink``) applied to one and forgotten in the others produces a skill that
silently lacks it, which is exactly the failure this test guards against.

Editing recipe, when this fails: change ``gmail``'s copy, then copy it over the
other four.

Same intent as :mod:`tests.skills.test_frontmatter_parity`, which pins the other
deliberate duplication in this tree.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import List, Tuple

import pytest

from app.skills.sync import BUILTIN_SKILLS_DIR

#: The reference copy. Every other skill's must match it byte for byte.
REFERENCE = "gmail"

GOOGLE_SKILLS = ("gmail", "gcalendar", "gdrive", "gsheets", "gdocs")

#: Shared by all five.
UNIVERSAL = (
    "auth.py",
    "account_key.py",
    "discovery.py",
    "__init__.py",
    "golden_account_keys.json",
)

#: gmail's copy was deleted by ``app/skills/sync.py``'s obsolete-file sweep when
#: its listener was retired, so this one is shared by the other four.
LISTENER_ONLY = ("relay_client.py",)
LISTENER_SKILLS = tuple(s for s in GOOGLE_SKILLS if s != REFERENCE)


def _helper(skill: str, name: str) -> Path:
    return BUILTIN_SKILLS_DIR / skill / "scripts" / "app" / "google" / name


def _cases(names: Tuple[str, ...], skills: Tuple[str, ...], reference: str) -> List[Tuple[str, str, str]]:
    return [(name, reference, skill) for name in names for skill in skills if skill != reference]


def _assert_identical(name: str, reference: str, skill: str) -> None:
    left, right = _helper(reference, name), _helper(skill, name)
    assert left.is_file(), f"missing reference copy {left}"
    assert right.is_file(), f"missing {right}"
    if left.read_bytes() == right.read_bytes():
        return
    diff = "\n".join(
        difflib.unified_diff(
            left.read_text(encoding="utf-8", errors="replace").splitlines(),
            right.read_text(encoding="utf-8", errors="replace").splitlines(),
            fromfile=f"{reference}/{name}",
            tofile=f"{skill}/{name}",
            lineterm="",
        )
    )
    raise AssertionError(
        f"{skill}'s google/{name} has drifted from {reference}'s. Edit "
        f"{reference}'s copy, then copy it over the other four.\n\n{diff}"
    )


@pytest.mark.parametrize(
    "name,reference,skill",
    _cases(UNIVERSAL, GOOGLE_SKILLS, REFERENCE),
    ids=lambda value: value,
)
def test_the_shared_google_helpers_are_identical(name, reference, skill):
    _assert_identical(name, reference, skill)


@pytest.mark.parametrize(
    "name,reference,skill",
    _cases(LISTENER_ONLY, LISTENER_SKILLS, "gcalendar"),
    ids=lambda value: value,
)
def test_the_relay_client_is_identical_where_it_ships(name, reference, skill):
    _assert_identical(name, reference, skill)


def test_gmail_does_not_ship_a_relay_client():
    """Retired with its listener; ``sync.py`` deletes it on upgrade, so a copy
    reappearing here means someone re-added a file the sweep will remove."""
    assert not _helper(REFERENCE, "relay_client.py").exists()


def test_the_google_skill_set_is_discovered_not_assumed():
    """A sixth Google skill must not be able to slip past this file unnoticed."""
    discovered = {
        path.parents[3].name
        for path in BUILTIN_SKILLS_DIR.glob("*/scripts/app/google/auth.py")
    }
    assert discovered == set(GOOGLE_SKILLS)


@pytest.mark.parametrize("skill", GOOGLE_SKILLS)
def test_every_google_skill_ships_every_universal_helper(skill):
    for name in UNIVERSAL:
        assert _helper(skill, name).is_file(), f"{skill} is missing google/{name}"


@pytest.mark.parametrize("skill", LISTENER_SKILLS)
def test_the_listener_skills_ship_the_relay_client(skill):
    assert _helper(skill, "relay_client.py").is_file()
