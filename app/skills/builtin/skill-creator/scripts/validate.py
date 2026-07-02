# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Validate a Cremind skill directory before Cremind's scanner sees it.

Cremind's runtime scanner (``app/skills/scanner.py``) fails *silently*: a
skill whose ``SKILL.md`` frontmatter is malformed is logged-and-skipped and
simply never appears as a tool, with no signal inside the conversation. This
validator surfaces those failure modes up front as actionable errors.

    uv run scripts/validate.py <path-to-skill-dir>

Exit codes: 0 = PASS, 1 = validation errors, 2 = usage error.

The frontmatter-parsing logic below intentionally mirrors
``app/skills/scanner.py::_parse_frontmatter`` / ``parse_skill_dir``: every
check the scanner makes (frontmatter parses to a mapping; ``name`` and
``description`` are non-empty strings) is reproduced here, so a **PASS
guarantees the scanner will load the skill**. On top of that the validator
flags things the scanner accepts but that break at runtime — a ``name`` that
differs from the directory, a name that collides with an existing/built-in
skill, and malformed ``metadata`` blocks — so a FAIL can be stricter than the
scanner. Keep the parsing in sync with that file if the contract ever changes.
This script imports nothing from ``app.*`` — it must run standalone under
``uv`` from a profile skills dir where the Cremind package is not importable.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

# The three metadata keys Cremind actually consumes. Anything else under
# `metadata` is preserved but ignored, and is worth a warning.
KNOWN_METADATA_KEYS = {"environment_variables", "events", "long_running_app"}
# Frontmatter keys the scanner reads. Any other top-level key is ignored.
KNOWN_FRONTMATTER_KEYS = {"name", "description", "metadata"}

EVENT_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

BODY_BUDGET_CHARS = 10_000


