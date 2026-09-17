"""``Feature.requirements`` must match the extras in ``pyproject.toml``.

The manifest keeps its own copy of each version range because the running
server has no reliable way to read its extras back (an editable dev install's
metadata is as old as its last ``uv sync``). A copy can drift: bump the pin in
pyproject but not the manifest and existing installs never hear that they are
outdated; bump the manifest alone and every install asks pip for a range that
contradicts cremind's own ``[<extra>]`` pin. So every entry is compared with
the matching line in each of its extras groups AND in ``all``, by canonical
name and specifier set (so ``<0.155,>=0.154.0`` still equals
``>=0.154.0,<0.155``).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from app.features.manifest import FEATURES

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

_RANGED = sorted(
    (key, req)
    for key, feature in FEATURES.items()
    for req in feature.requirements
)


def _optional_dependencies() -> dict[str, list[str]]:
    with _PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]["optional-dependencies"]


def test_at_least_one_feature_declares_a_range() -> None:
    """Guards the parametrised cases below against passing vacuously."""
    assert ("codex", "openai-codex>=0.154.0,<0.155") in _RANGED


@pytest.mark.parametrize(("feature_key", "requirement"), _RANGED)
def test_requirement_matches_every_extras_group(feature_key: str, requirement: str) -> None:
    extras = _optional_dependencies()
    wanted = Requirement(requirement)
    groups = [*FEATURES[feature_key].extras, "all"]

    for group in groups:
        assert group in extras, f"{feature_key}: extras group {group!r} is not in pyproject.toml"
        matches = [
            Requirement(line)
            for line in extras[group]
            if canonicalize_name(Requirement(line).name) == canonicalize_name(wanted.name)
        ]
        assert len(matches) == 1, (
            f"{feature_key}: expected exactly one {wanted.name!r} line in "
            f"[project.optional-dependencies].{group}, found {len(matches)}"
        )
        assert matches[0].specifier == wanted.specifier, (
            f"{feature_key}: FEATURES[{feature_key!r}].requirements has {requirement!r} "
            f"but pyproject's {group!r} extra has {str(matches[0])!r} - update both together"
        )


@pytest.mark.parametrize(
    "feature_key", sorted({key for key, _ in _RANGED}),
)
def test_ranged_feature_names_every_package_of_its_extras(feature_key: str) -> None:
    """On the dev channel a feature that declares ``requirements`` is left out
    of the ``cremind[...]`` spec and installed through those strings alone
    (see ``pip_requirements``), so a package in its extras group that the
    tuple does not name would silently never be installed there."""
    extras = _optional_dependencies()
    declared = {
        canonicalize_name(Requirement(req).name) for req in FEATURES[feature_key].requirements
    }
    for group in FEATURES[feature_key].extras:
        missing = sorted(
            line for line in extras[group]
            if canonicalize_name(Requirement(line).name) not in declared
        )
        assert not missing, (
            f"{feature_key}: extras group {group!r} has {missing} that "
            f"FEATURES[{feature_key!r}].requirements does not name - the dev-channel "
            f"install would skip them"
        )
