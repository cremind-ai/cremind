"""Guardrail: every built-in SKILL.md must use standards-compliant YAML frontmatter.

Cremind Skills follow the Agent Skills standard (https://agentskills.io/specification):
a ``SKILL.md`` with a **YAML frontmatter** block (``---`` fences) followed by a
Markdown body. The built-ins previously hand-authored the ``metadata`` value in
JSON flow-style (``metadata: { environment_variables: [ {"name": ...} ] }``); because
JSON is a subset of YAML it still parsed, but it diverged from the standard and from
Cremind's own ``skill-creator`` templates. This test locks in the YAML block-style
form so a JSON-flow regression fails loudly instead of silently shipping.

It also enforces the standard's ``name``/``description`` constraints and Cremind's
"only three consumed metadata keys" contract (see
``app/skills/builtin/skill-creator/references/spec.md``).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.skills.scanner import parse_skill_dir
from app.skills.sync import BUILTIN_SKILLS_DIR

# Agent Skills standard: 1-64 chars, lowercase a-z/0-9 + single hyphens, no
# leading/trailing/consecutive hyphens.
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024

# Cremind consumes exactly these three keys under ``metadata`` (spec.md §3-§5).
KNOWN_METADATA_KEYS = {"environment_variables", "events", "long_running_app"}

# Keys whose values are collections and must therefore be YAML block style
# (a JSON-flow value would start with ``{`` or ``[``).
_STRUCTURAL_KEY_RE = re.compile(
    r"^\s*(metadata|environment_variables|events|event_type|long_running_app)\s*:(.*)$"
)
_SEQ_ITEM_RE = re.compile(r"^\s*-\s*(.*)$")


def _builtin_skill_dirs() -> list[Path]:
    return sorted(p for p in BUILTIN_SKILLS_DIR.iterdir() if (p / "SKILL.md").exists())


def _raw_frontmatter(text: str) -> str | None:
    """Return the raw text between the ``---`` fences, mirroring the scanner."""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return None
    end = stripped.find("---", 3)
    if end == -1:
        return None
    return stripped[3:end]


_SKILL_DIRS = _builtin_skill_dirs()
_IDS = [p.name for p in _SKILL_DIRS]


def test_builtin_skills_present() -> None:
    """Sanity: the built-in set is discovered (guards against an empty parametrize)."""
    assert _SKILL_DIRS, "no built-in skills found under app/skills/builtin"


@pytest.mark.parametrize("skill_dir", _SKILL_DIRS, ids=_IDS)
def test_builtin_frontmatter_parses_and_conforms(skill_dir: Path) -> None:
    info = parse_skill_dir(skill_dir)
    assert info is not None, f"{skill_dir.name}: SKILL.md failed to parse into a skill"

    # name: matches the Agent Skills rule and equals the directory name.
    assert info.name == skill_dir.name, (
        f"{skill_dir.name}: frontmatter name '{info.name}' must equal the directory name"
    )
    assert len(info.name) <= MAX_NAME, f"{skill_dir.name}: name exceeds {MAX_NAME} chars"
    assert NAME_RE.match(info.name), (
        f"{skill_dir.name}: name '{info.name}' violates the Agent Skills name rule "
        f"(lowercase a-z/0-9 and single non-edge hyphens)"
    )

    # description: non-empty, within the standard's length limit.
    assert info.description.strip(), f"{skill_dir.name}: description is empty"
    assert len(info.description) <= MAX_DESCRIPTION, (
        f"{skill_dir.name}: description exceeds {MAX_DESCRIPTION} chars"
    )

    # metadata: only the three consumed keys.
    unknown = set(info.metadata) - KNOWN_METADATA_KEYS
    assert not unknown, (
        f"{skill_dir.name}: metadata has unsupported key(s) {sorted(unknown)}; "
        f"Cremind only consumes {sorted(KNOWN_METADATA_KEYS)}"
    )


@pytest.mark.parametrize("skill_dir", _SKILL_DIRS, ids=_IDS)
def test_builtin_metadata_is_yaml_block_style(skill_dir: Path) -> None:
    """Fail if a built-in reverts to JSON flow-style metadata (the original bug)."""
    raw = _raw_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    assert raw is not None, f"{skill_dir.name}: no YAML frontmatter block found"

    for line in raw.splitlines():
        struct = _STRUCTURAL_KEY_RE.match(line)
        if struct:
            key, value = struct.group(1), struct.group(2).strip()
            assert not value.startswith(("{", "[")), (
                f"{skill_dir.name}: '{key}' uses JSON flow style ({line.strip()!r}); "
                f"use YAML block style"
            )
        item = _SEQ_ITEM_RE.match(line)
        if item:
            value = item.group(1).strip()
            assert not value.startswith(("{", "[")), (
                f"{skill_dir.name}: a list item uses JSON flow style ({line.strip()!r}); "
                f"use YAML block style"
            )
