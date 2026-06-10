"""Agent skills scanner.

Scans ``{CREMIND_SYSTEM_DIR}/skills/`` for skill directories containing a
``SKILL.md`` file. Each valid skill is parsed into a :class:`SkillInfo`.

Skills are pure context injection: their ``SKILL.md`` content is loaded
verbatim as a reasoning-agent observation. The frontmatter only needs
``name`` and ``description``; ``metadata`` is preserved for future use.
Any other frontmatter fields (``variables``, ``arguments``, ...) are ignored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.utils.logger import logger


@dataclass
class SkillInfo:
    """In-memory representation of a parsed agent skill."""
    name: str
    description: str
    dir_path: Path
    metadata: dict[str, Any] = field(default_factory=dict)
    full_content: str = ""


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML frontmatter from a ``SKILL.md`` file.

    Returns the full parsed frontmatter dict and a cleaned version of *text*
    where the frontmatter only contains ``name`` and ``description`` (all
    other keys such as ``metadata`` are stripped so they don't leak into the
    agent instruction prompt).
    """
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return {}, text

    end_idx = stripped.find("---", 3)
    if end_idx == -1:
        return {}, text

    yaml_block = stripped[3:end_idx]
    try:
        data = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        logger.warning(f"Failed to parse YAML frontmatter: {exc}")
        return {}, text

    if not isinstance(data, dict):
        return {}, text

    # Rebuild frontmatter with only description for the agent prompt; the
    # skill name is surfaced separately in the skill section header.
    clean_fm: dict[str, str] = {}
    if "description" in data:
        clean_fm["description"] = data["description"]
    body = stripped[end_idx + 3:]  # text after closing ---
    clean_yaml = yaml.dump(clean_fm, default_flow_style=False, allow_unicode=True).rstrip("\n")
    clean_text = f"---\n{clean_yaml}\n---{body}"

    return data, clean_text


def _find_skill_md(entry: Path) -> Path | None:
    """Return the ``SKILL.md`` inside *entry* (case-insensitive), or None."""
    skill_md = entry / "SKILL.md"
    if skill_md.exists():
        return skill_md
    try:
        candidates = [f for f in entry.iterdir() if f.name.lower() == "skill.md"]
    except OSError:
        return None
    return candidates[0] if candidates else None


def parse_skill_dir(entry: Path) -> SkillInfo | None:
    """Validate and parse a single skill directory.

    A valid skill directory contains a ``SKILL.md`` (case-insensitive) whose
    YAML frontmatter provides at least a string ``name`` and ``description``.

    Returns the parsed :class:`SkillInfo`, or ``None`` if *entry* is not a
    directory or its ``SKILL.md`` is missing/invalid (a warning is logged for
    present-but-invalid frontmatter, mirroring :func:`scan_skills`).
    """
    if not entry.is_dir():
        return None

    skill_md = _find_skill_md(entry)
    if skill_md is None:
        return None

    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"Could not read {skill_md}: {exc}")
        return None

    fm, full_text = _parse_frontmatter(content)

    name = fm.get("name")
    description = fm.get("description")

    if not name or not isinstance(name, str):
        logger.warning(
            f"Skipping skill dir '{entry.name}': missing or invalid 'name' in SKILL.md"
        )
        return None
    if not description or not isinstance(description, str):
        logger.warning(
            f"Skipping skill dir '{entry.name}': missing or invalid 'description' in SKILL.md"
        )
        return None

    metadata = fm.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}

    return SkillInfo(
        name=name,
        description=description,
        dir_path=entry,
        metadata=metadata,
        full_content=full_text,
    )


def scan_skills(skills_dir: Path) -> dict[str, SkillInfo]:
    """Scan a directory for valid agent skill subdirectories.

    Each subdirectory must contain a ``SKILL.md`` whose YAML frontmatter
    provides at least ``name`` and ``description``.

    Returns a dict keyed by skill name (first-write-wins on duplicates).
    """
    if not skills_dir.is_dir():
        return {}

    skills: dict[str, SkillInfo] = {}

    for entry in sorted(skills_dir.iterdir()):
        info = parse_skill_dir(entry)
        if info is None:
            continue

        if info.name in skills:
            logger.warning(
                f"Duplicate skill name '{info.name}' (dir '{entry.name}'); "
                "keeping first occurrence"
            )
            continue

        skills[info.name] = info
        logger.info(f"Discovered agent skill: '{info.name}' ({entry.name}/)")

    return skills


def find_skill_dirs(root: Path, *, max_depth: int = 4) -> list[SkillInfo]:
    """Recursively locate every valid skill directory under *root*.

    Walks *root* up to *max_depth* levels deep and returns a
    :class:`SkillInfo` for each directory that validates via
    :func:`parse_skill_dir`. Used by the importer to discover skills in an
    uploaded archive or cloned repo, which may place the skill at the archive
    root, one level down (``repo-main/SKILL.md``), or nested under a folder
    (``repo-main/skills/<name>/SKILL.md``).

    A directory that is itself a skill is NOT descended into (its ``scripts/``
    or asset folders never contain nested skills). Results are de-duplicated by
    resolved path and sorted by directory name for stable ordering.
    """
    if not root.is_dir():
        return []

    found: dict[str, SkillInfo] = {}

    def _walk(current: Path, depth: int) -> None:
        info = parse_skill_dir(current)
        if info is not None:
            found[str(current.resolve())] = info
            return  # don't descend into a skill's own subtree
        if depth >= max_depth:
            return
        try:
            children = sorted(current.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir() and not child.is_symlink():
                _walk(child, depth + 1)

    _walk(root, 0)
    return [found[k] for k in sorted(found, key=lambda p: found[p].dir_path.name)]


def generate_dir_tree(dir_path: Path, *, max_depth: int = 10) -> str:
    """Generate a visual directory tree string for *dir_path*.

    Uses box-drawing characters and the platform-native path separator
    as the directory trailing marker (``\\`` on Windows, ``/`` elsewhere).
    """
    if not dir_path.is_dir():
        return ""

    sep = os.sep
    lines: list[str] = [str(dir_path) + sep]

    def _walk(current: Path, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(
                current.iterdir(),
                key=lambda e: (not e.is_dir(), e.name.lower()),
            )
        except PermissionError:
            return

        # Hide dotfiles by default, but keep `.env` visible so users (and the
        # agent) can see that skill variables have been provisioned.
        entries = [
            e for e in entries
            if not e.name.startswith(".") or e.name == ".env"
        ]

        for i, entry in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            if entry.is_dir() and not entry.is_symlink():
                lines.append(f"{prefix}{connector}{entry.name}{sep}")
                extension = "    " if is_last else "│   "
                _walk(entry, prefix + extension, depth + 1)
            elif entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}{sep} -> {os.readlink(entry)}")
            else:
                lines.append(f"{prefix}{connector}{entry.name}")

    _walk(dir_path, "", 0)
    return "\n".join(lines)