class Report:
    """Collects ERROR / WARN findings and renders the final verdict."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warns: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    def render(self) -> int:
        for msg in self.errors:
            print(f"ERROR: {msg}")
        for msg in self.warns:
            print(f"WARN:  {msg}")
        if self.errors:
            print(f"\nFAIL: {len(self.errors)} error(s), {len(self.warns)} warning(s)")
            return 1
        suffix = f" ({len(self.warns)} warning(s))" if self.warns else ""
        print(f"\nPASS{suffix}")
        return 0


def _find_skill_md(entry: Path) -> Path | None:
    """Return the SKILL.md inside *entry* (case-insensitive), or None.

    Mirrors ``scanner._find_skill_md``.
    """
    direct = entry / "SKILL.md"
    if direct.exists():
        return direct
    try:
        candidates = [f for f in entry.iterdir() if f.name.lower() == "skill.md"]
    except OSError:
        return None
    return candidates[0] if candidates else None


def _parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, str | None]:
    """Extract YAML frontmatter, mirroring ``scanner._parse_frontmatter``.

    Returns ``(data, body, error)``. ``data`` is the parsed frontmatter dict
    (or None when there is no parseable frontmatter); ``body`` is the markdown
    after the closing fence; ``error`` is a human-readable reason the scanner
    would have discarded the frontmatter (None when fine).
    """
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return None, text, "no YAML frontmatter (file must begin with '---')"

    # The scanner locates the closing fence with ``find('---', 3)`` — the FIRST
    # '---' after the opening one. A literal '---' inside a frontmatter value
    # (or a '---' rule early in the body) therefore truncates the block.
    end_idx = stripped.find("---", 3)
    if end_idx == -1:
        return None, text, "frontmatter opening '---' has no closing '---'"

    yaml_block = stripped[3:end_idx]
    body = stripped[end_idx + 3:]
    try:
        data = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        return None, body, f"YAML frontmatter failed to parse: {exc}"

    if not isinstance(data, dict):
        return None, body, "YAML frontmatter is not a mapping (expected key: value pairs)"

    return data, body, None


def _sibling_names(target: Path) -> dict[str, list[str]]:
    """Map frontmatter ``name`` -> [dir names] for every sibling skill dir.

    Siblings include the built-in skills synced into a profile, so this also
    enforces the "built-in names are reserved" rule with no hardcoded list.
    """
    names: dict[str, list[str]] = {}
    parent = target.parent
    try:
        entries = list(parent.iterdir())
    except OSError:
        return names
    for entry in entries:
        if not entry.is_dir() or entry.resolve() == target.resolve():
            continue
        skill_md = _find_skill_md(entry)
        if skill_md is None:
            continue
        try:
            data, _, err = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        except OSError:
            continue
        if err or not isinstance(data, dict):
            continue
        name = data.get("name")
        if isinstance(name, str) and name:
            names.setdefault(name, []).append(entry.name)
    return names


def _validate_environment_variables(raw: Any, rep: Report) -> None:
    if not isinstance(raw, list):
        rep.error("metadata.environment_variables must be a list")
        return
    for i, entry in enumerate(raw):
        if isinstance(entry, str):
            if not entry:
                rep.error(f"environment_variables[{i}] is an empty string")
            continue
        if not isinstance(entry, dict):
            rep.error(f"environment_variables[{i}] must be a string or an object")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            rep.error(f"environment_variables[{i}] is missing a string 'name'")
            continue
        etype = entry.get("type")
        if etype is not None and not isinstance(etype, str):
            rep.warn(f"environment_variables '{name}': 'type' should be a string")
        if isinstance(etype, str) and etype == "enum" and not entry.get("enum"):
            rep.warn(f"environment_variables '{name}': type 'enum' but no 'enum' list given")
        unknown = set(entry) - {
            "name", "description", "required", "secret", "type", "default", "enum",
        }
        if unknown:
            rep.warn(
                f"environment_variables '{name}': unknown field(s) "
                f"{sorted(unknown)} (ignored by Cremind)"
            )


def _validate_events(
    meta: dict[str, Any], target: Path, rep: Report
) -> list[str]:
    """Validate metadata.events against the events/ folders. Returns declared names."""
    events = meta.get("events")
    declared: list[str] = []
    if events is None:
        # No declaration: any events/<x>/ folder is dead weight.
        for folder in _events_subfolders(target):
            rep.warn(
                f"events/{folder}/ exists but no event is declared in "
                f"metadata.events.event_type"
            )
        return declared

    if not isinstance(events, dict):
        rep.error("metadata.events must be an object like {event_type: [...]}")
        return declared
    items = events.get("event_type")
    if not isinstance(items, list):
        rep.error("metadata.events.event_type must be a list of {name, description}")
        return declared

    for i, item in enumerate(items):
        if not isinstance(item, dict):
            rep.error(f"events.event_type[{i}] must be an object with a 'name'")
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            rep.error(f"events.event_type[{i}] is missing a string 'name'")
            continue
        declared.append(name)
        if not EVENT_NAME_RE.match(name):
            rep.warn(
                f"event name '{name}' should match ^[a-z0-9_]+$ (it doubles as a "
                f"folder name)"
            )
        elif name.lower() in _WINDOWS_RESERVED:
            rep.warn(f"event name '{name}' is a Windows-reserved name")
        if not item.get("description"):
            rep.warn(f"event '{name}' has no description (shown when subscribing)")
        folder = target / "events" / name
        if not folder.is_dir():
            # Not fatal: the listener creates events/<name>/ at runtime via
            # mkdir(parents=True), and the recursive watch picks it up either
            # way. But shipping the folder (with a .gitkeep) means it exists
            # right after install and survives git.
            rep.warn(
                f"event '{name}' is declared but events/{name}/ folder is absent; "
                f"add events/{name}/.gitkeep so it ships and survives git"
            )
        elif not (folder / ".gitkeep").exists() and not any(folder.iterdir()):
            rep.warn(
                f"events/{name}/ is empty and has no .gitkeep (won't survive git)"
            )

    # Folders present on disk but not declared.
    for folder in _events_subfolders(target):
        if folder not in declared:
            rep.warn(
                f"events/{folder}/ exists but '{folder}' is not declared in "
                f"metadata.events.event_type"
            )
    return declared


def _events_subfolders(target: Path) -> list[str]:
    events_dir = target / "events"
    if not events_dir.is_dir():
        return []
    try:
        return [d.name for d in events_dir.iterdir() if d.is_dir()]
    except OSError:
        return []


def _validate_long_running_app(
    meta: dict[str, Any], target: Path, declared_events: list[str], rep: Report
) -> None:
    lra = meta.get("long_running_app")
    if lra is None:
        return
    if not isinstance(lra, dict):
        rep.error("metadata.long_running_app must be an object {command, description}")
        return
    command = lra.get("command")
    if not isinstance(command, str) or not command:
        rep.error("metadata.long_running_app is missing a string 'command'")
    else:
        # Best-effort: if the command references scripts/<file>, check it exists.
        m = re.search(r"scripts/([\w.\-]+\.py)", command)
        if m and not (target / "scripts" / m.group(1)).exists():
            rep.warn(
                f"long_running_app command references scripts/{m.group(1)} "
                f"which does not exist"
            )
    if not declared_events:
        rep.warn(
            "long_running_app is declared but the skill declares no events — a "
            "listener with nothing to emit is usually a design smell"
        )


def validate(target: Path) -> int:
    rep = Report()

    if not target.is_dir():
        rep.error(f"'{target}' is not a directory")
        return rep.render()

    skill_md = _find_skill_md(target)
    if skill_md is None:
        rep.error(f"no SKILL.md found in '{target}'")
        return rep.render()

    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        rep.error(f"could not read {skill_md}: {exc}")
        return rep.render()

    data, body, fm_err = _parse_frontmatter(content)
    if fm_err:
        rep.error(fm_err)
        return rep.render()
    assert data is not None  # fm_err is None here

    name = data.get("name")
    description = data.get("description")
    if not isinstance(name, str) or not name:
        rep.error("frontmatter 'name' is missing or not a string")
    if not isinstance(description, str) or not description:
        rep.error("frontmatter 'description' is missing or not a string")

    # name must equal the directory name (event dispatch falls back to the
    # slugified dir name; keeping them equal avoids surprises).
    if isinstance(name, str) and name and name != target.name:
        rep.error(
            f"frontmatter name '{name}' must equal the directory name "
            f"'{target.name}'"
        )

    # Sibling name-collision (covers reserved built-in names).
    if isinstance(name, str) and name:
        siblings = _sibling_names(target)
        if name in siblings:
            where = ", ".join(sorted(siblings[name]))
            rep.error(
                f"name '{name}' collides with an existing sibling skill "
                f"(dir: {where}); built-in names are reserved — pick another"
            )

    metadata = data.get("metadata", {})
    if "metadata" in data and not isinstance(metadata, dict):
        rep.error("frontmatter 'metadata' must be a mapping")
        metadata = {}

    if isinstance(metadata, dict):
        unknown_meta = set(metadata) - KNOWN_METADATA_KEYS
        if unknown_meta:
            rep.warn(
                f"metadata has key(s) {sorted(unknown_meta)} that Cremind ignores "
                f"(only environment_variables, events, long_running_app are used)"
            )
        if "environment_variables" in metadata:
            _validate_environment_variables(metadata["environment_variables"], rep)
        declared_events = _validate_events(metadata, target, rep)
        _validate_long_running_app(metadata, target, declared_events, rep)
    else:
        # metadata absent entirely — still surface stray events/ folders.
        for folder in _events_subfolders(target):
            rep.warn(
                f"events/{folder}/ exists but the skill declares no metadata.events"
            )

    # Unknown top-level frontmatter keys (ignored by the scanner).
    unknown_fm = set(data) - KNOWN_FRONTMATTER_KEYS
    if unknown_fm:
        rep.warn(
            f"frontmatter has key(s) {sorted(unknown_fm)} that Cremind ignores "
            f"(only name, description, metadata are read)"
        )

    # Body sanity.
    if not body.strip():
        rep.warn("SKILL.md has an empty body — the whole body is what the agent reads")
    elif len(body) > BODY_BUDGET_CHARS:
        rep.warn(
            f"SKILL.md body is {len(body)} chars (> {BODY_BUDGET_CHARS}); the whole "
            f"body is injected into context on load - consider moving depth to "
            f"references/"
        )

    return rep.render()


def main(argv: list[str]) -> int:
    # Skill fields may contain non-ASCII (e.g. Vietnamese); avoid a
    # UnicodeEncodeError on a legacy Windows console codepage.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = [a for a in argv[1:] if not a.startswith("-")]
    if len(args) != 1:
        print("usage: uv run scripts/validate.py <path-to-skill-dir>", file=sys.stderr)
        return 2
    return validate(Path(args[0]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
